import asyncio
import io
import html
import logging
from telethon import events, Button, errors
from vip_bot.helpers import (
    create_qris_with_retries_sync,
    public_invoice_id,
    send_log,
    telegram_user_link,
    is_cloudflare_challenge,
    is_sociabuzz_timeout,
    is_active_payment_duplicate,
    format_button_amount,
    format_rupiah,
    parse_referral_payload,
    format_referral_code,
    display_name,
    safe_send_user,
    valid_withdrawal_amount,
    parse_withdrawal_amount,
    withdrawal_details_text,
    normalize_package_code,
)
from vip_bot.messages import (
    qris_caption,
    default_package,
    package_buttons,
    main_menu_keyboard_text,
    main_menu_buttons,
    main_menu_button_labels,
)
from sociabuzz_client import SociaBuzzError

LOGGER = logging.getLogger("telegram_vip_bot.handlers.user")


def private_only(handler):
    async def wrapped(event):
        if not event.is_private:
            return
        await handler(event)
    return wrapped


async def send_qris(event, config, store, qris_semaphore, user_locks, package=None, invoice_message=None):
    user = await event.get_sender()
    lock = user_locks.setdefault(user.id, asyncio.Lock())
    if lock.locked():
        await event.respond("QRIS kamu sedang dibuat. Tunggu beberapa detik, jangan klik berulang.")
        return
    async with lock:
        await send_qris_locked(event, config, store, qris_semaphore, user, package, invoice_message)


