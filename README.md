# SociaBuzz QRIS VIP Telegram Bot

Bot Telegram untuk menjual akses group VIP memakai QRIS. Teks bot hanya menyebut QRIS, tidak menyebut SociaBuzz.
QRIS dibuat lewat source payment Xendit supaya mengikuti flow QRIS website SociaBuzz.

## Setup

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Isi `.env`:

- `TELEGRAM_API_ID` dan `TELEGRAM_API_HASH` dari https://my.telegram.org
- `TELEGRAM_BOT_TOKEN` dari BotFather
- `VIP_CHAT_ID` optional fallback group/channel VIP tujuan invite
- `LOG_CHAT_ID` group/channel untuk log transaksi
- `SUPABASE_URL` dan `SUPABASE_SERVICE_ROLE_KEY` dari Supabase Project Settings
- `ADMIN_USER_IDS` Telegram user ID admin yang boleh ubah config bot
- `SOCIABUZZ_USERNAME` username SociaBuzz target TRIBE

Bot harus menjadi admin di semua group VIP tujuan invite dengan permission invite users.

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

Untuk banyak group dengan harga berbeda, buat paket dari group/channel logging:

```text
/package_add a group a|-1001234567890|5000
/package_add b group b|-1009876543210|2000
/package_list
/package_delete a
```

Tombol user akan tampil seperti `group a - 5.000`. Command paket hanya bisa dipakai admin. `/package_add` dan `/package_delete` hanya diproses di `LOG_CHAT_ID` supaya perubahan paket tercatat rapi.

Admin juga bisa membuat QRIS nominal bebas dari group/channel logging:

```text
/custom 50000
```

Command `/custom` hanya diproses kalau dikirim oleh admin di `LOG_CHAT_ID`. QRIS custom akan muncul di chat logging dan statusnya tetap dicek otomatis.

## Run

```powershell
python telegram_vip_bot.py
```

User DM bot lalu `/start`, pilih paket seperti `group a - 5.000`, message tombol akan berubah menjadi invoice QRIS. Nominal checkout mengikuti paket yang dipilih. Setelah pembayaran terdeteksi, bot menghapus QRIS dan mengirim invite link ke group paket tersebut. Kalau pembayaran gagal/expired/tidak valid, bot juga menghapus QRIS supaya tidak terscan lagi. Invite link berlaku sesuai `INVITE_EXPIRE_HOURS` dan hanya bisa dipakai 1 kali. Log transaksi menyimpan paket, kode pesanan internal, kode pesanan user, nominal checkout, nominal QRIS, dan invite link yang dikirim.

Bot juga menampilkan reply keyboard `Profile` dan `Tarik Saldo`. Semua user otomatis disimpan di `vip_users` saat berinteraksi dengan bot, jadi referral link 5-6 digit langsung aktif tanpa perlu membuka Profile dulu. `Profile` berisi saldo referral, referral berhasil, link referral, dan jumlah pending referral. Komisi referral adalah 50% dari `package_amount` setelah user undangan berhasil membayar paket VIP. Pengajuan tarik saldo dikirim ke `LOG_CHAT_ID` dengan tombol admin `Berhasil` dan `Tolak`.

Kalau database sudah pernah dibuat sebelum versi ini, jalankan ulang isi `supabase_schema.sql` di SQL Editor supaya tabel `vip_packages`, kolom paket payment, status recovery production, dan kolom adaptive polling ikut aktif.

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
- `SUPABASE_PACKAGE_TABLE=vip_packages`
- `SUPABASE_USER_TABLE=vip_users`
- `SUPABASE_REFERRAL_TABLE=vip_referrals`
- `SUPABASE_WITHDRAWAL_TABLE=vip_withdrawals`
- `SUPABASE_QUERY_RETRIES=3`
- `SUPABASE_RETRY_BASE_DELAY=0.35`
- `ADMIN_USER_IDS`
- `SOCIABUZZ_USERNAME`
- `PAYMENT_AMOUNT=2000` sebagai fallback kalau belum ada paket aktif.
- `INVITE_EXPIRE_HOURS=24`
- `POLL_INTERVAL_SECONDS=3`
- `POLL_MAX_ATTEMPTS=300`
- `POLL_BATCH_SIZE=20`
- `QRIS_CREATE_CONCURRENCY=5`

State invoice disimpan di Supabase, jadi Railway tidak perlu Volume. Gunakan `service_role` key hanya di Railway Variables, jangan taruh di frontend atau repo.

Polling payment bersifat adaptive. Bot menyimpan `next_check_at`, `poll_attempts`, dan `last_polled_at` di Supabase, lalu hanya mengecek invoice yang sudah waktunya dicek. Intervalnya dihitung dari `qris_expires` asli tiap invoice, jadi QRIS yang expire cepat otomatis dicek lebih rapat dan QRIS yang masih jauh dari expired dicek lebih santai. `POLL_BATCH_SIZE` membatasi jumlah invoice yang diproses per loop supaya Railway dan gateway pembayaran tidak dihajar semua pending user sekaligus.

Kalau Supabase/PostgREST memutus koneksi HTTP/2 saat polling, bot akan retry query Supabase sesuai `SUPABASE_QUERY_RETRIES` sebelum menulis error ke log channel.

## Test QRIS Flow Saja

```powershell
python sociabuzz_qris_test.py --amount 2000 --name "juki ganteng" --email "juki@gmail.com" --download-qr qris.png --wait-paid --interval 3 --max-polls 80
```
