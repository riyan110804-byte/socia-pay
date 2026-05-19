# Payment Success Group Button Design

## Goal

After a payment succeeds, the bot should make the next steps clearer for buyers:

1. Use the existing one-time invite link to join the purchased VIP group.
2. After joining, use an inline button to open that same group again later.

The inline button text should use the purchased package name, similar to the package selection buttons.

## Current behavior

`telegram_vip_bot.py` creates a one-time Telegram invite link after payment is detected, then sends a success message containing that link. Package data already includes `vip_chat_id` and `package_name` on the payment row.

## Design

### Message content

The paid message will become step-based:

```text
✅ Pembayaran berhasil terdeteksi

Akses <Package Name> kamu sudah aktif.

1️⃣ Join group lewat link ini dulu:
<one-time invite link>

2️⃣ Setelah sudah join, buka group lagi lewat tombol di bawah.

⚠️ Link join hanya bisa dipakai 1 kali dan berlaku <hours> jam.
```

### Inline button

When the payment has a valid `vip_chat_id`, the bot will attach one URL button:

```text
[Buka <Package Name>]
```

The URL will be derived from the Telegram private supergroup/channel id:

- `-1003906637568` becomes `https://t.me/c/3906637568`

This link is only intended for users who have already joined. It does not replace the one-time invite link.

### Data flow

- Continue generating the one-time invite link with `ExportChatInviteRequest` and `usage_limit=1`.
- Build the internal group URL from the payment row's `vip_chat_id`.
- Pass the URL button into the existing user delivery call.
- If `vip_chat_id` is missing or not in the expected private Telegram id shape, send the text message without the button.

### Error handling

Button creation should not block delivery. If the internal group URL cannot be built, the bot still sends the success message and the one-time invite link.

### Testing

Add or run lightweight tests/checks for:

- Internal URL conversion from `-100...` chat ids.
- Paid message wording includes the two clear steps.
- Button label uses the package name.

Run Python syntax/test verification before reporting completion.
