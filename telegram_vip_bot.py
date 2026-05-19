#!/usr/bin/env python3
import asyncio
import datetime as dt
import html
import io
import logging
import os
import random
import secrets
import string
import time
from dataclasses import dataclass

import httpx
from dotenv import load_dotenv
from supabase import create_client
from telethon import Button, TelegramClient, events, functions, errors

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

ACTIVE_PAYMENT_STATUSES = ("pending", "processing_paid", "invite_error", "delivery_error", "processing_delivery")
RETRYABLE_PAYMENT_STATUSES = ("pending", "invite_error", "delivery_error")


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
    poll_batch_size: int
    qris_create_concurrency: int
    admin_user_ids: set[int]
    supabase_url: str
    supabase_service_role_key: str
    supabase_table: str
    supabase_package_table: str


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
        sociabuzz_username=env_required("SOCIABUZZ_USERNAME"),
        sociabuzz_cookie=os.getenv("SOCIABUZZ_COOKIE", "").strip(),
        payment_amount=env_int("PAYMENT_AMOUNT", 2000),
        invite_expire_hours=env_int("INVITE_EXPIRE_HOURS", 24),
        poll_interval_seconds=env_int("POLL_INTERVAL_SECONDS", 3),
        poll_max_attempts=env_int("POLL_MAX_ATTEMPTS", 300),
        poll_batch_size=max(1, env_int("POLL_BATCH_SIZE", 20)),
        qris_create_concurrency=max(1, env_int("QRIS_CREATE_CONCURRENCY", 5)),
        admin_user_ids=parse_admin_ids(os.getenv("ADMIN_USER_IDS", "")),
        supabase_url=env_required("SUPABASE_URL"),
        supabase_service_role_key=env_required("SUPABASE_SERVICE_ROLE_KEY"),
        supabase_table=os.getenv("SUPABASE_TABLE", "vip_payments").strip() or "vip_payments",
        supabase_package_table=os.getenv("SUPABASE_PACKAGE_TABLE", "vip_packages").strip() or "vip_packages",
    )


