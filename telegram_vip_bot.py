#!/usr/bin/env python3
import asyncio
import datetime as dt
import html
import io
import json
import logging
import os
import random
import re
import secrets
import string
import time
from dataclasses import dataclass

import httpx
import requests
from dotenv import load_dotenv
from supabase import create_client
from telethon import Button, TelegramClient, events, functions, errors
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    MessageEntityBlockquote,
    MessageEntityBold,
    MessageEntityBotCommand,
    MessageEntityCode,
    MessageEntityEmail,
    MessageEntityHashtag,
    MessageEntityItalic,
    MessageEntityMention,
    MessageEntityMentionName,
    MessageEntityPhone,
    MessageEntityPre,
    MessageEntitySpoiler,
    MessageEntityStrike,
    MessageEntityTextUrl,
    MessageEntityUnderline,
    MessageEntityUrl,
)

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
MIN_WITHDRAWAL_AMOUNT = 10_000
WIB = dt.timezone(dt.timedelta(hours=7))
BROADCAST_DISABLED_VALUES = {"", "off", "disable", "disabled", "0"}
BROADCAST_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


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
    broadcast_batch_size: int
    admin_user_ids: set[int]
    supabase_url: str
    supabase_service_role_key: str
    supabase_table: str
    supabase_package_table: str
    user_table: str
    referral_table: str
    withdrawal_table: str


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
    poll_batch_size = max(1, env_int("POLL_BATCH_SIZE", 20))
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
        poll_batch_size=poll_batch_size,
        qris_create_concurrency=max(1, env_int("QRIS_CREATE_CONCURRENCY", 5)),
        broadcast_batch_size=max(1, env_int("BROADCAST_BATCH_SIZE", poll_batch_size)),
        admin_user_ids=parse_admin_ids(os.getenv("ADMIN_USER_IDS", "")),
        supabase_url=env_required("SUPABASE_URL"),
        supabase_service_role_key=env_required("SUPABASE_SERVICE_ROLE_KEY"),
        supabase_table=os.getenv("SUPABASE_TABLE", "vip_payments").strip() or "vip_payments",
        supabase_package_table=os.getenv("SUPABASE_PACKAGE_TABLE", "vip_packages").strip() or "vip_packages",
        user_table=os.getenv("SUPABASE_USER_TABLE", "vip_users").strip() or "vip_users",
        referral_table=os.getenv("SUPABASE_REFERRAL_TABLE", "vip_referrals").strip() or "vip_referrals",
        withdrawal_table=os.getenv("SUPABASE_WITHDRAWAL_TABLE", "vip_withdrawals").strip() or "vip_withdrawals",
    )


