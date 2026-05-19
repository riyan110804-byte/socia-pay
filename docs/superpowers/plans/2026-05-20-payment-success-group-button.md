# Payment Success Group Button Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make successful payment delivery show clear join/open steps and include a package-named inline button that opens the purchased Telegram group after the user has joined.

**Architecture:** Keep all behavior in `telegram_vip_bot.py`, matching the existing single-file bot structure. Add small pure helper functions for internal Telegram URL generation and button construction, then wire them into `process_paid_payment` where the paid message is sent.

**Tech Stack:** Python 3, Telethon `Button.url`, Supabase-backed payment/package rows, existing HTML message formatting.

---

## File Structure

- Modify: `telegram_vip_bot.py`
  - Add pure helper `internal_telegram_chat_url(chat_id)` near other formatting/link helpers.
  - Add pure helper `paid_message_buttons(payment)` near `paid_message`.
  - Update `paid_message()` wording to the approved step-by-step message.
  - Update `process_paid_payment()` to pass `buttons=paid_message_buttons(payment)` into `safe_send_user()`.
- No new production files.
- No database schema changes.

---

### Task 1: Add internal Telegram URL helper

**Files:**
- Modify: `telegram_vip_bot.py:580-590`

- [ ] **Step 1: Add the helper near `telegram_user_link`**

Insert this function after `telegram_user_link(user)`:

```python
def internal_telegram_chat_url(chat_id):
    raw = str(chat_id or "").strip()
    if not raw.startswith("-100"):
        return ""
    internal_id = raw[4:]
    if not internal_id.isdigit():
        return ""
    return f"https://t.me/c/{internal_id}"
```

- [ ] **Step 2: Run a syntax check**

Run:

```bash
python -m py_compile telegram_vip_bot.py
```

Expected: command exits with code 0 and prints no output.

- [ ] **Step 3: Commit the helper**

```bash
git add telegram_vip_bot.py
git commit -m "Add Telegram internal chat URL helper"
```

---

### Task 2: Update paid success message wording

**Files:**
- Modify: `telegram_vip_bot.py:745-751`

- [ ] **Step 1: Replace `paid_message` with step-by-step wording**

Replace the existing `paid_message` function with:

```python
def paid_message(invite_link, package_name="VIP", invite_hours=24):
    safe_package_name = html.escape(package_name)
    return (
        "✅ <b>Pembayaran berhasil terdeteksi</b>\n\n"
        f"Akses <b>{safe_package_name}</b> kamu sudah aktif.\n\n"
        "1️⃣ Join group lewat link ini dulu:\n"
        f"{html.escape(invite_link)}\n\n"
        "2️⃣ Setelah sudah join, buka group lagi lewat tombol di bawah.\n\n"
        f"⚠️ Link join hanya bisa dipakai <b>1 kali</b> dan berlaku <b>{int(invite_hours)} jam</b>."
    )
```

- [ ] **Step 2: Run a syntax check**

Run:

```bash
python -m py_compile telegram_vip_bot.py
```

Expected: command exits with code 0 and prints no output.

- [ ] **Step 3: Commit the message wording**

```bash
git add telegram_vip_bot.py
git commit -m "Clarify paid invite message steps"
```

---

### Task 3: Add package-named paid message button helper

**Files:**
- Modify: `telegram_vip_bot.py:745-770`

- [ ] **Step 1: Add `paid_message_buttons` after `paid_message`**

Insert this function immediately after `paid_message`:

```python
def paid_message_buttons(payment):
    url = internal_telegram_chat_url(payment.get("vip_chat_id"))
    if not url:
        return None
    package_name = (payment.get("package_name") or "VIP").strip() or "VIP"
    return [[Button.url(f"Buka {package_name}", url)]]
```

- [ ] **Step 2: Run a syntax check**

Run:

```bash
python -m py_compile telegram_vip_bot.py
```

Expected: command exits with code 0 and prints no output.

- [ ] **Step 3: Commit the button helper**

```bash
git add telegram_vip_bot.py
git commit -m "Add paid message group button helper"
```

---

### Task 4: Attach the button when delivering paid message

**Files:**
- Modify: `telegram_vip_bot.py:1107-1120`

- [ ] **Step 1: Pass buttons into `safe_send_user`**

Find the `safe_send_user` call in `process_paid_payment` that sends `paid_message(...)` and change it to include the button helper:

```python
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
```

- [ ] **Step 2: Run a syntax check**

Run:

```bash
python -m py_compile telegram_vip_bot.py
```

Expected: command exits with code 0 and prints no output.

- [ ] **Step 3: Commit the delivery wiring**

```bash
git add telegram_vip_bot.py
git commit -m "Attach group button to paid message"
```

---

### Task 5: Add lightweight verification script command

**Files:**
- Modify: `telegram_vip_bot.py`

- [ ] **Step 1: Run helper verification in Python**

Run:

```bash
python - <<'PY'
from telegram_vip_bot import internal_telegram_chat_url, paid_message

assert internal_telegram_chat_url(-1003906637568) == "https://t.me/c/3906637568"
assert internal_telegram_chat_url("-1003906637568") == "https://t.me/c/3906637568"
assert internal_telegram_chat_url(-3906637568) == ""
assert internal_telegram_chat_url("") == ""

message = paid_message("https://t.me/+abc", "Package A", 24)
assert "Akses <b>Package A</b> kamu sudah aktif." in message
assert "1️⃣ Join group lewat link ini dulu:" in message
assert "https://t.me/+abc" in message
assert "2️⃣ Setelah sudah join, buka group lagi lewat tombol di bawah." in message
assert "Link join hanya bisa dipakai <b>1 kali</b>" in message
print("ok")
PY
```

Expected output:

```text
ok
```

- [ ] **Step 2: Run final syntax check**

Run:

```bash
python -m py_compile telegram_vip_bot.py sociabuzz_client.py sociabuzz_qris_test.py
```

Expected: command exits with code 0 and prints no output.

- [ ] **Step 3: Check git status**

Run:

```bash
git status --short
```

Expected: no uncommitted changes except intentionally untracked/ignored local files that existed before implementation.

---

## Self-Review

- Spec coverage: The plan keeps the one-time invite link, updates the success message to two clear steps, derives `https://t.me/c/...` from `vip_chat_id`, labels the button with the package name, and omits the button when no valid internal URL can be built.
- Placeholder scan: No TBD/TODO/fill-in placeholders are present.
- Type consistency: `internal_telegram_chat_url(chat_id)` returns a string; `paid_message_buttons(payment)` returns Telethon-compatible `[[Button.url(...)] ]` or `None`; `safe_send_user` already forwards keyword arguments to `client.send_message`.
