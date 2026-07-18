import asyncio
import io
import html
import logging
from telethon import events, Button, errors
from vip_bot.helpers import (
    is_admin,
    runtime_vip_chat_id,
    runtime_log_chat_id,
    send_log,
    parse_chat_setting,
    format_rupiah,
    create_qris_with_retries_sync,
    public_invoice_id,
    telegram_user_link,
    is_cloudflare_challenge,
    is_sociabuzz_timeout,
    is_active_payment_duplicate,
    normalize_package_code,
    format_button_amount,
    safe_send_user,
    entities_to_json,
)
from vip_bot.messages import (
    admin_command_list_text,
    custom_qris_caption,
    package_list_text,
)
from vip_bot.loops import send_broadcast_batch
from sociabuzz_client import SociaBuzzError

LOGGER = logging.getLogger("telegram_vip_bot.handlers.admin")


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


async def require_admin_logchat(event, config, store):
    if not is_admin(config, event.sender_id):
        return False
    try:
        log_chat_id = runtime_log_chat_id(config, store)
    except Exception as exc:
        LOGGER.warning("Failed to load runtime log_chat_id for command guard: %s", exc)
        log_chat_id = config.log_chat_id
    if event.chat_id == log_chat_id and not event.is_private:
        return True
    if event.is_private:
        return False
    await event.respond("Command ini cuma bisa dipakai di group/channel logging.")
    return False


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
            ) = await asyncio.to_thread(create_qris_with_retries_sync, config, user, amount, "CUSTOM")
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
                "<b>Custom QRIS CREATED</b>\n\n"
                "<blockquote>"
                f"<b>User</b>: {telegram_user_link(user)} (<code>{user.id}</code>)\n"
                f"<b>Custom QRIS Amount</b>: {format_rupiah(checkout_amount)}\n"
                f"<b>QRIS Amount</b>: {html.escape(payload.get('amount') or '')}\n"
                f"<b>Invoice</b>: <code>{html.escape(buyer_invoice_id)}</code>\n"
                f"<b>Internal Invoice</b>: <code>{html.escape(socia_invoice_id)}</code>\n"
                f"<b>Source Payment</b>: {html.escape(qris.get('source_payment') or '')}\n"
                f"<b>Order ID</b>: <code>{html.escape(order_id)}</code>"
                "</blockquote>"
            ),
        )
    except Exception as exc:
        if is_cloudflare_challenge(exc):
            LOGGER.warning("SociaBuzz Cloudflare challenge while creating custom QRIS for user %s", user.id)
        elif is_sociabuzz_timeout(exc):
            LOGGER.warning("SociaBuzz timed out while creating custom QRIS for user %s after retries: %s", user.id, exc)
        else:
            LOGGER.exception("Failed to create custom QRIS")
        try:
            await invoice_message.delete()
        except Exception:
            LOGGER.warning("Failed to delete custom invoice message after create error", exc_info=True)
        if is_cloudflare_challenge(exc):
            await event.respond("Custom QRIS belum bisa dibuat karena sistem sedang membatasi request. Coba lagi beberapa menit lagi.")
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
        if is_sociabuzz_timeout(exc):
            await event.respond("Custom QRIS lagi lambat dibuat. Coba lagi sebentar lagi ya.")
            await send_log(
                event.client,
                config,
                store,
                (
                    "<b>Custom QRIS gateway timeout</b>\n"
                    f"User: {telegram_user_link(user)} (<code>{user.id}</code>)\n"
                    f"Amount: <code>{format_rupiah(amount)}</code>\n"
                    f"Error: <code>{html.escape(str(exc))}</code>"
                ),
            )
            return
        await event.respond("Gagal membuat custom QRIS. Coba lagi beberapa saat lagi.")
        await send_log(event.client, config, store, f"<b>Custom QRIS error</b>\n<code>{html.escape(str(exc))}</code>")


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