class PaymentStore:
    def __init__(self, config):
        self.table = config.supabase_table
        self.package_table = config.supabase_package_table
        self.user_table = config.user_table
        self.referral_table = config.referral_table
        self.withdrawal_table = config.withdrawal_table
        self.broadcast_table = os.getenv("SUPABASE_BROADCAST_TABLE", "vip_broadcast_messages").strip() or "vip_broadcast_messages"
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
        referral=None,
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
        if referral:
            data["referral_id"] = referral.get("id")
            data["referrer_user_id"] = referral.get("referrer_user_id")
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

    def set_broadcast_message(self, message_text, media_file_id="", media_type="", entities_json="[]"):
        now = utc_now_iso()
        self._execute(
            self.client.table(self.broadcast_table).update(
                {"is_active": False, "updated_at": now}
            ).eq("is_active", True),
            "deactivate broadcast messages",
        )
        data = {
            "message_text": message_text or "",
            "media_telegram_file_id": media_file_id or "",
            "media_type": media_type or "",
            "entities_json": entities_json or "[]",
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        }
        response = self._execute(self.client.table(self.broadcast_table).insert(data), "set broadcast message")
        return response.data[0] if response.data else data

    def get_active_broadcast_message(self):
        response = self._execute(
            self.client.table(self.broadcast_table).select("*").eq("is_active", True).order("id", desc=True).limit(1),
            "get active broadcast message",
        )
        return response.data[0] if response.data else None

    def delete_broadcast_message(self):
        response = self._execute(
            self.client.table(self.broadcast_table).update(
                {"is_active": False, "updated_at": utc_now_iso()}
            ).eq("is_active", True),
            "delete broadcast message",
        )
        return bool(response.data)

    def get_broadcast_targets(self, limit=20, before_iso=None):
        query = self.client.table(self.user_table).select("user_id").eq("is_bot", False)
        if before_iso:
            query = query.or_(f"last_broadcast_at.is.null,last_broadcast_at.lt.{before_iso}")
        response = self._execute(
            query.order("last_broadcast_at", desc=False, nullsfirst=True)
            .order("user_id", desc=False)
            .limit(max(1, int(limit))),
            "get broadcast targets",
        )
        return response.data or []

    def mark_user_broadcasted(self, user_id):
        self._execute(
            self.client.table(self.user_table).update(
                {"last_broadcast_at": utc_now_iso(), "updated_at": utc_now_iso()}
            ).eq("user_id", int(user_id)),
            "mark user broadcasted",
        )

    def count_broadcast_targets(self):
        response = self._execute(
            self.client.table(self.user_table).select("user_id", count="exact").eq("is_bot", False).limit(1),
            "count broadcast targets",
        )
        return int(response.count or 0)

    def set_broadcast_time(self, time_str):
        self.set_setting("broadcast_time", time_str or "")

    def get_broadcast_time(self):
        return self.get_setting("broadcast_time", "")

    def set_last_broadcast_date(self, date_str):
        self.set_setting("last_broadcast_date", date_str or "")

    def get_last_broadcast_date(self):
        return self.get_setting("last_broadcast_date", "")

    def upsert_user(self, user):
        existing = self.get_user(user.id)
        code = existing.get("referral_code") if existing else format_referral_code(user.id)
        data = {
            "user_id": user.id,
            "username": user.username or "",
            "full_name": display_name(user),
            "referral_code": code,
            "is_bot": bool(getattr(user, "bot", False)),
            "updated_at": utc_now_iso(),
        }
        if not existing:
            data.update(
                {
                    "balance": 0,
                    "pending_referrals": 0,
                    "successful_referrals": 0,
                    "created_at": utc_now_iso(),
                }
            )
        response = self._execute(self.client.table(self.user_table).upsert(data, on_conflict="user_id"), "upsert user")
        return response.data[0] if response.data else {**(existing or {}), **data}

    def get_user(self, user_id):
        query = self.client.table(self.user_table).select("*").eq("user_id", int(user_id)).limit(1)
        response = self._execute(query, "get user")
        return response.data[0] if response.data else None

    def get_user_by_referral_code(self, code):
        query = self.client.table(self.user_table).select("*").eq("referral_code", code).limit(1)
        response = self._execute(query, "get user by referral code")
        return response.data[0] if response.data else None

    def create_referral_if_absent(self, referrer, invited_user):
        invited = self.upsert_user(invited_user)
        if not should_create_referral(invited_user.id, referrer.get("user_id") if referrer else 0, invited.get("invited_by_user_id")):
            return None, False
        code = referrer["referral_code"]
        data = {
            "referrer_user_id": int(referrer["user_id"]),
            "referrer_code": code,
            "invited_user_id": invited_user.id,
            "invited_username": invited_user.username or "",
            "invited_full_name": display_name(invited_user),
            "status": "pending",
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        }
        response = self._execute(self.client.table(self.referral_table).insert(data), "create referral")
        referral = response.data[0] if response.data else data
        self._execute(
            self.client.table(self.user_table).update(
                {"invited_by_user_id": int(referrer["user_id"]), "updated_at": utc_now_iso()}
            ).eq("user_id", invited_user.id).is_("invited_by_user_id", "null"),
            "set invited by user",
        )
        self._execute(
            self.client.rpc("vip_increment_pending_referral", {"p_user_id": int(referrer["user_id"])}),
            "increment pending referral",
        )
        return referral, True

    def pending_referral_for_user(self, user_id):
        query = self.client.table(self.referral_table).select("*").eq("invited_user_id", user_id).eq("status", "pending").limit(1)
        response = self._execute(query, "get pending referral")
        return response.data[0] if response.data else None

    def referral_stats(self, user_id):
        user = self.get_user(user_id) or {}
        return {
            "pending_count": int(user.get("pending_referrals") or 0),
            "successful_count": int(user.get("successful_referrals") or 0),
            "balance": int(user.get("balance") or 0),
            "referral_code": user.get("referral_code") or format_referral_code(user_id),
            "invited_by_user_id": user.get("invited_by_user_id"),
            "phone": user.get("phone") or "",
        }

    def mark_referral_paid(self, referral_id, payment, commission):
        query = self.client.table(self.referral_table).update(
            {
                "status": "paid",
                "payment_inv_id": payment["inv_id"],
                "package_code": payment.get("package_code") or "",
                "package_amount": int(payment.get("package_amount") or 0),
                "commission_amount": int(commission),
                "updated_at": utc_now_iso(),
            }
        ).eq("id", referral_id).eq("status", "pending")
        response = self._execute(query, "mark referral paid")
        referral = response.data[0] if response.data else None
        if referral:
            self._execute(
                self.client.rpc(
                    "vip_credit_referral_commission",
                    {"p_user_id": int(referral["referrer_user_id"]), "p_amount": int(commission)},
                ),
                "credit referral balance",
            )
        return referral

    def create_withdrawal(self, user, amount, details):
        data = {
            "user_id": user.id,
            "username": user.username or "",
            "full_name": display_name(user),
            "amount": int(amount),
            "phone": details["phone"],
            "wallet_name": details["wallet_name"],
            "account_name": details["account_name"],
            "status": "pending",
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        }
        response = self._execute(
            self.client.rpc(
                "vip_create_withdrawal",
                {
                    "p_user_id": user.id,
                    "p_username": user.username or "",
                    "p_full_name": display_name(user),
                    "p_amount": int(amount),
                    "p_phone": details["phone"],
                    "p_wallet_name": details["wallet_name"],
                    "p_account_name": details["account_name"],
                },
            ),
            "create withdrawal",
        )
        if not response.data:
            raise ValueError("Insufficient balance")
        return response.data[0]

    def get_withdrawal(self, withdrawal_id):
        query = self.client.table(self.withdrawal_table).select("*").eq("id", int(withdrawal_id)).limit(1)
        response = self._execute(query, "get withdrawal")
        return response.data[0] if response.data else None

    def update_withdrawal_status(self, withdrawal_id, from_status, to_status, admin_user_id):
        query = self.client.table(self.withdrawal_table).update(
            {"status": to_status, "admin_user_id": int(admin_user_id), "updated_at": utc_now_iso()}
        ).eq("id", int(withdrawal_id)).eq("status", from_status)
        response = self._execute(query, f"mark withdrawal {to_status}")
        withdrawal = response.data[0] if response.data else None
        if withdrawal and to_status == "rejected":
            self._execute(
                self.client.rpc(
                    "vip_credit_balance",
                    {"p_user_id": int(withdrawal["user_id"]), "p_amount": int(withdrawal.get("amount") or 0)},
                ),
                "refund rejected withdrawal",
            )
        return withdrawal


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


