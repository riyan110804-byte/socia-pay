import html
from telethon import Button
from vip_bot.helpers import (
    format_custom_qris_expiry,
    format_rupiah,
    format_button_amount,
    telegram_user_link,
    internal_telegram_chat_url,
    normalize_package_code,
    runtime_vip_chat_id,
)

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
    detail_lines_str = "\n".join(detail_lines)
    lines = [
        f"<b>Akses {package_name}</b>",
        "",
        f"<blockquote>{detail_lines_str}</blockquote>",
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
    detail_lines_str = "\n".join(detail_lines)
    lines = [
        "🧾 <b>Custom QRIS</b>",
        "",
        f"<blockquote>{detail_lines_str}</blockquote>",
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
        "dan lanjut masuk to group member VIP."
    )


def main_menu_button_labels():
    return ["🛒 Beli Group VIP", "👤 Profile", "💰 Tarik Saldo"]


def main_menu_buttons():
    buy_label, profile_label, withdrawal_label = main_menu_button_labels()
    return [[Button.text(buy_label, resize=True)], [Button.text(profile_label, resize=True), Button.text(withdrawal_label, resize=True)]]


def main_menu_keyboard_text(user):
    from vip_bot.helpers import display_name
    name = display_name(user) or "kak"
    return f"Hi {html.escape(name)}, Welcome di Bot Payment @boboinaja."


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


def package_buttons(config, store):
    try:
        packages = store.list_packages()
    except Exception:
        packages = []
    if not packages:
        return [[Button.inline(package_label(default_package(config, store)), b"buy_vip")]]
    rows = []
    for package in packages:
        data = f"buy_pkg:{normalize_package_code(package['code'])}".encode()
        rows.append([Button.inline(package_label(package), data)])
    return rows


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


def package_log_line(package):
    if not package:
        return "Package: <code>custom</code>"
    return (
        f"Package: <code>{html.escape(package.get('code') or '')}</code> "
        f"{html.escape(package.get('name') or '')} "
        f"(<code>{package.get('vip_chat_id') or ''}</code>)"
    )