def register_admin_handlers(client, config, store, qris_semaphore, user_locks):
    @client.on(events.CallbackQuery(pattern=rb"^withdraw_(done|reject):(\d+)$"))
    async def withdrawal_admin_action(event):
        if not is_admin(config, event.sender_id):
            await event.answer("Khusus admin.", alert=True)
            return
        action = event.pattern_match.group(1).decode()
        withdrawal_id = event.pattern_match.group(2).decode()
        status = "completed" if action == "done" else "rejected"
        label = "berhasil diproses" if action == "done" else "ditolak"
        try:
            withdrawal = store.update_withdrawal_status(withdrawal_id, "pending", status, event.sender_id)
            if not withdrawal:
                await event.answer("Pengajuan sudah diproses.", alert=True)
                return
            await event.answer(f"Withdrawal {label}.")
            await safe_send_user(
                event.client,
                config,
                store,
                withdrawal["user_id"],
                f"Pengajuan penarikan saldo {format_rupiah(withdrawal['amount'])} {label} oleh admin.",
            )
            message = await event.get_message()
            await event.edit(
                f"{message.raw_text}\n\nStatus: <b>{html.escape(status)}</b>\nAdmin: <code>{event.sender_id}</code>",
                parse_mode="html",
                buttons=None,
            )
        except Exception as exc:
            LOGGER.exception("Withdrawal admin action error")
            await event.answer("Gagal memproses withdrawal. Cek log.", alert=True)
            await send_log(event.client, config, store, f"<b>Withdrawal admin action error</b>\nID: <code>{html.escape(withdrawal_id)}</code>\n<code>{html.escape(str(exc))}</code>")

    @client.on(events.NewMessage(pattern=r"^/chatid(?:@\w+)?$"))
    async def chat_id(event):
        if not await require_admin_logchat(event, config, store):
            return
        await event.respond(f"chat_id: `{event.chat_id}`")

    @client.on(events.NewMessage(pattern=r"^/custom(?:@\w+)?(?:\s+(.+))?$"))
    async def custom_payment(event):
        if not await require_admin_logchat(event, config, store):
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
        if not await require_admin_logchat(event, config, store):
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
            LOGGER.exception("Failed to add package")
            await event.respond("Gagal tambah paket. Detail error dikirim ke log admin.")
            await send_log(client, config, store, f"<b>Package add error</b>\nAdmin: <code>{event.sender_id}</code>\n<code>{html.escape(str(exc))}</code>")

    @client.on(events.NewMessage(pattern=r"^/package_list(?:@\w+)?$"))
    async def package_list(event):
        if not await require_admin_logchat(event, config, store):
            return
        packages = store.list_packages()
        await event.respond(package_list_text(packages), parse_mode="html")

    @client.on(events.NewMessage(pattern=r"^/package_delete(?:@\w+)?(?:\s+(.+))?$"))
    async def package_delete(event):
        if not await require_admin_logchat(event, config, store):
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
            LOGGER.exception("Failed to delete package")
            await event.respond("Gagal hapus paket. Detail error dikirim ke log admin.")
            await send_log(client, config, store, f"<b>Package delete error</b>\nAdmin: <code>{event.sender_id}</code>\n<code>{html.escape(str(exc))}</code>")

    @client.on(events.NewMessage(pattern=r"^/setvip(?:@\w+)?(?:\s+(.+))?$"))
    async def set_vip(event):
        if not await require_admin_logchat(event, config, store):
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
            LOGGER.exception("Failed to set VIP chat")
            await event.respond("Gagal set VIP chat. Detail error dikirim ke log admin.")
            await send_log(client, config, store, f"<b>Set VIP chat error</b>\nAdmin: <code>{event.sender_id}</code>\n<code>{html.escape(str(exc))}</code>")

    @client.on(events.NewMessage(pattern=r"^/setlog(?:@\w+)?(?:\s+(.+))?$"))
    async def set_log(event):
        if not await require_admin_logchat(event, config, store):
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
            LOGGER.exception("Failed to set log chat")
            await event.respond("Gagal set log chat. Detail error dikirim ke log admin.")
            await send_log(client, config, store, f"<b>Set log chat error</b>\nAdmin: <code>{event.sender_id}</code>\n<code>{html.escape(str(exc))}</code>")

    @client.on(events.NewMessage(pattern=r"^/config(?:@\w+)?$"))
    async def show_config(event):
        if not await require_admin_logchat(event, config, store):
            return
        vip_chat_id = runtime_vip_chat_id(config, store)
        log_chat_id = runtime_log_chat_id(config, store)
        await event.respond(
            "Config aktif:\n"
            f"VIP_CHAT_ID: `{vip_chat_id or 'belum diset'}`\n"
            f"LOG_CHAT_ID: `{log_chat_id or 'belum diset'}`\n"
            f"PAYMENT_AMOUNT: `{config.payment_amount}`\n"
            f"SUPABASE_PACKAGE_TABLE: `{config.supabase_package_table}`\n"
            f"BROADCAST_BATCH_SIZE: `{config.broadcast_batch_size}`\n"
            f"INVITE_EXPIRE_HOURS: `{config.invite_expire_hours}`"
        )

    @client.on(events.NewMessage(pattern=r"^/commands?(?:@\w+)?$"))
    async def commands(event):
        if not await require_admin_logchat(event, config, store):
            return
        await event.respond(admin_command_list_text(), parse_mode="html")

    @client.on(events.NewMessage(pattern=r"^/set_broadcast(?:@\w+)?$"))
    async def set_broadcast(event):
        if not await require_admin_logchat(event, config, store):
            return
        replied = await event.get_reply_message()
        if not replied:
            await event.respond("Reply ke pesan yang mau dijadikan broadcast.")
            return
        text = replied.raw_text or ""
        media_file_id = getattr(getattr(replied, "file", None), "id", "") or ""
        media_type = getattr(getattr(replied, "file", None), "mime_type", "") or ""
        if not text and not media_file_id:
            await event.respond("Pesan harus memiliki teks atau media.")
            return
        saved = store.set_broadcast_message(text, media_file_id, media_type, entities_to_json(replied.entities or []))
        await event.respond("Broadcast berhasil disimpan. Gunakan /test_broadcast untuk uji coba.")
        await send_log(
            client,
            config,
            store,
            (
                "<b>Broadcast saved</b>\n"
                f"ID: <code>{saved.get('id', '')}</code>\n"
                f"Admin: <code>{event.sender_id}</code>\n"
                f"Media: <code>{html.escape(media_type or '-')}</code>"
            ),
        )

    @client.on(events.NewMessage(pattern=r"^/set_broadcasttime(?:@\w+)?(?:\s+(.+))?$"))
    async def set_broadcast_time(event):
        if not await require_admin_logchat(event, config, store):
            return
        raw_value = event.pattern_match.group(1) or ""
        from vip_bot.helpers import validate_broadcast_time
        try:
            time_value = validate_broadcast_time(raw_value)
        except ValueError:
            await event.respond("Format: `/set_broadcasttime HH:MM` contoh `/set_broadcasttime 09:00`, atau `/set_broadcasttime off`.")
            return
        store.set_broadcast_time(time_value)
        store.set_last_broadcast_date("")
        if not time_value:
            await event.respond("Broadcast otomatis dinonaktifkan.")
            return
        await event.respond(f"Broadcast otomatis dijadwalkan setiap <b>{html.escape(time_value)} WIB</b>.", parse_mode="html")

    @client.on(events.NewMessage(pattern=r"^/test_broadcast(?:@\w+)?$"))
    async def test_broadcast(event):
        if not await require_admin_logchat(event, config, store):
            return
        broadcast_message = store.get_active_broadcast_message()
        if not broadcast_message:
            await event.respond("Belum ada broadcast yang disimpan. Gunakan /set_broadcast dulu.")
            return
        admin_ids = sorted(config.admin_user_ids)
        if not admin_ids:
            await event.respond("ADMIN_USER_IDS belum diisi.")
            return
        totals = await send_broadcast_batch(client, store, broadcast_message, admin_ids, config.qris_create_concurrency)
        await event.respond(
            "Test broadcast selesai.\n"
            f"Terkirim: <code>{totals['sent']}</code>\n"
            f"Blocked: <code>{totals['blocked']}</code>\n"
            f"Deactivated: <code>{totals['deactivated']}</code>\n"
            f"Error: <code>{totals['error']}</code>",
            parse_mode="html",
        )