def format_referral_code(user_id):
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    value = (int(user_id) * 2654435761) & 0xFFFFFFFF
    chars = []
    for _ in range(6):
        chars.append(alphabet[value % len(alphabet)])
        value //= len(alphabet)
    return "".join(chars).rstrip("A") or "A2345"


def parse_referral_payload(payload):
    raw = (payload or "").strip()
    if raw.lower().startswith("ref_"):
        raw = raw[4:]
    raw = raw.upper()
    if 5 <= len(raw) <= 6 and all(ch in string.ascii_uppercase + string.digits for ch in raw):
        return raw
    return ""


def should_create_referral(invited_user_id, referrer_user_id, existing_referral):
    return bool(referrer_user_id) and int(invited_user_id) != int(referrer_user_id) and not existing_referral


def updated_referral_counters(user_row, commission):
    return {
        "balance": int(user_row.get("balance") or 0) + int(commission),
        "pending_referrals": max(0, int(user_row.get("pending_referrals") or 0) - 1),
        "successful_referrals": int(user_row.get("successful_referrals") or 0) + 1,
    }


def valid_withdrawal_amount(amount, balance):
    return int(amount) >= MIN_WITHDRAWAL_AMOUNT and int(amount) <= int(balance)


def referral_commission(payment):
    return int(payment.get("package_amount") or 0) // 2