class PaymentStore:
    def __init__(self, config):
        self.table = config.supabase_table
        self.package_table = config.supabase_package_table
        self.settings_table = os.getenv("SUPABASE_SETTINGS_TABLE", "vip_bot_settings").strip() or "vip_bot_settings"
        self.client = create_client(config.supabase_url, config.supabase_service_role_key)
        self.query_retries = max(1, env_int("SUPABASE_QUERY_RETRIES", 3))
        self.retry_base_delay = max(0.1, float(os.getenv("SUPABASE_RETRY_BASE_DELAY", "0.35")))

    def _execute(self, query, action):
        for attempt in range(1, self.query_retries + 1):
            try:
                return query.execute()
            except httpx.TransportError as exc:
                if attempt >= self.query_retries:
                    raise
                delay = min(4.0, self.retry_base_delay * (2 ** (attempt - 1))) + random.uniform(0, 0.15)
                LOGGER.warning(
                    "Transient Supabase transport error during %s, retrying in %.2fs (%s/%s): %s",
                    action,
                    delay,
                    attempt,
                    self.query_retries,
                    exc,
                )
                time.sleep(delay)

    def create_payment(
        self,
        user,
        public_invoice_id,
        order_id,
        payment_url,
        inv_id,
        amount,
        buyer_name,
        buyer_email,
        qris_data,
        qris_chat_id,
        qris_message_id,
        package=None,
    ):
        now = utc_now_iso()
        payload = qris_data.get("data", {})
        package = package or {}
        expires_at = parse_iso_datetime(payload.get("countdown") or "")
        next_check_at = next_poll_at(dt.datetime.now(dt.UTC), expires_at, attempts=0, error="")
        data = {
            "user_id": user.id,
            "username": user.username or "",
            "full_name": display_name(user),
            "package_code": package.get("code") or "",
            "package_name": package.get("name") or "",
            "package_amount": int(package.get("amount") or amount),
            "vip_chat_id": package.get("vip_chat_id"),
            "invite_expire_hours": int(package.get("invite_expire_hours") or 0),
            "public_invoice_id": public_invoice_id,
            "order_id": order_id,
            "payment_url": payment_url,
            "inv_id": inv_id,
            "amount": amount,
            "status": "pending",
            "buyer_name": buyer_name,
            "buyer_email": buyer_email,
            "qris_amount": payload.get("amount") or "",
            "qris_expires": payload.get("countdown") or "",
            "qris_chat_id": qris_chat_id,
            "qris_message_id": qris_message_id,
            "next_check_at": next_check_at,
            "poll_attempts": 0,
            "created_at": now,
            "updated_at": now,
        }
        self._execute(self.client.table(self.table).insert(data), "create payment")

    def ensure_payment_schema_ready(self):
        columns = "id,package_code,package_name,package_amount,vip_chat_id,invite_expire_hours,next_check_at,poll_attempts,last_polled_at"
        query = self.client.table(self.table).select(columns).limit(1)
        self._execute(query, "check payment schema")

    def list_packages(self, include_inactive=False):
        query = self.client.table(self.package_table).select("*")
        if not include_inactive:
            query = query.eq("active", True)
        query = query.order("sort_order", desc=False).order("code", desc=False)
        response = self._execute(query, "list packages")
        return response.data or []

    def get_package(self, code):
        query = self.client.table(self.package_table).select("*").eq("code", normalize_package_code(code)).eq("active", True).limit(1)
        response = self._execute(query, "get package")
        return response.data[0] if response.data else None

    def upsert_package(self, code, name, vip_chat_id, amount, invite_expire_hours=0):
        now = utc_now_iso()
        data = {
            "code": normalize_package_code(code),
            "name": name.strip(),
            "vip_chat_id": int(vip_chat_id),
            "amount": int(amount),
            "invite_expire_hours": int(invite_expire_hours or 0),
            "active": True,
            "updated_at": now,
        }
        query = self.client.table(self.package_table).upsert(data, on_conflict="code")
        self._execute(query, "upsert package")

    def delete_package(self, code):
        query = self.client.table(self.package_table).update(
            {"active": False, "updated_at": utc_now_iso()}
        ).eq("code", normalize_package_code(code))
        response = self._execute(query, "delete package")
        return bool(response.data)

    def latest_pending_for_user(self, user_id):
        rows = []
        for status in ACTIVE_PAYMENT_STATUSES:
            query = (
                self.client.table(self.table)
                .select("*")
                .eq("user_id", user_id)
                .eq("status", status)
                .order("id", desc=True)
                .limit(1)
            )
            response = self._execute(query, f"latest active payment {status}")
            rows.extend(response.data or [])
        rows.sort(key=lambda item: item["id"], reverse=True)
        return rows[0] if rows else None

    def retryable_payments(self, due_before, limit):
        rows = []
        for status in RETRYABLE_PAYMENT_STATUSES:
            query = (
                self.client.table(self.table)
                .select("*")
                .eq("status", status)
                .lte("next_check_at", due_before)
                .order("next_check_at", desc=False)
                .order("id", desc=False)
                .limit(limit)
            )
            response = self._execute(query, f"retryable payments {status}")
            rows.extend(response.data or [])
        rows.sort(key=lambda item: ((item.get("next_check_at") or ""), item["id"]))
        return rows[:limit]

    def recover_stale_processing(self, older_than_seconds=300):
        cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=older_than_seconds)).replace(microsecond=0).isoformat()
        now = utc_now_iso()
        query = self.client.table(self.table).update(
            {
                "status": "invite_error",
                "error": "Recovered stale paid processing",
                "updated_at": now,
            }
        ).eq("status", "processing_paid").lt("updated_at", cutoff)
        self._execute(query, "recover stale paid processing")
        query = self.client.table(self.table).update(
            {
                "status": "delivery_error",
                "error": "Recovered stale delivery processing",
                "updated_at": now,
            }
        ).eq("status", "processing_delivery").lt("updated_at", cutoff)
        self._execute(query, "recover stale delivery processing")

    def get_by_inv_id(self, inv_id):
        query = self.client.table(self.table).select("*").eq("inv_id", inv_id).limit(1)
        response = self._execute(query, "get payment by invoice")
        return response.data[0] if response.data else None

    def set_error(self, inv_id, error):
        query = self.client.table(self.table).update(
            {"error": error[:1000], "updated_at": utc_now_iso()}
        ).eq("inv_id", inv_id)
        self._execute(query, "set payment error")

    def mark_status_if_current(self, inv_id, from_status, to_status, error=""):
        data = {"status": to_status, "error": error, "updated_at": utc_now_iso()}
        if to_status in RETRYABLE_PAYMENT_STATUSES:
            data["next_check_at"] = utc_now_iso()
        else:
            data["next_check_at"] = None
        query = self.client.table(self.table).update(
            data
        ).eq("inv_id", inv_id).eq("status", from_status)
        response = self._execute(query, f"mark status {from_status} to {to_status}")
        return bool(response.data)

    def record_poll_result(self, payment, next_check_at, error=""):
        attempts = int(payment.get("poll_attempts") or 0) + 1
        data = {
            "poll_attempts": attempts,
            "last_polled_at": utc_now_iso(),
            "next_check_at": next_check_at,
            "updated_at": utc_now_iso(),
        }
        if error:
            data["error"] = error[:1000]
        elif payment.get("error"):
            data["error"] = ""
        query = self.client.table(self.table).update(data).eq("inv_id", payment["inv_id"]).eq("status", payment["status"])
        self._execute(query, "record poll result")

    def claim_paid_processing(self, inv_id):
        return self.mark_status_if_current(inv_id, "pending", "processing_paid") or self.mark_status_if_current(
            inv_id, "invite_error", "processing_paid"
        )

    def mark_invite_error(self, inv_id, error):
        return self.mark_status_if_current(inv_id, "processing_paid", "invite_error", error[:1000])

    def mark_delivery_processing(self, inv_id, invite_link, invite_expires_at):
        query = (
            self.client.table(self.table)
            .update(
                {
                    "status": "processing_delivery",
                    "invite_link": invite_link,
                    "invite_expires_at": invite_expires_at,
                    "next_check_at": None,
                    "error": "",
                    "updated_at": utc_now_iso(),
                }
            )
            .eq("inv_id", inv_id)
            .eq("status", "processing_paid")
        )
        response = self._execute(query, "mark delivery processing")
        return bool(response.data)

    def claim_delivery_processing(self, inv_id):
        return self.mark_status_if_current(inv_id, "delivery_error", "processing_delivery")

    def mark_delivery_error(self, inv_id, error):
        return self.mark_status_if_current(inv_id, "processing_delivery", "delivery_error", error[:1000])

    def mark_delivery_blocked(self, inv_id, error):
        return self.mark_status_if_current(inv_id, "processing_delivery", "delivery_blocked", error[:1000])

    def mark_paid(self, inv_id, invite_link, invite_expires_at):
        query = (
            self.client.table(self.table)
            .update(
                {
                    "status": "paid",
                    "invite_link": invite_link,
                    "invite_expires_at": invite_expires_at,
                    "next_check_at": None,
                    "updated_at": utc_now_iso(),
                }
            )
            .eq("inv_id", inv_id)
            .eq("status", "processing_delivery")
        )
        response = self._execute(query, "mark paid")
        return bool(response.data)

    def get_setting(self, key, default=""):
        query = self.client.table(self.settings_table).select("value").eq("key", key).limit(1)
        response = self._execute(query, "get bot setting")
        if not response.data:
            return default
        return response.data[0].get("value") or default

    def set_setting(self, key, value):
        now = utc_now_iso()
        query = self.client.table(self.settings_table).upsert(
            {"key": key, "value": str(value), "updated_at": now},
            on_conflict="key",
        )
        self._execute(query, "set bot setting")

    def get_int_setting(self, key, default=0):
        value = self.get_setting(key, "")
        if not value:
            return default
        return int(value)


def utc_now_iso():
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def parse_iso_datetime(raw):
    if not raw:
        return None
    if isinstance(raw, dt.datetime):
        parsed = raw
    else:
        try:
            parsed = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def adaptive_poll_delay(created_at, expires_at, attempts=0, error=""):
    now = dt.datetime.now(dt.UTC)
    if expires_at:
        remaining = (expires_at - now).total_seconds()
        if remaining <= 0:
            return None
        total = max((expires_at - created_at).total_seconds(), 1)
        elapsed = max((now - created_at).total_seconds(), 0)
        progress = max(0.0, min(1.0, elapsed / total))
    else:
        remaining = None
        total = 1800
        progress = min(1.0, max(0, attempts) / 300)

    if error:
        base = min(180, max(30, 20 * max(1, min(int(attempts or 1), 6))))
        if remaining is None:
            return base
        return max(5, min(base, int(max(5, remaining / 2))))

    if remaining is not None:
        final_window = max(30, min(120, total * 0.20))
        if remaining <= final_window:
            return 5

    if progress < 0.05:
        return 5
    if progress < 0.15:
        return 8
    if progress < 0.75:
        return min(45, max(12, int(total * 0.015)))
    return 10


