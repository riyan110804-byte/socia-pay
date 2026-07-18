import asyncio
import datetime as dt
import html
import logging
from vip_bot.config import WIB, BROADCAST_DISABLED_VALUES
from vip_bot.helpers import (
    check_payment_sync,
    is_cloudflare_challenge,
    is_sociabuzz_timeout,
    parse_iso_datetime,
    next_poll_at,
    utc_now_iso,
    send_log,
    safe_send_user,
    delete_qris_message,
    create_invite_link,
    send_broadcast_to_user,
    user_link,
    plain_user_link,
    telegram_user_link,
    format_rupiah,
    referral_commission,
    internal_telegram_chat_url,
)
from vip_bot.messages import (
    invalid_payment_message,
    timeout_payment_message,
    package_buttons,
    paid_message,
)

LOGGER = logging.getLogger("telegram_vip_bot.loops")


async def credit_referral_if_needed(client, config, store, payment):
    referral_id = payment.get("referral_id")
    if not referral_id:
        referral = store.pending_referral_for_user(payment["user_id"])
        referral_id = referral.get("id") if referral else None
    if not referral_id:
        return
    commission = referral_commission(payment)
    if commission <= 0:
        return
    referral = store.mark_referral_paid(referral_id, payment, commission)
    if not referral:
        return
    await safe_send_user(
        client,
        config,
        store,
        referral["referrer_user_id"],
        (
            "✅ <b>Komisi referral masuk</b>\n\n"
            f"{html.escape(payment.get('full_name') or str(payment['user_id']))} sudah join member VIP.\n"
            f"Komisi kamu: <b>{format_rupiah(commission)}</b>"
        ),
        parse_mode="html",
    )
    referrer_row = store.get_user(referral["referrer_user_id"]) or {"user_id": referral["referrer_user_id"]}
    payment_row = {
        "user_id": payment["user_id"],
        "username": payment.get("username") or "",
        "full_name": payment.get("full_name") or str(payment["user_id"]),
    }
    await send_log(
        client,
        config,
        store,
        (
            "<b>REFERRAL COMISSION CREDITED</b>\n\n"
            "<blockquote>"
            "<b>INVITER</b>\n"
            f"<b>Name</b>: {html.escape(referrer_row.get('full_name') or str(referrer_row.get('user_id')))}\n"
            f"<b>Username</b>: {('@' + html.escape(referrer_row.get('username'))) if referrer_row.get('username') else '-'}\n"
            f"<b>User ID</b>: <code>{referrer_row.get('user_id')}</code>"
            "</blockquote>\n\n"
            "<blockquote>"
            "<b>USER INVITED</b>\n"
            f"<b>Name</b>: {html.escape(payment.get('full_name') or str(payment['user_id']))}\n"
            f"<b>User ID</b>: <code>{payment['user_id']}</code>"
            "</blockquote>\n\n"
            "<blockquote>"
            "<b>TRANSACTION</b>\n"
            f"<b>Invoice</b>: <code>{html.escape(payment.get('public_invoice_id') or payment['inv_id'])}</code>\n"
            f"<b>Package</b>: {html.escape(payment.get('package_code') or '')} {html.escape(payment.get('package_name') or '')}\n"
            f"<b>Package Amount</b>: {format_rupiah(int(payment.get('package_amount') or 0))}\n"
            f"<b>Commission</b>: {format_rupiah(commission)}\n\n"
            "Status: Success"
            "</blockquote>\n\n"
            f"User {plain_user_link(payment_row)} joined using Referral User {plain_user_link(referrer_row)}\n\n"
            f"Referral Code: {html.escape(referrer_row.get('referral_code') or '')}"
        ),
    )


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
            internal_telegram_chat_url(payment.get("vip_chat_id")),
        ),
        parse_mode="html",
        link_preview=False,
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

    await credit_referral_if_needed(client, config, store, payment)

    await send_log(
        client,
        config,
        store,
        (
            "<b>PAYMENT PAID</b>\n\n"
            "<blockquote>"
            f"<b>User</b>: {user_link(payment)} (<code>{payment['user_id']}</code>)\n"
            f"<b>Package</b>: {html.escape(payment.get('package_code') or '')} {html.escape(payment.get('package_name') or '')}\n"
            f"<b>Invoice</b>: <code>{html.escape(payment.get('public_invoice_id') or payment['inv_id'])}</code>\n"
            f"<b>Internal Invoice</b>: {html.escape(payment['inv_id'])}\n"
            f"<b>Invite Link</b>: {html.escape(invite_link)}\n"
            f"<b>Invite Link Expires</b>: {format_log_datetime_wrapper(invite_expires_at)}"
            "</blockquote>"
        ),
    )


