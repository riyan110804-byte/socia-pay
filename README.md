# SociaBuzz QRIS VIP Telegram Bot

Bot Telegram untuk menjual akses group VIP memakai QRIS. Teks bot hanya menyebut QRIS, tidak menyebut SociaBuzz.

## Setup

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Isi `.env`:

- `TELEGRAM_API_ID` dan `TELEGRAM_API_HASH` dari https://my.telegram.org
- `TELEGRAM_BOT_TOKEN` dari BotFather
- `VIP_CHAT_ID` group/channel VIP tujuan invite
- `LOG_CHAT_ID` group/channel untuk log transaksi
- `SUPABASE_URL` dan `SUPABASE_SERVICE_ROLE_KEY` dari Supabase Project Settings
- `ADMIN_USER_IDS` Telegram user ID admin yang boleh ubah config bot
- `SOCIABUZZ_USERNAME=yudhaprihardana`

Bot harus menjadi admin di `VIP_CHAT_ID` dengan permission invite users.

Jalankan SQL di `supabase_schema.sql` lewat Supabase SQL Editor sebelum bot dijalankan.

`VIP_CHAT_ID` dan `LOG_CHAT_ID` bisa dikosongkan di Railway kalau mau di-set dari Telegram. Ini lebih aman untuk first deploy supaya startup bot tidak gagal kirim log ke chat yang belum benar:

```text
/chatid
/setvip <chat_id>
/setvip here
/setlog <chat_id>
/setlog here
/config
```

Command `/setvip`, `/setlog`, dan `/config` hanya bisa dipakai Telegram user ID yang masuk `ADMIN_USER_IDS`.

Admin juga bisa membuat QRIS nominal bebas dari group/channel logging:

```text
/custom 50000
```

Command `/custom` hanya diproses kalau dikirim oleh admin di `LOG_CHAT_ID`. QRIS custom akan muncul di chat logging dan statusnya tetap dicek otomatis.

## Run

```powershell
python telegram_vip_bot.py
```

User DM bot lalu `/start`, klik tombol `Beli VIP - Rp2.000`, message tombol akan berubah menjadi invoice QRIS. Setiap invoice memakai kode unik 3 digit di atas harga paket, jadi nominal checkout tidak terus-terusan sama. Setelah pembayaran terdeteksi, bot menghapus QRIS dan mengirim invite link VIP. Kalau pembayaran gagal/expired/tidak valid, bot juga menghapus QRIS supaya tidak terscan lagi. Invite link berlaku 24 jam dan hanya bisa dipakai 1 kali. Log transaksi menyimpan kode pesanan internal, kode pesanan user, total checkout, nominal QRIS, dan invite link yang dikirim.

Kalau database sudah pernah dibuat sebelum versi ini, jalankan ulang isi `supabase_schema.sql` di SQL Editor supaya kolom `public_invoice_id`, `qris_chat_id`, `qris_message_id`, dan status recovery production ikut aktif.

Bot membatasi pembuatan QRIS bersamaan lewat `QRIS_CREATE_CONCURRENCY` supaya traffic ramai tetap antre rapi. Default `5` cukup aman untuk awal; naikkan pelan-pelan kalau SociaBuzz tetap stabil.

## Deploy Railway

Project sudah punya `railway.json`, jadi Railway akan start worker dengan:

```powershell
python telegram_vip_bot.py
```

Set variables ini di Railway:

- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `TELEGRAM_BOT_TOKEN`
- `VIP_CHAT_ID` optional fallback
- `LOG_CHAT_ID` optional fallback
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `SUPABASE_TABLE=vip_payments`
- `SUPABASE_SETTINGS_TABLE=vip_bot_settings`
- `SUPABASE_QUERY_RETRIES=3`
- `SUPABASE_RETRY_BASE_DELAY=0.35`
- `ADMIN_USER_IDS`
- `SOCIABUZZ_USERNAME`
- `PAYMENT_AMOUNT=2000` sebagai harga dasar paket. Bot otomatis menambah kode unik 3 digit untuk total checkout.
- `INVITE_EXPIRE_HOURS=24`
- `POLL_INTERVAL_SECONDS=3`
- `POLL_MAX_ATTEMPTS=300`
- `QRIS_CREATE_CONCURRENCY=5`

State invoice disimpan di Supabase, jadi Railway tidak perlu Volume. Gunakan `service_role` key hanya di Railway Variables, jangan taruh di frontend atau repo.

Kalau Supabase/PostgREST memutus koneksi HTTP/2 saat polling, bot akan retry query Supabase sesuai `SUPABASE_QUERY_RETRIES` sebelum menulis error ke log channel.

## Test QRIS Flow Saja

```powershell
python sociabuzz_qris_test.py --amount 2000 --name "juki ganteng" --email "juki@gmail.com" --download-qr qris.png --wait-paid --interval 3 --max-polls 80
```