def next_poll_at(created_at, expires_at, attempts=0, error=""):
    delay = adaptive_poll_delay(created_at, expires_at, attempts=attempts, error=error)
    if delay is None:
        return None
    return (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=delay)).replace(microsecond=0).isoformat()


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


def public_invoice_id():
    date_part = dt.datetime.now(dt.timezone(dt.timedelta(hours=7))).strftime("%y%m%d")
    suffix = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    return f"VIP-{date_part}-{suffix}"


def format_rupiah(amount):
    return f"Rp{amount:,}".replace(",", ".")


def format_button_amount(amount):
    return f"{int(amount):,}".replace(",", ".")


def normalize_package_code(code):
    normalized = (code or "").strip().lower()
    if not normalized:
        raise ValueError("Kode paket wajib diisi.")
    if len(normalized) > 32:
        raise ValueError("Kode paket maksimal 32 karakter.")
    allowed = set(string.ascii_lowercase + string.digits + "_-")
    if any(ch not in allowed for ch in normalized):
        raise ValueError("Kode paket hanya boleh huruf, angka, underscore, dan strip.")
    return normalized


def default_package(config, store):
    vip_chat_id = runtime_vip_chat_id(config, store)
    return {
        "code": "default",
        "name": "VIP",
        "amount": config.payment_amount,
        "vip_chat_id": vip_chat_id,
        "invite_expire_hours": config.invite_expire_hours,
    }


def package_label(package):
    return f"{package['name']} - {format_button_amount(package['amount'])}"


def package_log_line(package):
    if not package:
        return "Package: <code>custom</code>"
    return (
        f"Package: <code>{html.escape(package.get('code') or '')}</code> "
        f"{html.escape(package.get('name') or '')} "
        f"(<code>{package.get('vip_chat_id') or ''}</code>)"
    )


def is_active_payment_duplicate(exc):
    return "idx_vip_payments_one_active_per_user" in str(exc)


def is_cloudflare_challenge(exc):
    text = str(exc)
    return "HTTP 403" in text and ("Just a moment" in text or "challenges.cloudflare.com" in text)


def is_user_blocked_error(exc):
    return isinstance(exc, errors.UserIsBlockedError) or "User is blocked" in str(exc)


def format_qris_expiry(raw_expires):
    if not raw_expires:
        return ""
    parsed = parse_iso_datetime(raw_expires)
    if not parsed:
        return raw_expires
    return parsed.astimezone(dt.timezone(dt.timedelta(hours=7))).strftime("%d/%m/%Y %H:%M WIB")


def user_link(row):
    name = html.escape(row["full_name"] or str(row["user_id"]))
    return f'<a href="tg://user?id={row["user_id"]}">{name}</a>'


def telegram_user_link(user):
    name = html.escape(display_name(user))
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def internal_telegram_chat_url(chat_id):
    raw = str(chat_id or "").strip()
    if not raw.startswith("-100"):
        return ""
    internal_id = raw[4:]
    if not internal_id.isdigit():
        return ""
    return f"https://t.me/c/{internal_id}"


def is_admin(config, user_id):
    return user_id in config.admin_user_ids


def runtime_vip_chat_id(config, store):
    return store.get_int_setting("vip_chat_id", config.vip_chat_id)


def runtime_log_chat_id(config, store):
    return store.get_int_setting("log_chat_id", config.log_chat_id)


async def send_log(client, config, store, text):
    try:
        log_chat_id = runtime_log_chat_id(config, store)
    except Exception as exc:
        LOGGER.warning("Failed to load runtime log_chat_id, falling back to env: %s", exc)
        log_chat_id = config.log_chat_id
    if not log_chat_id:
        return
    try:
        await client.send_message(log_chat_id, text, parse_mode="html", link_preview=False)
    except Exception as exc:
        LOGGER.warning("Failed to send log message to %s: %s", log_chat_id, exc)


async def safe_send_user(client, config, store, user_id, text, **kwargs):
    try:
        await client.send_message(user_id, text, **kwargs)
        return "sent"
    except Exception as exc:
        if is_user_blocked_error(exc):
            LOGGER.warning("User %s blocked the bot, cannot deliver message", user_id)
            status = "blocked"
        else:
            LOGGER.exception("Failed to send message to user %s", user_id)
            status = "error"
        await send_log(
            client,
            config,
            store,
            (
                "<b>User delivery error</b>\n"
                f"User: <code>{user_id}</code>\n"
                f"Error: <code>{html.escape(str(exc))}</code>"
            ),
        )
        return status


def create_qris_sync(config, user, amount=None, note_prefix="VIP"):
    session = new_session(config.sociabuzz_cookie)
    buyer_name, buyer_email = random_indonesian_identity()
    checkout_amount = amount if amount is not None else config.payment_amount
    note = f"{note_prefix} {user.id}"
    order_id, payment_url, _ = create_donation_order(
        session,
        config.sociabuzz_username,
        checkout_amount,
        buyer_name,
        buyer_email,
        note,
    )
    qris = create_qris(session, order_id, payment_url, checkout_amount, source_payment="xendit")
    qr_response = download_qr_response(session, qris)
    return (
        session,
        buyer_name,
        buyer_email,
        order_id,
        payment_url,
        qris,
        qr_response.content,
        checkout_amount,
    )


def check_payment_sync(config, inv_id):
    session = new_session(config.sociabuzz_cookie)
    return check_pending(session, inv_id)


async def create_invite_link(client, config, store, payment):
    vip_chat_id = int(payment.get("vip_chat_id") or 0) or runtime_vip_chat_id(config, store)
    if not vip_chat_id:
        raise RuntimeError("VIP chat belum di-set. Admin perlu set paket atau pakai /setvip <chat_id>.")
    invite_hours = int(payment.get("invite_expire_hours") or 0) or config.invite_expire_hours
    expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(hours=invite_hours)
    title_name = payment.get("package_name") or "VIP"
    result = await client(
        functions.messages.ExportChatInviteRequest(
            peer=vip_chat_id,
            expire_date=expires_at,
            usage_limit=1,
            title=f"{title_name} {payment['inv_id']}",
        )
    )
    return result.link, expires_at.replace(microsecond=0).isoformat()