def parse_withdrawal_amount(raw):
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    return int(digits) if digits else 0


def withdrawal_details_text(raw):
    fields = {}
    labels = {
        "no hp": "phone",
        "nama e-wallet": "wallet_name",
        "atas nama": "account_name",
    }
    for line in (raw or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized = key.strip().lower()
        if normalized in labels:
            fields[labels[normalized]] = value.strip()
    missing = [label for label, key in labels.items() if not fields.get(key)]
    if missing:
        raise ValueError("Format data penarikan belum lengkap.")
    return fields


def main_menu_button_labels():
    return ["🛒 Beli Group VIP", "👤 Profile", "💰 Tarik Saldo"]


def main_menu_buttons():
    buy_label, profile_label, withdrawal_label = main_menu_button_labels()
    return [[Button.text(buy_label, resize=True)], [Button.text(profile_label, resize=True), Button.text(withdrawal_label, resize=True)]]


def main_menu_keyboard_text(user):
    name = display_name(user) or "kak"
    return f"Hi {html.escape(name)}, Welcome di Bot Payment @boboinaja."


def validate_broadcast_time(raw):
    value = (raw or "").strip().lower()
    if value in BROADCAST_DISABLED_VALUES:
        return ""
    if not BROADCAST_TIME_PATTERN.fullmatch(value):
        raise ValueError("Format: /set_broadcasttime HH:MM atau /set_broadcasttime off")
    return value


def admin_command_list_text():
    return "\n".join(
        [
            "<b>Daftar Command Admin</b>",
            "",
            "/chatid - Lihat chat_id logging chat",
            "/custom &lt;amount&gt; - Buat QRIS custom",
            "/package_add &lt;kode&gt; &lt;nama&gt;|&lt;chat_id&gt;|&lt;harga&gt; - Tambah/update paket",
            "/package_list - Lihat paket aktif",
            "/package_delete &lt;kode&gt; - Nonaktifkan paket",
            "/setvip &lt;chat_id|here&gt; - Set VIP chat default",
            "/setlog &lt;chat_id|here&gt; - Set logging chat",
            "/config - Lihat config aktif",
            "/set_broadcast - Reply pesan untuk disimpan sebagai broadcast",
            "/set_broadcasttime &lt;HH:MM|off&gt; - Jadwalkan broadcast harian WIB",
            "/test_broadcast - Kirim test broadcast hanya ke admin",
            "/commands - Tampilkan daftar command ini",
        ]
    )


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


def is_user_deactivated_error(exc):
    deactivated_error = getattr(errors, "InputUserDeactivatedError", None)
    return (deactivated_error is not None and isinstance(exc, deactivated_error)) or "user was deleted" in str(exc).lower()


def is_sociabuzz_timeout(exc):
    return isinstance(exc, (requests.Timeout, TimeoutError)) or "timed out" in str(exc).lower()


ENTITY_NAME_MAP = {
    "MessageEntityBlockquote": MessageEntityBlockquote,
    "MessageEntityBold": MessageEntityBold,
    "MessageEntityBotCommand": MessageEntityBotCommand,
    "MessageEntityCode": MessageEntityCode,
    "MessageEntityEmail": MessageEntityEmail,
    "MessageEntityHashtag": MessageEntityHashtag,
    "MessageEntityItalic": MessageEntityItalic,
    "MessageEntityMention": MessageEntityMention,
    "MessageEntityMentionName": MessageEntityMentionName,
    "MessageEntityPhone": MessageEntityPhone,
    "MessageEntityPre": MessageEntityPre,
    "MessageEntitySpoiler": MessageEntitySpoiler,
    "MessageEntityStrike": MessageEntityStrike,
    "MessageEntityTextUrl": MessageEntityTextUrl,
    "MessageEntityUnderline": MessageEntityUnderline,
    "MessageEntityUrl": MessageEntityUrl,
}


def entities_to_json(entities):
    if not entities:
        return "[]"
    return json.dumps([entity.to_dict() for entity in entities], ensure_ascii=False)


def json_to_entities(raw):
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        LOGGER.warning("Invalid broadcast entities JSON: %r", raw)
        return []
    entities = []
    for item in data or []:
        cls = ENTITY_NAME_MAP.get(item.get("_"))
        if not cls:
            continue
        args = {key: value for key, value in item.items() if key != "_"}
        try:
            entities.append(cls(**args))
        except TypeError as exc:
            LOGGER.warning("Failed to rebuild entity %s: %s", item.get("_"), exc)
    return entities


def format_qris_expiry(raw_expires):
    if not raw_expires:
        return ""
    parsed = parse_iso_datetime(raw_expires)
    if not parsed:
        return raw_expires
    return parsed.astimezone(dt.timezone(dt.timedelta(hours=7))).strftime("%d/%m/%Y %H:%M WIB")


def format_log_datetime(raw_datetime):
    parsed = parse_iso_datetime(raw_datetime)
    if not parsed:
        return html.escape(str(raw_datetime or ""))
    local = parsed.astimezone(dt.timezone(dt.timedelta(hours=7)))
    return html.escape(local.strftime("%Y %B %d, %H:%M:%S WIB"))


def format_custom_qris_expiry(raw_expires):
    parsed = parse_iso_datetime(raw_expires)
    if not parsed:
        return html.escape(str(raw_expires or ""))
    local = parsed.astimezone(dt.timezone(dt.timedelta(hours=7)))
    month_names = {
        1: "Januari",
        2: "Februari",
        3: "Maret",
        4: "April",
        5: "Mei",
        6: "Juni",
        7: "Juli",
        8: "Agustus",
        9: "September",
        10: "Oktober",
        11: "November",
        12: "Desember",
    }
    return f"{local.day} {month_names[local.month]} {local.year}, {local:%H:%M:%S} WIB"


def user_link(row):
    name = html.escape(row["full_name"] or str(row["user_id"]))
    return f'<a href="tg://user?id={row["user_id"]}">{name}</a>'


def username_or_name(row):
    username = (row.get("username") or "").strip()
    if username:
        return f"@{html.escape(username)}"
    return html.escape(row.get("full_name") or str(row.get("user_id") or ""))


def plain_user_link(row):
    user_id = int(row.get("user_id") or 0)
    return f'<a href="tg://user?id={user_id}">{username_or_name(row)}</a>'


def referral_user_log_text(row):
    user_id = int(row["user_id"])
    name = html.escape(row.get("full_name") or str(user_id))
    username = (row.get("username") or "").strip()
    username_line = f"\nUsername: @{html.escape(username)}" if username else ""
    return f'<a href="tg://user?id={user_id}">{name}</a>{username_line}\nUser ID: <code>{user_id}</code>'


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
    return f"https://t.me/c/{internal_id}/4"


def is_admin(config, user_id):
    return user_id in config.admin_user_ids


def runtime_vip_chat_id(config, store):
    return store.get_int_setting("vip_chat_id", config.vip_chat_id)


def runtime_log_chat_id(config, store):
    return store.get_int_setting("log_chat_id", config.log_chat_id)


async def send_log(client, config, store, text, **kwargs):
    try:
        log_chat_id = runtime_log_chat_id(config, store)
    except Exception as exc:
        LOGGER.warning("Failed to load runtime log_chat_id, falling back to env: %s", exc)
        log_chat_id = config.log_chat_id
    if not log_chat_id:
        return
    try:
        await client.send_message(log_chat_id, text, parse_mode="html", link_preview=False, **kwargs)
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
            log_title = "User delivery blocked"
        elif is_user_deactivated_error(exc):
            LOGGER.warning("User %s is deleted/deactivated, cannot deliver message", user_id)
            status = "blocked"
            log_title = "User delivery deactivated"
        else:
            LOGGER.exception("Failed to send message to user %s", user_id)
            status = "error"
            log_title = "User delivery error"
        await send_log(
            client,
            config,
            store,
            (
                f"<b>{log_title}</b>\n"
                f"User: <code>{user_id}</code>\n"
                f"Error: <code>{html.escape(str(exc))}</code>"
            ),
        )
        return status


async def send_broadcast_to_user(client, user_id, broadcast_message):
    async def send_once():
        text = broadcast_message.get("message_text") or ""
        media_file_id = broadcast_message.get("media_telegram_file_id") or ""
        entities = json_to_entities(broadcast_message.get("entities_json") or "[]")
        entity_kwargs = {"formatting_entities": entities} if entities else {}
        if media_file_id:
            await client.send_file(user_id, media_file_id, caption=text or None, parse_mode=None, **entity_kwargs)
        elif text:
            await client.send_message(user_id, text, parse_mode=None, **entity_kwargs)
        else:
            return "empty"
        return "sent"

    error = None
    try:
        return await send_once()
    except FloodWaitError as exc:
        LOGGER.warning("FloodWait %ss while broadcasting to user %s", exc.seconds, user_id)
        await asyncio.sleep(max(1, int(exc.seconds)))
        try:
            return await send_once()
        except Exception as retry_exc:
            error = retry_exc
    except Exception as exc:
        error = exc

    if is_user_blocked_error(error):
        LOGGER.warning("User %s blocked the bot, cannot deliver broadcast", user_id)
        return "blocked"
    if is_user_deactivated_error(error):
        LOGGER.warning("User %s is deleted/deactivated, cannot deliver broadcast", user_id)
        return "deactivated"
    LOGGER.warning("Broadcast send error to user %s: %s", user_id, error)
    return "error"


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


def create_qris_with_retries_sync(config, user, amount=None, note_prefix="VIP", attempts=3):
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return create_qris_sync(config, user, amount, note_prefix)
        except Exception as exc:
            last_exc = exc
            if not is_sociabuzz_timeout(exc) or attempt >= attempts:
                raise
            delay = min(6, 2 ** (attempt - 1)) + random.uniform(0, 0.5)
            LOGGER.warning(
                "SociaBuzz timed out while creating %s QRIS for user %s, retrying in %.1fs (%s/%s): %s",
                note_prefix,
                user.id,
                delay,
                attempt,
                attempts,
                exc,
            )
            time.sleep(delay)
    raise last_exc


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
    package_name = html.escape(package["name"])
    human_expires = format_custom_qris_expiry(expires) if expires else ""
    detail_lines = [
        f"<b>Kode Pesanan</b>: <code>{public_invoice}</code>",
        f"<b>Paket</b>: {package_name}",
        f"<b>Nominal Paket</b>: {format_rupiah(checkout_amount)}",
    ]
    if final_amount:
        detail_lines.append(f"<b>Nominal QRIS</b>: {html.escape(final_amount)}")
    if human_expires:
        detail_lines.append(f"<b>⏳ Batas Bayar</b>: {human_expires}")
    lines = [
        f"<b>Akses {package_name}</b>",
        "",
        f"<blockquote>{'\n'.join(detail_lines)}</blockquote>",
        "",
        "📌 <b>Aturan pembayaran</b>",
        "• Scan QRIS ini lalu bayar sesuai nominal QRIS.",
        "• Bayar 1 kali saja, jangan diulang.",
        "• QRIS ini unik khusus pesanan kamu.",
        "• Status dicek otomatis, tidak perlu kirim bukti transfer.",
        "",
        f"Setelah pembayaran terdeteksi, link akses {package_name} akan langsung dikirim otomatis.",
    ]
    return "\n".join(lines)


def custom_qris_caption(inv_id, checkout_amount, final_amount, expires, user):
    public_invoice = html.escape(inv_id)
    human_expires = format_custom_qris_expiry(expires) if expires else ""
    detail_lines = [
        f"<b>Kode Pesanan</b>: <code>{public_invoice}</code>",
        f"<b>Requester</b>: {telegram_user_link(user)} (<code>{user.id}</code>)",
        f"<b>Nominal Custom</b>: {format_rupiah(checkout_amount)}",
    ]
    if final_amount:
        detail_lines.append(f"<b>Nominal QRIS</b>: {html.escape(final_amount)}")
    if human_expires:
        detail_lines.append(f"<b>⏳ Batas Bayar</b>: {human_expires}")
    lines = [
        "🧾 <b>Custom QRIS</b>",
        "",
        f"<blockquote>{'\n'.join(detail_lines)}</blockquote>",
        "",
        "📌 <b>Aturan pembayaran</b>",
        "• Bayar <b>sesuai nominal QRIS</b>.",
        "• Bayar <b>1 kali saja</b>, jangan diulang.",
        "• Status akan dicek otomatis.",
    ]
    return "\n".join(lines)


def paid_message(invite_link, package_name="VIP", invite_hours=24, group_url=""):
    safe_package_name = html.escape(package_name)
    safe_group_url = html.escape(group_url or "")
    group_link = f'<a href="{safe_group_url}">Buka {safe_package_name}</a>' if safe_group_url else f"Buka {safe_package_name}"
    return (
        "✅ <b>Pembayaran berhasil terdeteksi</b>\n\n"
        f"Akses <b>{safe_package_name}</b> kamu sudah aktif.\n\n"
        "1️⃣ Join group lewat link ini dulu:\n"
        f"{html.escape(invite_link)}\n\n"
        "2️⃣ Setelah sudah join, buka group lagi lewat link ini:\n"
        f"{group_link}\n\n"
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
            f"<b>Invite Link Expires</b>: {format_log_datetime(invite_expires_at)}"
            "</blockquote>"
        ),
    )


