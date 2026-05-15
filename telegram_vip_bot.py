#!/usr/bin/env python3
import asyncio
import datetime as dt
import html
import io
import logging
import os
import random
import secrets
from dataclasses import dataclass

from dotenv import load_dotenv
from supabase import create_client
from telethon import Button, TelegramClient, events, functions

from sociabuzz_client import (
    SociaBuzzError,
    create_donation_order,
    create_qris,
    download_qr_response,
    new_session,
    check_pending,
)


load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger("telegram_vip_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)


FIRST_NAMES = [
    "Agus",
    "Andi",
    "Bambang",
    "Budi",
    "Dedi",
    "Dian",
    "Eka",
    "Fajar",
    "Hendra",
    "Joko",
    "Rizki",
    "Sari",
    "Siti",
    "Taufik",
    "Wahyu",
    "Yudha",
]

LAST_NAMES = [
    "Saputra",
    "Pratama",
    "Santoso",
    "Wijaya",
    "Nugroho",
    "Kurniawan",
    "Hidayat",
    "Setiawan",
    "Permana",
    "Ramadhan",
    "Maulana",
    "Lestari",
]


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    vip_chat_id: int
    log_chat_id: int
    sociabuzz_username: str
    sociabuzz_cookie: str
    payment_amount: int
    invite_expire_hours: int
    poll_interval_seconds: int
    poll_max_attempts: int
    admin_user_ids: set[int]
    supabase_url: str
    supabase_service_role_key: str
    supabase_table: str


def env_required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env: {name}")
    return value


def env_int(name, default=None):
    value = os.getenv(name, "").strip()
    if not value:
        if default is None:
            raise RuntimeError(f"Missing required env: {name}")
        return default
    return int(value)


def env_optional_int(name, default=0):
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def parse_admin_ids(raw):
    ids = set()
    for item in raw.split(","):
        item = item.strip()
        if item:
            ids.add(int(item))
    return ids


def load_config():
    return Config(
        api_id=env_int("TELEGRAM_API_ID"),
        api_hash=env_required("TELEGRAM_API_HASH"),
        bot_token=env_required("TELEGRAM_BOT_TOKEN"),
        vip_chat_id=env_optional_int("VIP_CHAT_ID"),
        log_chat_id=env_optional_int("LOG_CHAT_ID"),
        sociabuzz_username=os.getenv("SOCIABUZZ_USERNAME", "yudhaprihardana").strip(),
        sociabuzz_cookie=os.getenv("SOCIABUZZ_COOKIE", "").strip(),
        payment_amount=env_int("PAYMENT_AMOUNT", 2000),
        invite_expire_hours=env_int("INVITE_EXPIRE_HOURS", 24),
        poll_interval_seconds=env_int("POLL_INTERVAL_SECONDS", 3),
        poll_max_attempts=env_int("POLL_MAX_ATTEMPTS", 300),
        admin_user_ids=parse_admin_ids(os.getenv("ADMIN_USER_IDS", "")),
        supabase_url=env_required("SUPABASE_URL"),
        supabase_service_role_key=env_required("SUPABASE_SERVICE_ROLE_KEY"),
        supabase_table=os.getenv("SUPABASE_TABLE", "vip_payments").strip() or "vip_payments",
    )


class PaymentStore:
    def __init__(self, config):
        self.table = config.supabase_table
        self.settings_table = os.getenv("SUPABASE_SETTINGS_TABLE", "vip_bot_settings").strip() or "vip_bot_settings"
        self.client = create_client(config.supabase_url, config.supabase_service_role_key)

    def create_payment(self, user, order_id, payment_url, inv_id, amount, buyer_name, buyer_email, qris_data):
        now = utc_now_iso()
        payload = qris_data.get("data", {})
        data = {
            "user_id": user.id,
            "username": user.username or "",
            "full_name": display_name(user),
            "order_id": order_id,
            "payment_url": payment_url,
            "inv_id": inv_id,
            "amount": amount,
            "status": "pending",
            "buyer_name": buyer_name,
            "buyer_email": buyer_email,
            "qris_amount": payload.get("amount") or "",
            "qris_expires": payload.get("countdown") or "",
            "created_at": now,
            "updated_at": now,
        }
        self.client.table(self.table).insert(data).execute()

    def latest_pending_for_user(self, user_id):
        response = (
            self.client.table(self.table)
            .select("*")
            .eq("user_id", user_id)
            .eq("status", "pending")
            .order("id", desc=True)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None

    def pending_payments(self):
        response = (
            self.client.table(self.table)
            .select("*")
            .eq("status", "pending")
            .order("id", desc=False)
            .execute()
        )
        return response.data or []

    def get_by_inv_id(self, inv_id):
        response = self.client.table(self.table).select("*").eq("inv_id", inv_id).limit(1).execute()
        return response.data[0] if response.data else None

    def mark_status(self, inv_id, status, error=""):
        self.client.table(self.table).update(
            {"status": status, "error": error, "updated_at": utc_now_iso()}
        ).eq("inv_id", inv_id).execute()

    def mark_paid(self, inv_id, invite_link, invite_expires_at):
        response = (
            self.client.table(self.table)
            .update(
                {
                    "status": "paid",
                    "invite_link": invite_link,
                    "invite_expires_at": invite_expires_at,
                    "updated_at": utc_now_iso(),
                }
            )
            .eq("inv_id", inv_id)
            .eq("status", "pending")
            .execute()
        )
        return bool(response.data)

    def get_setting(self, key, default=""):
        response = self.client.table(self.settings_table).select("value").eq("key", key).limit(1).execute()
        if not response.data:
            return default
        return response.data[0].get("value") or default

    def set_setting(self, key, value):
        now = utc_now_iso()
        self.client.table(self.settings_table).upsert(
            {"key": key, "value": str(value), "updated_at": now},
            on_conflict="key",
        ).execute()

    def get_int_setting(self, key, default=0):
        value = self.get_setting(key, "")
        if not value:
            return default
        return int(value)


def utc_now_iso():
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def display_name(user):
    parts = [user.first_name or "", user.last_name or ""]
    name = " ".join(part for part in parts if part).strip()
    return name or str(user.id)


def random_indonesian_identity():
    first = secrets.choice(FIRST_NAMES)
    last = secrets.choice(LAST_NAMES)
    suffix = random.randint(1000, 999999)
    email = f"{first.lower()}.{last.lower()}{suffix}@gmail.com"
    return f"{first} {last}", email


def user_link(row):
    name = html.escape(row["full_name"] or str(row["user_id"]))
    return f'<a href="tg://user?id={row["user_id"]}">{name}</a>'


def is_admin(config, user_id):
    return user_id in config.admin_user_ids


def runtime_vip_chat_id(config, store):
    return store.get_int_setting("vip_chat_id", config.vip_chat_id)


def runtime_log_chat_id(config, store):
    return store.get_int_setting("log_chat_id", config.log_chat_id)


async def send_log(client, config, store, text):
    log_chat_id = runtime_log_chat_id(config, store)
    if not log_chat_id:
        return
    try:
        await client.send_message(log_chat_id, text, parse_mode="html", link_preview=False)
    except Exception as exc:
        LOGGER.warning("Failed to send log message to %s: %s", log_chat_id, exc)


def create_qris_sync(config, user):
    session = new_session(config.sociabuzz_cookie)
    buyer_name, buyer_email = random_indonesian_identity()
    note = f"VIP {user.id}"
    order_id, payment_url, _ = create_donation_order(
        session,
        config.sociabuzz_username,
        config.payment_amount,
        buyer_name,
        buyer_email,
        note,
    )
    qris = create_qris(session, order_id, payment_url, config.payment_amount)
    qr_response = download_qr_response(session, qris)
    return session, buyer_name, buyer_email, order_id, payment_url, qris, qr_response.content


def check_payment_sync(config, inv_id):
    session = new_session(config.sociabuzz_cookie)
    return check_pending(session, inv_id)


async def create_invite_link(client, config, store, payment):
    vip_chat_id = runtime_vip_chat_id(config, store)
    if not vip_chat_id:
        raise RuntimeError("VIP chat belum di-set. Admin perlu pakai /setvip <chat_id>.")
    expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(hours=config.invite_expire_hours)
    result = await client(
        functions.messages.ExportChatInviteRequest(
            peer=vip_chat_id,
            expire_date=expires_at,
            usage_limit=1,
            title=f"VIP {payment['inv_id']}",
        )
    )
    return result.link, expires_at.replace(microsecond=0).isoformat()


def qris_caption(config, inv_id, final_amount, expires):
    lines = [
        "Scan QRIS ini untuk pembayaran VIP.",
        f"Paket: Rp{config.payment_amount:,}".replace(",", "."),
        f"Invoice: {inv_id}",
        "",
        "Status pembayaran dicek otomatis. Link VIP dikirim setelah pembayaran terdeteksi.",
    ]
    if final_amount:
        lines.insert(2, f"Nominal QRIS: {final_amount}")
    if expires:
        lines.insert(4, f"Batas bayar: {expires}")
    return "\n".join(lines)


async def send_qris(event, config, store):
    user = await event.get_sender()
    pending = store.latest_pending_for_user(user.id)
    if pending:
        await event.respond(
            "Masih ada pembayaran yang sedang dicek. Tunggu statusnya selesai dulu sebelum membuat QRIS baru."
        )
        return

    status_msg = await event.respond("Membuat QRIS...")
    try:
        _session, buyer_name, buyer_email, order_id, payment_url, qris, qr_bytes = await asyncio.to_thread(
            create_qris_sync, config, user
        )
        inv_id = qris.get("inv_id")
        if not inv_id:
            raise SociaBuzzError(f"QRIS response missing inv_id: {qris}")

        store.create_payment(
            user,
            order_id,
            payment_url,
            inv_id,
            config.payment_amount,
            buyer_name,
            buyer_email,
            qris,
        )
        qr_file = io.BytesIO(qr_bytes)
        qr_file.name = f"{inv_id}.png"
        payload = qris.get("data", {})
        await event.client.send_file(
            event.chat_id,
            qr_file,
            caption=qris_caption(config, inv_id, payload.get("amount") or "", payload.get("countdown") or ""),
            force_document=False,
        )
        await status_msg.delete()
        await send_log(
            event.client,
            config,
            store,
            (
                "<b>QRIS created</b>\n"
                f"User: {html.escape(display_name(user))} (<code>{user.id}</code>)\n"
                f"Invoice: <code>{html.escape(inv_id)}</code>\n"
                f"Order: <code>{html.escape(order_id)}</code>\n"
                f"Amount: <code>{config.payment_amount}</code>"
            ),
        )
    except Exception as exc:
        LOGGER.exception("Failed to create QRIS")
        await status_msg.edit("Gagal membuat QRIS. Coba lagi beberapa saat lagi.")
        await send_log(event.client, config, store, f"<b>QRIS error</b>\n<code>{html.escape(str(exc))}</code>")


async def process_paid_payment(client, config, store, payment):
    invite_link, invite_expires_at = await create_invite_link(client, config, store, payment)
    changed = store.mark_paid(payment["inv_id"], invite_link, invite_expires_at)
    if not changed:
        return

    await client.send_message(
        payment["user_id"],
        (
            "Pembayaran terdeteksi.\n\n"
            f"Link VIP:\n{invite_link}\n\n"
            "Link ini hanya bisa dipakai 1 kali dan berlaku 24 jam."
        ),
        link_preview=False,
    )
    await send_log(
        client,
        config,
        store,
        (
            "<b>Payment paid</b>\n"
            f"User: {user_link(payment)} (<code>{payment['user_id']}</code>)\n"
            f"Invoice: <code>{html.escape(payment['inv_id'])}</code>\n"
            f"Invite expires: <code>{html.escape(invite_expires_at)}</code>"
        ),
    )


async def poll_once(client, config, store, payment):
    try:
        status, status_url, elapsed_ms = await asyncio.to_thread(check_payment_sync, config, payment["inv_id"])
        LOGGER.info("Invoice %s status=%s latency=%sms", payment["inv_id"], status, elapsed_ms)
        if status == "paid":
            await process_paid_payment(client, config, store, payment)
        elif status in {"failed_or_expired", "unknown"}:
            store.mark_status(payment["inv_id"], status)
            await client.send_message(
                payment["user_id"],
                "Pembayaran belum berhasil atau sudah tidak valid. Silakan buat QRIS baru.",
            )
            await send_log(
                client,
                config,
                store,
                (
                    f"<b>Payment {html.escape(status)}</b>\n"
                    f"User: {user_link(payment)} (<code>{payment['user_id']}</code>)\n"
                    f"Invoice: <code>{html.escape(payment['inv_id'])}</code>\n"
                    f"Check: {html.escape(status_url)}"
                ),
            )
    except Exception as exc:
        LOGGER.exception("Polling failed for %s", payment["inv_id"])
        store.mark_status(payment["inv_id"], "poll_error", str(exc))
        await send_log(
            client,
            config,
            store,
            (
                "<b>Polling error</b>\n"
                f"Invoice: <code>{html.escape(payment['inv_id'])}</code>\n"
                f"Error: <code>{html.escape(str(exc))}</code>"
            ),
        )


async def polling_loop(client, config, store):
    attempts = {}
    while True:
        pending = store.pending_payments()
        for payment in pending:
            count = attempts.get(payment["inv_id"], 0) + 1
            attempts[payment["inv_id"]] = count
            if count > config.poll_max_attempts:
                store.mark_status(payment["inv_id"], "timeout")
                await client.send_message(
                    payment["user_id"],
                    "Pembayaran belum terdeteksi sampai batas waktu pengecekan. Silakan buat QRIS baru.",
                )
                await send_log(
                    client,
                    config,
                    store,
                    (
                        "<b>Payment timeout</b>\n"
                        f"User: {user_link(payment)} (<code>{payment['user_id']}</code>)\n"
                        f"Invoice: <code>{html.escape(payment['inv_id'])}</code>"
                    ),
                )
                continue
            await poll_once(client, config, store, payment)
        await asyncio.sleep(config.poll_interval_seconds)


def private_only(handler):
    async def wrapped(event):
        if not event.is_private:
            return
        await handler(event)

    return wrapped


def parse_chat_setting(event, raw_value):
    value = raw_value.strip()
    if value.lower() in {"here", "this"}:
        return event.chat_id
    return int(value)


async def require_admin(event, config):
    if is_admin(config, event.sender_id):
        return True
    await event.respond("Command ini khusus admin.")
    return False


async def main():
    config = load_config()
    store = PaymentStore(config)
    client = TelegramClient("vip_bot", config.api_id, config.api_hash)

    @client.on(events.NewMessage(pattern=r"^/start$"))
    @private_only
    async def start(event):
        await event.respond(
            "Akses VIP tersedia dengan pembayaran QRIS.",
            buttons=[[Button.inline(f"Beli VIP - Rp{config.payment_amount:,}".replace(",", "."), b"buy_vip")]],
        )

    @client.on(events.NewMessage(pattern=r"^/buy$"))
    @private_only
    async def buy_command(event):
        await send_qris(event, config, store)

    @client.on(events.CallbackQuery(data=b"buy_vip"))
    async def buy_callback(event):
        if not event.is_private:
            await event.answer("Buka bot lewat private chat.", alert=True)
            return
        await event.answer("Membuat QRIS...")
        await send_qris(event, config, store)

    @client.on(events.NewMessage(pattern=r"^/status$"))
    @private_only
    async def status(event):
        pending = store.latest_pending_for_user(event.sender_id)
        if pending:
            await event.respond(f"Pembayaran masih dicek otomatis.\nInvoice: {pending['inv_id']}")
        else:
            await event.respond("Tidak ada pembayaran pending.")

    @client.on(events.NewMessage(pattern=r"^/chatid$"))
    async def chat_id(event):
        await event.respond(f"chat_id: `{event.chat_id}`")

    @client.on(events.NewMessage(pattern=r"^/setvip(?:\s+(.+))?$"))
    async def set_vip(event):
        if not await require_admin(event, config):
            return
        raw_value = event.pattern_match.group(1)
        if not raw_value:
            await event.respond("Format: `/setvip <chat_id>` atau `/setvip here`")
            return
        try:
            chat_id_value = parse_chat_setting(event, raw_value)
            store.set_setting("vip_chat_id", chat_id_value)
            await event.respond(f"VIP chat diset ke `{chat_id_value}`.")
            await send_log(client, config, store, f"<b>Config updated</b>\n<code>vip_chat_id={chat_id_value}</code>")
        except Exception as exc:
            await event.respond(f"Gagal set VIP chat: `{html.escape(str(exc))}`")

    @client.on(events.NewMessage(pattern=r"^/setlog(?:\s+(.+))?$"))
    async def set_log(event):
        if not await require_admin(event, config):
            return
        raw_value = event.pattern_match.group(1)
        if not raw_value:
            await event.respond("Format: `/setlog <chat_id>` atau `/setlog here`")
            return
        try:
            chat_id_value = parse_chat_setting(event, raw_value)
            store.set_setting("log_chat_id", chat_id_value)
            await event.respond(f"Log chat diset ke `{chat_id_value}`.")
            await send_log(client, config, store, f"<b>Config updated</b>\n<code>log_chat_id={chat_id_value}</code>")
        except Exception as exc:
            await event.respond(f"Gagal set log chat: `{html.escape(str(exc))}`")

    @client.on(events.NewMessage(pattern=r"^/config$"))
    async def show_config(event):
        if not await require_admin(event, config):
            return
        vip_chat_id = runtime_vip_chat_id(config, store)
        log_chat_id = runtime_log_chat_id(config, store)
        await event.respond(
            "Config aktif:\n"
            f"VIP_CHAT_ID: `{vip_chat_id or 'belum diset'}`\n"
            f"LOG_CHAT_ID: `{log_chat_id or 'belum diset'}`\n"
            f"PAYMENT_AMOUNT: `{config.payment_amount}`\n"
            f"INVITE_EXPIRE_HOURS: `{config.invite_expire_hours}`"
        )

    await client.start(bot_token=config.bot_token)
    await send_log(client, config, store, "<b>VIP bot started</b>")
    LOGGER.info("VIP bot started")
    asyncio.create_task(polling_loop(client, config, store))
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