def qris_caption(package, inv_id, checkout_amount, final_amount, expires):
    public_invoice = html.escape(inv_id)
    human_expires = html.escape(format_qris_expiry(expires))
    lines = [
        "🔥 <b>Akses VIP Premium</b>",
        "",
        f"Kode pesanan: <code>{public_invoice}</code>",
        f"Paket: <b>{html.escape(package['name'])}</b>",
        f"Nominal paket: <code>{format_rupiah(checkout_amount)}</code>",
    ]
    if final_amount:
        lines.append(f"Nominal QRIS: <code>{html.escape(final_amount)}</code>")
    if human_expires:
        lines.append(f"⏳ Batas bayar: <code>{human_expires}</code>")
    lines.extend(
        [
            "",
            "📌 <b>Aturan pembayaran</b>",
            "• Scan QRIS ini lalu bayar <b>sesuai nominal QRIS</b>.",
            "• Bayar <b>1 kali saja</b>, jangan diulang.",
            "• QRIS ini <b>unik khusus pesanan kamu</b>.",
            "• Status dicek otomatis, tidak perlu kirim bukti transfer.",
            "",
            "Setelah pembayaran terdeteksi, link VIP akan langsung dikirim otomatis.",
        ]
    )
    return "\n".join(lines)


def custom_qris_caption(inv_id, checkout_amount, final_amount, expires, user):
    public_invoice = html.escape(inv_id)
    human_expires = html.escape(format_qris_expiry(expires))
    lines = [
        "🧾 <b>Custom QRIS</b>",
        "",
        f"Kode pesanan: <code>{public_invoice}</code>",
        f"Requester: {telegram_user_link(user)} (<code>{user.id}</code>)",
        f"Nominal custom: <code>{format_rupiah(checkout_amount)}</code>",
    ]
    if final_amount:
        lines.append(f"Nominal QRIS: <code>{html.escape(final_amount)}</code>")
    if human_expires:
        lines.append(f"⏳ Batas bayar: <code>{human_expires}</code>")
    lines.extend(
        [
            "",
            "📌 <b>Aturan pembayaran</b>",
            "• Bayar <b>sesuai nominal QRIS</b>.",
            "• Bayar <b>1 kali saja</b>, jangan diulang.",
            "• Status akan dicek otomatis.",
        ]
    )
    return "\n".join(lines)


def paid_message(invite_link, package_name="VIP", invite_hours=24):
    safe_package_name = html.escape(package_name)
    return (
        "✅ <b>Pembayaran berhasil terdeteksi</b>\n\n"
        f"Akses <b>{safe_package_name}</b> kamu sudah aktif.\n\n"
        "1️⃣ Join group lewat link ini dulu:\n"
        f"<code>{html.escape(invite_link)}</code>\n\n"
        "2️⃣ Setelah sudah join, buka group lagi lewat tombol di bawah.\n\n"
        f"⚠️ Link join hanya bisa dipakai <b>1 kali</b> dan berlaku <b>{int(invite_hours)} jam</b>."
    )


def paid_message_buttons(payment):
    url = internal_telegram_chat_url(payment.get("vip_chat_id"))
    if not url:
        return None
    package_name = (payment.get("package_name") or "VIP").strip() or "VIP"
    return [[Button.url(f"Buka {package_name}", url)]]


def invalid_payment_message():
    return (
        "⚠️ <b>QRIS sebelumnya sudah tidak aktif</b>\n\n"
        "Slot VIP kamu masih bisa diamankan. Buat QRIS baru sekarang, selesaikan pembayaran 1 kali, "
        "dan link member VIP akan dikirim otomatis setelah pembayaran terdeteksi."
    )


def timeout_payment_message():
    return (
        "⏳ <b>Invoice VIP sudah kedaluwarsa</b>\n\n"
        "QRIS lama sudah ditutup supaya tidak salah scan. Klik tombol di bawah untuk checkout ulang "
        "dan lanjut masuk ke group member VIP."
    )


def package_buttons(config, store):
    try:
        packages = store.list_packages()
    except Exception as exc:
        LOGGER.warning("Failed to load package buttons, using default package: %s", exc)
        packages = []
    if not packages:
        return [[Button.inline(package_label(default_package(config, store)), b"buy_vip")]]
    rows = []
    for package in packages:
        data = f"buy_pkg:{normalize_package_code(package['code'])}".encode()
        rows.append([Button.inline(package_label(package), data)])
    return rows


async def send_qris(event, config, store, qris_semaphore, user_locks, package=None, invoice_message=None):
    user = await event.get_sender()
    lock = user_locks.setdefault(user.id, asyncio.Lock())
    if lock.locked():
        await event.respond("QRIS kamu sedang dibuat. Tunggu beberapa detik, jangan klik berulang.")
        return
    async with lock:
        await send_qris_locked(event, config, store, qris_semaphore, user, package, invoice_message)