async def delete_qris_message(client, payment):
    chat_id = payment.get("qris_chat_id")
    message_id = payment.get("qris_message_id")
    invoice_id = payment.get("public_invoice_id") or payment.get("inv_id")
    if not chat_id or not message_id:
        return
    try:
        await client.delete_messages(int(chat_id), [int(message_id)], revoke=True)
    except Exception as exc:
        text = str(exc).lower()
        if "service message" in text or "message id is invalid" in text or "could not find" in text:
            LOGGER.info(
                "QRIS message %s in chat %s for invoice %s was already unavailable or not deletable: %s",
                message_id,
                chat_id,
                invoice_id,
                exc,
            )
            return
        LOGGER.warning(
            "Failed to delete QRIS message %s in chat %s for invoice %s: %s",
            message_id,
            chat_id,
            invoice_id,
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
        lines = [
            "<b>Profile</b>",
            "Dapatkan komisi sebesar <b>50%</b> dari setiap pembelian paket VIP melalui referral link kamu.",
            "",
            f"<blockquote>{'\n'.join(detail_lines)}</blockquote>",
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

    withdrawal_states = {}

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

    await client.start(bot_token=config.bot_token)
    await send_log(client, config, store, "<b>VIP bot started</b>")
    LOGGER.info("VIP bot started")
    asyncio.create_task(polling_loop(client, config, store))
    asyncio.create_task(broadcast_loop(client, config, store))
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