async def send_qris_locked(event, config, store, qris_semaphore, user, package=None, invoice_message=None):
    store.upsert_user(user)
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
            ) = await asyncio.to_thread(create_qris_with_retries_sync, config, user, int(package["amount"]), package["code"].upper())
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
        referral = store.pending_referral_for_user(user.id)
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
            referral=referral,
        )
        await send_log(
            event.client,
            config,
            store,
            (
                "<b>QRIS CREATED</b>\n\n"
                "<blockquote>"
                f"<b>User</b>: {telegram_user_link(user)} (<code>{user.id}</code>)\n"
                f"<b>Package</b>: {html.escape(package.get('code') or '')} {html.escape(package.get('name') or '')} (<code>{package.get('vip_chat_id') or ''}</code>)\n"
                f"<b>Package Amount</b>: {format_button_amount(checkout_amount)}\n"
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
            LOGGER.warning("SociaBuzz Cloudflare challenge while creating QRIS for user %s", user.id)
        elif is_sociabuzz_timeout(exc):
            LOGGER.warning("SociaBuzz timed out while creating QRIS for user %s after retries: %s", user.id, exc)
        else:
            LOGGER.exception("Failed to create QRIS")
        try:
            await invoice_message.delete()
        except Exception:
            LOGGER.warning("Failed to delete invoice message after create error", exc_info=True)
        if is_cloudflare_challenge(exc):
            await event.respond("QRIS belum bisa dibuat karena sistem sedang membatasi request. Coba lagi beberapa menit lagi.")
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
        if is_sociabuzz_timeout(exc):
            await event.respond("QRIS lagi lambat dibuat. Coba lagi sebentar lagi ya.")
            await send_log(
                event.client,
                config,
                store,
                (
                    "<b>QRIS gateway timeout</b>\n"
                    f"User: {telegram_user_link(user)} (<code>{user.id}</code>)\n"
                    f"Package: <code>{html.escape(package.get('code') or '')}</code> {html.escape(package.get('name') or '')}\n"
                    f"Error: <code>{html.escape(str(exc))}</code>"
                ),
            )
            return
        await event.respond("Gagal membuat QRIS. Coba lagi beberapa saat lagi.")
        await send_log(event.client, config, store, f"<b>QRIS error</b>\n<code>{html.escape(str(exc))}</code>")


async def send_package_menu(event, config, store, text=None, buttons=None):
    await event.respond(
        text
        or (
            "🔥 <b>VIP Premium sudah siap</b>\n\n"
            "Pilih group VIP yang mau kamu akses. Pembayaran pakai QRIS, dicek otomatis, "
            "dan link VIP dikirim langsung setelah berhasil."
        ),
        buttons=buttons or package_buttons(config, store),
        parse_mode="html",
    )


async def handle_referral_start(event, config, store, payload):
    code = parse_referral_payload(payload)
    if not code:
        return
    user = await event.get_sender()
    if code == format_referral_code(user.id):
        return
    try:
        referrer = store.get_user_by_referral_code(code)
        if not referrer:
            return
        referral, created = store.create_referral_if_absent(referrer, user)
        if not created:
            return
        await safe_send_user(
            event.client,
            config,
            store,
            referrer["user_id"],
            f"✅ {html.escape(display_name(user))} berhasil diundang menggunakan referral link kamu.",
            parse_mode="html",
        )
        invited_row = {
            "user_id": user.id,
            "username": user.username or "",
            "full_name": display_name(user),
        }
        from vip_bot.helpers import plain_user_link
        await send_log(
            event.client,
            config,
            store,
            (
                f"User {plain_user_link(invited_row)} joined using Referral User {plain_user_link(referrer)}\n\n"
                f"Referral Code: {html.escape(code)}"
            ),
        )
    except Exception as exc:
        LOGGER.exception("Failed to process referral start")
        await send_log(event.client, config, store, f"<b>Referral start error</b>\n<code>{html.escape(str(exc))}</code>")


async def send_profile(event, config, store):
    try:
        user = await event.get_sender()
        store.upsert_user(user)
        stats = store.referral_stats(user.id)
        me = await event.client.get_me()
        code = stats["referral_code"]
        link = f"https://t.me/{me.username}?start=ref_{code}" if me.username else f"ref_{code}"
        detail_lines = [
            f"<b>User ID</b>: <code>{user.id}</code>",
        ]
        if user.username:
            detail_lines.append(f"<b>Username</b>: @{html.escape(user.username)}")
        detail_lines.extend(
            [
                f"<b>Saldo</b>: <b>{format_rupiah(stats['balance'])}</b>",
                f"<b>Referral link</b>: {html.escape(link)}",
                f"<b>Referral Berhasil</b>: <b>{stats['successful_count']}</b>",
                f"<b>Pending Referral</b>: <b>{stats['pending_count']}</b>",
            ]
        )
        detail_lines_str = "\n".join(detail_lines)
        lines = [
            "<b>Profile</b>",
            "Dapatkan komisi sebesar <b>50%</b> dari setiap pembelian paket VIP melalui referral link kamu.",
            "",
            f"<blockquote>{detail_lines_str}</blockquote>",
        ]
        await event.respond("\n".join(lines), parse_mode="html", buttons=main_menu_buttons())
    except Exception as exc:
        LOGGER.exception("Failed to show profile")
        await event.respond("Profile belum bisa ditampilkan. Coba lagi beberapa saat lagi.", buttons=main_menu_buttons())
        await send_log(event.client, config, store, f"<b>Profile error</b>\nUser: <code>{event.sender_id}</code>\n<code>{html.escape(str(exc))}</code>")


async def send_withdrawal_menu(event, config, store):
    try:
        user = await event.get_sender()
        store.upsert_user(user)
        stats = store.referral_stats(event.sender_id)
        await event.respond(
            f"Saldo kamu: <b>{format_rupiah(stats['balance'])}</b>\n\nKlik tombol di bawah untuk tarik saldo.",
            parse_mode="html",
            buttons=[[Button.inline("Tarik Saldo", b"withdraw_start")]],
        )
    except Exception as exc:
        LOGGER.exception("Failed to show withdrawal menu")
        await event.respond("Menu tarik saldo belum bisa ditampilkan. Coba lagi beberapa saat lagi.")
        await send_log(event.client, config, store, f"<b>Withdrawal menu error</b>\nUser: <code>{event.sender_id}</code>\n<code>{html.escape(str(exc))}</code>")


async def create_withdrawal_request(event, config, store, user, amount, details):
    stats = store.referral_stats(user.id)
    if not valid_withdrawal_amount(amount, stats["balance"]):
        await event.respond("Minimal penarikan saldo adalah Rp10.000 dan saldo kamu harus mencukupi.")
        return
    withdrawal = store.create_withdrawal(user, amount, details)
    await send_log(
        event.client,
        config,
        store,
        (
            "<b>Withdrawal requested</b>\n"
            f"ID: <code>{withdrawal.get('id')}</code>\n"
            f"User: {telegram_user_link(user)} (<code>{user.id}</code>)\n"
            f"Amount: <code>{amount}</code>\n"
            f"No Hp: <code>{html.escape(details['phone'])}</code>\n"
            f"Nama E-Wallet: <code>{html.escape(details['wallet_name'])}</code>\n"
            f"Atas Nama: <code>{html.escape(details['account_name'])}</code>"
        ),
        buttons=[
            [
                Button.inline("Berhasil", f"withdraw_done:{withdrawal.get('id')}".encode()),
                Button.inline("Tolak", f"withdraw_reject:{withdrawal.get('id')}".encode()),
            ]
        ],
    )
    await event.respond("Pengajuan penarikan saldo berhasil. Mohon tunggu 1x24 jam untuk diproses oleh admin.", buttons=main_menu_buttons())


def register_user_handlers(client, config, store, qris_semaphore, user_locks, withdrawal_states):
    @client.on(events.NewMessage(pattern=r"^/start(?:\s+(.+))?$"))
    @private_only
    async def start(event):
        user = await event.get_sender()
        store.upsert_user(user)
        await handle_referral_start(event, config, store, event.pattern_match.group(1) or "")
        await event.respond(main_menu_keyboard_text(user), buttons=main_menu_buttons(), parse_mode="html")
        await send_package_menu(event, config, store)

    @client.on(events.NewMessage(pattern=r"^/buy$"))
    @private_only
    async def buy_command(event):
        await send_package_menu(event, config, store)

    @client.on(events.NewMessage(pattern=r"^🛒 Beli Group VIP$"))
    @private_only
    async def buy_button(event):
        withdrawal_states.pop(event.sender_id, None)
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

    @client.on(events.NewMessage(pattern=r"^👤 Profile$"))
    @private_only
    async def profile_button(event):
        withdrawal_states.pop(event.sender_id, None)
        await send_profile(event, config, store)

    @client.on(events.NewMessage(pattern=r"^💰 Tarik Saldo$"))
    @private_only
    async def withdrawal_button(event):
        withdrawal_states.pop(event.sender_id, None)
        await send_withdrawal_menu(event, config, store)

    @client.on(events.CallbackQuery(data=b"withdraw_start"))
    async def withdraw_start(event):
        if not event.is_private:
            await event.answer("Buka bot lewat private chat.", alert=True)
            return
        await event.answer()
        withdrawal_states[event.sender_id] = {"step": "amount"}
        await event.respond("Masukkan nominal yang mau ditarik. Minimal Rp10.000. Contoh: 50.000 atau 50000")

    @client.on(events.NewMessage)
    @private_only
    async def withdrawal_input(event):
        state = withdrawal_states.get(event.sender_id)
        if not state:
            return
        text = (event.raw_text or "").strip()
        if text in set(main_menu_button_labels()) or text.startswith("/"):
            withdrawal_states.pop(event.sender_id, None)
            return
        try:
            if state["step"] == "amount":
                amount = parse_withdrawal_amount(text)
                user = await event.get_sender()
                store.upsert_user(user)
                stats = store.referral_stats(event.sender_id)
                if not valid_withdrawal_amount(amount, stats["balance"]):
                    await event.respond("Minimal penarikan saldo adalah Rp10.000 dan saldo kamu harus mencukupi.")
                    return
                withdrawal_states[event.sender_id] = {"step": "details", "amount": amount}
                await event.respond("Kirim data tujuan dengan format:\nNo Hp: 08123456789\nNama E-Wallet: Dana\nAtas Nama: Nama Kamu")
                return
            details = withdrawal_details_text(text)
            user = await event.get_sender()
            await create_withdrawal_request(event, config, store, user, state["amount"], details)
            withdrawal_states.pop(event.sender_id, None)
        except ValueError:
            await event.respond("Format data belum lengkap. Gunakan:\nNo Hp: 08123456789\nNama E-Wallet: Dana\nAtas Nama: Nama Kamu")
        except Exception as exc:
            LOGGER.exception("Withdrawal input error")
            withdrawal_states.pop(event.sender_id, None)
            await event.respond("Pengajuan penarikan belum bisa diproses. Coba lagi beberapa saat lagi.", buttons=main_menu_buttons())
            await send_log(event.client, config, store, f"<b>Withdrawal input error</b>\nUser: <code>{event.sender_id}</code>\n<code>{html.escape(str(exc))}</code>")