async def send_qris_locked(event, config, store, qris_semaphore, user, package=None, invoice_message=None):
    package = package or default_package(config, store)
    pending = store.latest_pending_for_user(user.id)
    if pending:
        await event.respond(
            "Masih ada pembayaran yang sedang dicek. Tunggu statusnya selesai dulu sebelum membuat QRIS baru."
        )
        return

    if invoice_message is None:
        invoice_message = await event.respond("⏳ Membuat QRIS...")
    else:
        try:
            await event.client.edit_message(event.chat_id, invoice_message.id, "⏳ Membuat QRIS...")
        except errors.MessageNotModifiedError:
            pass
    try:
        try:
            await asyncio.to_thread(store.ensure_payment_schema_ready)
        except Exception as exc:
            LOGGER.exception("Payment schema is not ready")
            await invoice_message.edit(
                "Bot sedang maintenance database. Admin perlu jalankan ulang `supabase_schema.sql`, lalu coba lagi.",
            )
            await send_log(
                event.client,
                config,
                store,
                (
                    "<b>Database schema not ready</b>\n"
                    "Action: <code>Run supabase_schema.sql in Supabase SQL Editor</code>\n"
                    f"Error: <code>{html.escape(str(exc))}</code>"
                ),
            )
            return
        async with qris_semaphore:
            (
                _session,
                buyer_name,
                buyer_email,
                order_id,
                payment_url,
                qris,
                qr_bytes,
                checkout_amount,
            ) = await asyncio.to_thread(create_qris_sync, config, user, int(package["amount"]), package["code"].upper())
        socia_invoice_id = qris.get("inv_id")
        if not socia_invoice_id:
            raise SociaBuzzError(f"QRIS response missing inv_id: {qris}")

        buyer_invoice_id = public_invoice_id()
        qr_file = io.BytesIO(qr_bytes)
        qr_file.name = f"{buyer_invoice_id}.png"
        payload = qris.get("data", {})
        invoice_message = await event.client.edit_message(
            event.chat_id,
            invoice_message.id,
            qris_caption(
                package,
                buyer_invoice_id,
                checkout_amount,
                payload.get("amount") or "",
                payload.get("countdown") or "",
            ),
            file=qr_file,
            parse_mode="html",
        )
        store.create_payment(
            user,
            buyer_invoice_id,
            order_id,
            payment_url,
            socia_invoice_id,
            checkout_amount,
            buyer_name,
            buyer_email,
            qris,
            event.chat_id,
            invoice_message.id,
            package=package,
        )
        await send_log(
            event.client,
            config,
            store,
            (
                "<b>QRIS created</b>\n"
                f"User: {telegram_user_link(user)} (<code>{user.id}</code>)\n"
                f"{package_log_line(package)}\n"
                f"Invoice: <code>{html.escape(buyer_invoice_id)}</code>\n"
                f"Internal invoice: <code>{html.escape(socia_invoice_id)}</code>\n"
                f"Source payment: <code>{html.escape(qris.get('source_payment') or '')}</code>\n"
                f"Order: <code>{html.escape(order_id)}</code>\n"
                f"Checkout amount: <code>{checkout_amount}</code>\n"
                f"QRIS amount: <code>{html.escape(payload.get('amount') or '')}</code>"
            ),
        )
    except Exception as exc:
        if is_cloudflare_challenge(exc):
            LOGGER.warning("SociaBuzz Cloudflare challenge while creating QRIS for user %s", user.id)
        else:
            LOGGER.exception("Failed to create QRIS")
        try:
            await invoice_message.delete()
        except Exception:
            LOGGER.warning("Failed to delete invoice message after create error", exc_info=True)
        if is_cloudflare_challenge(exc):
            await event.respond("QRIS belum bisa dibuat karena gateway pembayaran sedang membatasi request. Coba lagi beberapa menit lagi.")
            await send_log(
                event.client,
                config,
                store,
                (
                    "<b>QRIS gateway blocked</b>\n"
                    f"User: {telegram_user_link(user)} (<code>{user.id}</code>)\n"
                    "Reason: <code>SociaBuzz Cloudflare HTTP 403</code>"
                ),
            )
            return
        if is_active_payment_duplicate(exc):
            await event.respond("Masih ada pembayaran yang sedang dicek. Tunggu statusnya selesai dulu sebelum membuat QRIS baru.")
            await send_log(
                event.client,
                config,
                store,
                (
                    "<b>Duplicate active payment blocked</b>\n"
                    f"User: {telegram_user_link(user)} (<code>{user.id}</code>)"
                ),
            )
            return
        await event.respond("Gagal membuat QRIS. Coba lagi beberapa saat lagi.")
        await send_log(event.client, config, store, f"<b>QRIS error</b>\n<code>{html.escape(str(exc))}</code>")


async def send_custom_qris(event, config, store, qris_semaphore, user_locks, amount):
    user = await event.get_sender()
    lock = user_locks.setdefault(user.id, asyncio.Lock())
    if lock.locked():
        await event.respond("Custom QRIS admin ini sedang dibuat. Tunggu beberapa detik.")
        return
    async with lock:
        await send_custom_qris_locked(event, config, store, qris_semaphore, user, amount)


async def send_custom_qris_locked(event, config, store, qris_semaphore, user, amount):
    pending = store.latest_pending_for_user(user.id)
    if pending:
        await event.respond(
            "Masih ada pembayaran yang sedang dicek untuk admin ini. Tunggu selesai dulu sebelum membuat custom QRIS baru."
        )
        return
    invoice_message = await event.respond(f"⏳ Membuat custom QRIS {format_rupiah(amount)}...")
    try:
        try:
            await asyncio.to_thread(store.ensure_payment_schema_ready)
        except Exception as exc:
            LOGGER.exception("Payment schema is not ready")
            await invoice_message.edit(
                "Bot sedang maintenance database. Admin perlu jalankan ulang `supabase_schema.sql`, lalu coba lagi.",
            )
            await send_log(
                event.client,
                config,
                store,
                (
                    "<b>Database schema not ready</b>\n"
                    "Action: <code>Run supabase_schema.sql in Supabase SQL Editor</code>\n"
                    f"Error: <code>{html.escape(str(exc))}</code>"
                ),
            )
            return
        async with qris_semaphore:
            (
                _session,
                buyer_name,
                buyer_email,
                order_id,
                payment_url,
                qris,
                qr_bytes,
                checkout_amount,
            ) = await asyncio.to_thread(create_qris_sync, config, user, amount, "CUSTOM")
        socia_invoice_id = qris.get("inv_id")
        if not socia_invoice_id:
            raise SociaBuzzError(f"QRIS response missing inv_id: {qris}")

        buyer_invoice_id = public_invoice_id()
        qr_file = io.BytesIO(qr_bytes)
        qr_file.name = f"{buyer_invoice_id}.png"
        payload = qris.get("data", {})
        invoice_message = await event.client.edit_message(
            event.chat_id,
            invoice_message.id,
            custom_qris_caption(
                buyer_invoice_id,
                checkout_amount,
                payload.get("amount") or "",
                payload.get("countdown") or "",
                user,
            ),
            file=qr_file,
            parse_mode="html",
        )
        store.create_payment(
            user,
            buyer_invoice_id,
            order_id,
            payment_url,
            socia_invoice_id,
            checkout_amount,
            buyer_name,
            buyer_email,
            qris,
            event.chat_id,
            invoice_message.id,
        )
        await send_log(
            event.client,
            config,
            store,
            (
                "<b>Custom QRIS created</b>\n"
                f"User: {telegram_user_link(user)} (<code>{user.id}</code>)\n"
                f"Invoice: <code>{html.escape(buyer_invoice_id)}</code>\n"
                f"Internal invoice: <code>{html.escape(socia_invoice_id)}</code>\n"
                f"Source payment: <code>{html.escape(qris.get('source_payment') or '')}</code>\n"
                f"Order: <code>{html.escape(order_id)}</code>\n"
                f"Custom amount: <code>{checkout_amount}</code>\n"
                f"QRIS amount: <code>{html.escape(payload.get('amount') or '')}</code>"
            ),
        )
    except Exception as exc:
        if is_cloudflare_challenge(exc):
            LOGGER.warning("SociaBuzz Cloudflare challenge while creating custom QRIS for user %s", user.id)
        else:
            LOGGER.exception("Failed to create custom QRIS")
        try:
            await invoice_message.delete()
        except Exception:
            LOGGER.warning("Failed to delete custom invoice message after create error", exc_info=True)
        if is_cloudflare_challenge(exc):
            await event.respond("Custom QRIS belum bisa dibuat karena gateway pembayaran sedang membatasi request. Coba lagi beberapa menit lagi.")
            await send_log(
                event.client,
                config,
                store,
                (
                    "<b>Custom QRIS gateway blocked</b>\n"
                    f"User: {telegram_user_link(user)} (<code>{user.id}</code>)\n"
                    "Reason: <code>SociaBuzz Cloudflare HTTP 403</code>"
                ),
            )
            return
        if is_active_payment_duplicate(exc):
            await event.respond("Masih ada pembayaran yang sedang dicek untuk admin ini. Tunggu selesai dulu sebelum membuat custom QRIS baru.")
            await send_log(
                event.client,
                config,
                store,
                (
                    "<b>Duplicate custom active payment blocked</b>\n"
                    f"User: {telegram_user_link(user)} (<code>{user.id}</code>)"
                ),
            )
            return
        await event.respond("Gagal membuat custom QRIS. Coba lagi beberapa saat lagi.")
        await send_log(event.client, config, store, f"<b>Custom QRIS error</b>\n<code>{html.escape(str(exc))}</code>")