def format_log_datetime_wrapper(raw):
    from vip_bot.helpers import format_log_datetime
    return format_log_datetime(raw)


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
    is_custom = not (payment.get("package_code") or payment.get("package_name"))
    if is_custom:
        log_text = (
            "<b>Custom QRIS Expired</b>\n\n"
            "<blockquote>"
            f"<b>User</b>: {user_link(payment)} (<code>{payment['user_id']}</code>)\n"
            f"<b>Custom QRIS Amount</b>: {format_rupiah(int(payment.get('amount') or 0))}\n"
            f"<b>Invoice</b>: <code>{html.escape(payment.get('public_invoice_id') or payment['inv_id'])}</code>\n"
            f"<b>Internal Invoice</b>: <code>{html.escape(payment['inv_id'])}</code>"
            "</blockquote>"
        )
    else:
        log_text = (
            "<b>PAYMENT EXPIRED</b>\n\n"
            "<blockquote>"
            f"<b>User</b>: {user_link(payment)} (<code>{payment['user_id']}</code>)\n"
            f"<b>Package</b>: {html.escape(payment.get('package_code') or '')} {html.escape(payment.get('package_name') or '')}\n"
            f"<b>Invoice</b>: <code>{html.escape(payment.get('public_invoice_id') or payment['inv_id'])}</code>\n"
            f"<b>Internal Invoice</b>: <code>{html.escape(payment['inv_id'])}</code>"
            "</blockquote>"
        )
    await send_log(client, config, store, log_text)


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


async def send_broadcast_batch(client, store, broadcast_message, user_ids, concurrency):
    semaphore = asyncio.Semaphore(max(1, int(concurrency)))

    async def send_one(user_id):
        async with semaphore:
            status = await send_broadcast_to_user(client, user_id, broadcast_message)
            if status in {"sent", "blocked", "deactivated"}:
                await asyncio.to_thread(store.mark_user_broadcasted, user_id)
            return status

    results = await asyncio.gather(*(send_one(user_id) for user_id in user_ids))
    return {
        "sent": results.count("sent"),
        "blocked": results.count("blocked"),
        "deactivated": results.count("deactivated"),
        "error": results.count("error") + results.count("empty"),
    }


async def broadcast_loop(client, config, store):
    while True:
        try:
            broadcast_time = (await asyncio.to_thread(store.get_broadcast_time)).strip().lower()
            if broadcast_time in BROADCAST_DISABLED_VALUES:
                await asyncio.sleep(60)
                continue
            now = dt.datetime.now(WIB)
            today = now.strftime("%Y-%m-%d")
            broadcast_day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.UTC).isoformat()
            if await asyncio.to_thread(store.get_last_broadcast_date) == today:
                await asyncio.sleep(60)
                continue
            if now.strftime("%H:%M") < broadcast_time:
                await asyncio.sleep(60)
                continue

            broadcast_message = await asyncio.to_thread(store.get_active_broadcast_message)
            if not broadcast_message:
                await asyncio.to_thread(store.set_last_broadcast_date, today)
                await send_log(client, config, store, "<b>Daily broadcast skipped</b>\nBelum ada broadcast aktif.")
                await asyncio.sleep(60)
                continue

            totals = {"sent": 0, "blocked": 0, "deactivated": 0, "error": 0}
            while True:
                targets = await asyncio.to_thread(
                    store.get_broadcast_targets,
                    config.broadcast_batch_size,
                    broadcast_day_start,
                )
                user_ids = [int(target["user_id"]) for target in targets]
                if not user_ids:
                    break
                batch = await send_broadcast_batch(client, store, broadcast_message, user_ids, config.qris_create_concurrency)
                for key, value in batch.items():
                    totals[key] += value
                await asyncio.sleep(1)

            await asyncio.to_thread(store.set_last_broadcast_date, today)
            await send_log(
                client,
                config,
                store,
                (
                    "<b>Broadcast selesai</b>\n"
                    f"Terkirim: <code>{totals['sent']}</code>\n"
                    f"Blocked: <code>{totals['blocked']}</code>\n"
                    f"Deactivated: <code>{totals['deactivated']}</code>\n"
                    f"Error: <code>{totals['error']}</code>"
                ),
            )
        except Exception as exc:
            LOGGER.exception("Broadcast loop error")
            await send_log(client, config, store, f"<b>Broadcast loop error</b>\n<code>{html.escape(str(exc))}</code>")
        await asyncio.sleep(60)