async def process_paid_payment(client, config, store, payment):
    if payment["status"] == "delivery_error":
        if not store.claim_delivery_processing(payment["inv_id"]):
            return
        invite_link = payment.get("invite_link") or ""
        invite_expires_at = payment.get("invite_expires_at") or ""
        if not invite_link:
            store.mark_delivery_error(payment["inv_id"], "Missing invite_link for delivery retry")
            await send_log(
                client,
                config,
                store,
                (
                    "<b>Delivery retry error</b>\n"
                    f"Invoice: <code>{html.escape(payment.get('public_invoice_id') or payment['inv_id'])}</code>\n"
                    "Error: <code>Missing invite_link for delivery retry</code>"
                ),
            )
            return
    else:
        if not store.claim_paid_processing(payment["inv_id"]):
            return
        try:
            invite_link, invite_expires_at = await create_invite_link(client, config, store, payment)
        except Exception as exc:
            LOGGER.exception("Failed to create invite link for %s", payment["inv_id"])
            store.mark_invite_error(payment["inv_id"], str(exc))
            await send_log(
                client,
                config,
                store,
                (
                    "<b>Invite creation error</b>\n"
                    f"Invoice: <code>{html.escape(payment.get('public_invoice_id') or payment['inv_id'])}</code>\n"
                    f"Internal invoice: <code>{html.escape(payment['inv_id'])}</code>\n"
                    f"Error: <code>{html.escape(str(exc))}</code>"
                ),
            )
            return
        if not store.mark_delivery_processing(payment["inv_id"], invite_link, invite_expires_at):
            return

    await delete_qris_message(client, payment)
    delivery_status = await safe_send_user(
        client,
        config,
        store,
        payment["user_id"],
        paid_message(
            invite_link,
            payment.get("package_name") or "VIP",
            int(payment.get("invite_expire_hours") or 0) or config.invite_expire_hours,
        ),
        parse_mode="html",
        link_preview=False,
        buttons=paid_message_buttons(payment),
    )
    if delivery_status != "sent":
        if delivery_status == "blocked":
            store.mark_delivery_blocked(payment["inv_id"], "User blocked the bot")
            await send_log(
                client,
                config,
                store,
                (
                    "<b>Invite delivery blocked</b>\n"
                    f"User: {user_link(payment)} (<code>{payment['user_id']}</code>)\n"
                    f"Invoice: <code>{html.escape(payment.get('public_invoice_id') or payment['inv_id'])}</code>\n"
                    "Status: <code>User blocked the bot, delivery will not be retried</code>"
                ),
            )
        else:
            store.mark_delivery_error(payment["inv_id"], "Failed to send invite link to user")
        return

    changed = store.mark_paid(payment["inv_id"], invite_link, invite_expires_at)
    if not changed:
        return

    await send_log(
        client,
        config,
        store,
        (
            "<b>Payment paid</b>\n"
            f"User: {user_link(payment)} (<code>{payment['user_id']}</code>)\n"
            f"Package: <code>{html.escape(payment.get('package_code') or '')}</code> {html.escape(payment.get('package_name') or '')}\n"
            f"Invoice: <code>{html.escape(payment.get('public_invoice_id') or payment['inv_id'])}</code>\n"
            f"Internal invoice: <code>{html.escape(payment['inv_id'])}</code>\n"
            f"Invite link: <code>{html.escape(invite_link)}</code>\n"
            f"Invite expires: <code>{html.escape(invite_expires_at)}</code>"
        ),
    )


async def delete_qris_message(client, payment):
    chat_id = payment.get("qris_chat_id")
    message_id = payment.get("qris_message_id")
    if not chat_id or not message_id:
        return
    try:
        await client.delete_messages(int(chat_id), [int(message_id)], revoke=True)
    except Exception as exc:
        LOGGER.warning(
            "Failed to delete QRIS message %s in chat %s for invoice %s: %s",
            message_id,
            chat_id,
            payment.get("public_invoice_id") or payment.get("inv_id"),
            exc,
        )


async def poll_once(client, config, store, payment):
    try:
        if payment["status"] in {"invite_error", "delivery_error"}:
            await process_paid_payment(client, config, store, payment)
            return

        status, status_url, elapsed_ms = await asyncio.to_thread(check_payment_sync, config, payment["inv_id"])
        LOGGER.info("Invoice %s status=%s latency=%sms", payment["inv_id"], status, elapsed_ms)
        if status == "paid":
            await process_paid_payment(client, config, store, payment)
        elif status in {"failed_or_expired", "unknown"}:
            changed = store.mark_status_if_current(payment["inv_id"], "pending", status)
            if not changed:
                return
            log_status = "expired" if status == "unknown" else status
            await delete_qris_message(client, payment)
            await safe_send_user(
                client,
                config,
                store,
                payment["user_id"],
                invalid_payment_message(),
                parse_mode="html",
                buttons=package_buttons(config, store),
            )
            await send_log(
                client,
                config,
                store,
                (
                    f"<b>Payment {html.escape(log_status)}</b>\n"
                    f"User: {user_link(payment)} (<code>{payment['user_id']}</code>)\n"
                    f"Invoice: <code>{html.escape(payment.get('public_invoice_id') or payment['inv_id'])}</code>\n"
                    f"Internal invoice: <code>{html.escape(payment['inv_id'])}</code>\n"
                    f"Package: <code>{html.escape(payment.get('package_code') or '')}</code> {html.escape(payment.get('package_name') or '')}\n"
                    f"Check: {html.escape(status_url)}"
                ),
            )
        else:
            created_at = parse_iso_datetime(payment.get("created_at")) or dt.datetime.now(dt.UTC)
            expires_at = parse_iso_datetime(payment.get("qris_expires"))
            attempts = int(payment.get("poll_attempts") or 0) + 1
            store.record_poll_result(payment, next_poll_at(created_at, expires_at, attempts=attempts, error="") or utc_now_iso())
    except Exception as exc:
        if is_cloudflare_challenge(exc):
            marker = "SociaBuzz Cloudflare HTTP 403"
            previous_error = payment.get("error") or ""
            if marker not in previous_error:
                LOGGER.warning("SociaBuzz Cloudflare challenge while polling %s", payment["inv_id"])
            if payment["status"] == "pending":
                created_at = parse_iso_datetime(payment.get("created_at")) or dt.datetime.now(dt.UTC)
                expires_at = parse_iso_datetime(payment.get("qris_expires"))
                attempts = int(payment.get("poll_attempts") or 0) + 1
                store.record_poll_result(
                    payment,
                    next_poll_at(created_at, expires_at, attempts=attempts, error=marker) or utc_now_iso(),
                    error=marker,
                )
            if marker not in previous_error:
                await send_log(
                    client,
                    config,
                    store,
                    (
                        "<b>Polling gateway blocked</b>\n"
                        f"Invoice: <code>{html.escape(payment.get('public_invoice_id') or payment['inv_id'])}</code>\n"
                        f"Internal invoice: <code>{html.escape(payment['inv_id'])}</code>\n"
                        "Reason: <code>SociaBuzz Cloudflare HTTP 403</code>"
                    ),
            )
            return
        LOGGER.exception("Polling failed for %s", payment["inv_id"])
        if payment["status"] == "pending":
            created_at = parse_iso_datetime(payment.get("created_at")) or dt.datetime.now(dt.UTC)
            expires_at = parse_iso_datetime(payment.get("qris_expires"))
            attempts = int(payment.get("poll_attempts") or 0) + 1
            store.record_poll_result(
                payment,
                next_poll_at(created_at, expires_at, attempts=attempts, error=str(exc)) or utc_now_iso(),
                error=str(exc),
            )
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


async def expire_pending_payment(client, config, store, payment, title="Payment expired"):
    changed = store.mark_status_if_current(payment["inv_id"], "pending", "timeout")
    if not changed:
        return
    await delete_qris_message(client, payment)
    await safe_send_user(
        client,
        config,
        store,
        payment["user_id"],
        timeout_payment_message(),
        parse_mode="html",
        buttons=package_buttons(config, store),
    )
    await send_log(
        client,
        config,
        store,
        (
            f"<b>{html.escape(title)}</b>\n"
            f"User: {user_link(payment)} (<code>{payment['user_id']}</code>)\n"
            f"Package: <code>{html.escape(payment.get('package_code') or '')}</code> {html.escape(payment.get('package_name') or '')}\n"
            f"Invoice: <code>{html.escape(payment.get('public_invoice_id') or payment['inv_id'])}</code>\n"
            f"Internal invoice: <code>{html.escape(payment['inv_id'])}</code>"
        ),
    )


async def polling_loop(client, config, store):
    while True:
        try:
            store.recover_stale_processing()
            pending = store.retryable_payments(utc_now_iso(), config.poll_batch_size)
            for payment in pending:
                expires_at = parse_iso_datetime(payment.get("qris_expires"))
                if payment["status"] == "pending" and expires_at and expires_at <= dt.datetime.now(dt.UTC):
                    await expire_pending_payment(client, config, store, payment)
                    continue
                count = int(payment.get("poll_attempts") or 0)
                if payment["status"] == "pending" and count >= config.poll_max_attempts:
                    await expire_pending_payment(client, config, store, payment, title="Payment timeout")
                    continue
                await poll_once(client, config, store, payment)
        except Exception as exc:
            LOGGER.exception("Polling loop error")
            await send_log(
                client,
                config,
                store,
                f"<b>Polling loop error</b>\n<code>{html.escape(str(exc))}</code>",
            )
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


async def require_admin(event, config, store):
    if is_admin(config, event.sender_id):
        return True
    await send_log(
        event.client,
        config,
        store,
        (
            "<b>Unauthorized admin command</b>\n"
            f"User: <code>{event.sender_id}</code>\n"
            f"Chat: <code>{event.chat_id}</code>\n"
            f"Command: <code>{html.escape(event.raw_text or '')}</code>"
        ),
    )
    await event.respond("Command ini khusus admin.")
    return False


async def require_log_chat(event, config, store):
    try:
        log_chat_id = runtime_log_chat_id(config, store)
    except Exception as exc:
        LOGGER.warning("Failed to load runtime log_chat_id for command guard: %s", exc)
        log_chat_id = config.log_chat_id
    if event.chat_id == log_chat_id and not event.is_private:
        return True
    await event.respond("Command ini cuma bisa dipakai di group/channel logging.")
    return False


async def send_package_menu(event, config, store, text=None):
    await event.respond(
        text
        or (
            "🔥 <b>VIP Premium sudah siap</b>\n\n"
            "Pilih group VIP yang mau kamu akses. Pembayaran pakai QRIS, dicek otomatis, "
            "dan link VIP dikirim langsung setelah berhasil."
        ),
        buttons=package_buttons(config, store),
        parse_mode="html",
    )


def parse_package_add_args(raw):
    raw = (raw or "").strip()
    if not raw or " " not in raw:
        raise ValueError("Format: /package_add kode Nama Group|-1001234567890|5000")
    code, rest = raw.split(None, 1)
    parts = [part.strip() for part in rest.split("|")]
    if len(parts) != 3:
        raise ValueError("Format: /package_add kode Nama Group|-1001234567890|5000")
    name, chat_id, amount = parts
    if not name:
        raise ValueError("Nama paket wajib diisi.")
    digits = "".join(ch for ch in amount if ch.isdigit())
    if not digits:
        raise ValueError("Nominal paket harus angka.")
    amount_value = int(digits)
    if amount_value < 1000:
        raise ValueError("Nominal paket minimal Rp1.000.")
    if amount_value > 10_000_000:
        raise ValueError("Nominal paket maksimal Rp10.000.000.")
    return normalize_package_code(code), name, int(chat_id), amount_value


def package_list_text(packages):
    if not packages:
        return "Belum ada paket aktif. Tambah dengan `/package_add kode Nama Group|-1001234567890|5000`."
    lines = ["<b>Paket aktif</b>"]
    for package in packages:
        lines.append(
            f"- <code>{html.escape(package['code'])}</code> "
            f"{html.escape(package['name'])} - <b>{format_button_amount(package['amount'])}</b> "
            f"chat <code>{package['vip_chat_id']}</code>"
        )
    return "\n".join(lines)


async def main():
    config = load_config()
    store = PaymentStore(config)
    client = TelegramClient("vip_bot", config.api_id, config.api_hash)
    qris_semaphore = asyncio.Semaphore(config.qris_create_concurrency)
    user_locks = {}

    @client.on(events.NewMessage(pattern=r"^/start$"))
    @private_only
    async def start(event):
        await send_package_menu(event, config, store)

    @client.on(events.NewMessage(pattern=r"^/buy$"))
    @private_only
    async def buy_command(event):
        await send_package_menu(event, config, store)

    @client.on(events.CallbackQuery(data=b"buy_vip"))
    async def buy_callback(event):
        if not event.is_private:
            await event.answer("Buka bot lewat private chat.", alert=True)
            return
        await event.answer("Membuat QRIS...")
        invoice_message = await event.get_message()
        await send_qris(event, config, store, qris_semaphore, user_locks, invoice_message=invoice_message)

    @client.on(events.CallbackQuery(pattern=rb"^buy_pkg:(.+)$"))
    async def buy_package_callback(event):
        if not event.is_private:
            await event.answer("Buka bot lewat private chat.", alert=True)
            return
        code = event.pattern_match.group(1).decode()
        package = store.get_package(code)
        if not package:
            await event.answer("Paket sudah tidak aktif. Ketik /start lagi.", alert=True)
            return
        await event.answer("Membuat QRIS...")
        invoice_message = await event.get_message()
        await send_qris(event, config, store, qris_semaphore, user_locks, package=package, invoice_message=invoice_message)

    @client.on(events.NewMessage(pattern=r"^/status$"))
    @private_only
    async def status(event):
        pending = store.latest_pending_for_user(event.sender_id)
        if pending:
            await event.respond(
                "⏳ <b>Pembayaran masih dicek otomatis</b>\n"
                f"Kode pesanan: <code>{html.escape(pending.get('public_invoice_id') or pending['inv_id'])}</code>",
                parse_mode="html",
            )
        else:
            await event.respond("Tidak ada pembayaran pending.")

    @client.on(events.NewMessage(pattern=r"^/chatid$"))
    async def chat_id(event):
        if not event.is_private and not await require_admin(event, config, store):
            return
        await event.respond(f"chat_id: `{event.chat_id}`")

    @client.on(events.NewMessage(pattern=r"^/custom(?:@\w+)?(?:\s+(.+))?$"))
    async def custom_payment(event):
        if not await require_admin(event, config, store):
            return
        if not await require_log_chat(event, config, store):
            return
        raw_amount = (event.pattern_match.group(1) or "").strip()
        if not raw_amount:
            await event.respond("Format: `/custom 50000`")
            return
        digits = "".join(ch for ch in raw_amount if ch.isdigit())
        if not digits:
            await event.respond("Nominal custom harus angka. Contoh: `/custom 50000`")
            return
        amount = int(digits)
        if amount < 1000:
            await event.respond("Nominal custom minimal Rp1.000.")
            return
        if amount > 10_000_000:
            await event.respond("Nominal custom maksimal Rp10.000.000.")
            return
        await send_custom_qris(event, config, store, qris_semaphore, user_locks, amount)

    @client.on(events.NewMessage(pattern=r"^/package_add(?:@\w+)?(?:\s+(.+))?$"))
    async def package_add(event):
        if not await require_admin(event, config, store):
            return
        if not await require_log_chat(event, config, store):
            return
        try:
            code, name, vip_chat_id, amount = parse_package_add_args(event.pattern_match.group(1) or "")
            store.upsert_package(code, name, vip_chat_id, amount, config.invite_expire_hours)
            await event.respond(
                f"Paket aktif: <code>{html.escape(code)}</code> {html.escape(name)} - <b>{format_button_amount(amount)}</b>",
                parse_mode="html",
            )
            await send_log(
                client,
                config,
                store,
                (
                    "<b>Package updated</b>\n"
                    f"Code: <code>{html.escape(code)}</code>\n"
                    f"Name: <code>{html.escape(name)}</code>\n"
                    f"VIP chat: <code>{vip_chat_id}</code>\n"
                    f"Amount: <code>{amount}</code>\n"
                    f"Admin: <code>{event.sender_id}</code>"
                ),
            )
        except Exception as exc:
            await event.respond(f"Gagal tambah paket: <code>{html.escape(str(exc))}</code>", parse_mode="html")

    @client.on(events.NewMessage(pattern=r"^/package_list(?:@\w+)?$"))
    async def package_list(event):
        if not await require_admin(event, config, store):
            return
        packages = store.list_packages()
        await event.respond(package_list_text(packages), parse_mode="html")

    @client.on(events.NewMessage(pattern=r"^/package_delete(?:@\w+)?(?:\s+(.+))?$"))
    async def package_delete(event):
        if not await require_admin(event, config, store):
            return
        if not await require_log_chat(event, config, store):
            return
        code = (event.pattern_match.group(1) or "").strip()
        if not code:
            await event.respond("Format: `/package_delete kode`")
            return
        try:
            normalized = normalize_package_code(code)
            changed = store.delete_package(normalized)
            if changed:
                await event.respond(f"Paket <code>{html.escape(normalized)}</code> dinonaktifkan.", parse_mode="html")
                await send_log(
                    client,
                    config,
                    store,
                    f"<b>Package disabled</b>\nCode: <code>{html.escape(normalized)}</code>\nAdmin: <code>{event.sender_id}</code>",
                )
            else:
                await event.respond("Paket tidak ditemukan atau sudah nonaktif.")
        except Exception as exc:
            await event.respond(f"Gagal hapus paket: <code>{html.escape(str(exc))}</code>", parse_mode="html")

    @client.on(events.NewMessage(pattern=r"^/setvip(?:\s+(.+))?$"))
    async def set_vip(event):
        if not await require_admin(event, config, store):
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
        if not await require_admin(event, config, store):
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
        if not await require_admin(event, config, store):
            return
        vip_chat_id = runtime_vip_chat_id(config, store)
        log_chat_id = runtime_log_chat_id(config, store)
        await event.respond(
            "Config aktif:\n"
            f"VIP_CHAT_ID: `{vip_chat_id or 'belum diset'}`\n"
            f"LOG_CHAT_ID: `{log_chat_id or 'belum diset'}`\n"
            f"PAYMENT_AMOUNT: `{config.payment_amount}`\n"
            f"SUPABASE_PACKAGE_TABLE: `{config.supabase_package_table}`\n"
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
