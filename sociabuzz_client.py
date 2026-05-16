import json
import io
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests


BASE_URL = "https://sociabuzz.com"


class SociaBuzzError(RuntimeError):
    pass


def noop_log(_message):
    pass


def new_session(cookie_header=""):
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
        }
    )
    if cookie_header:
        session.headers["Cookie"] = cookie_header
    return session


def request_or_fail(session, method, url, **kwargs):
    response = session.request(method, url, timeout=30, **kwargs)
    if response.status_code >= 400:
        snippet = response.text[:500].replace("\n", " ")
        raise SociaBuzzError(f"{method} {url} failed: HTTP {response.status_code}: {snippet}")
    return response


def extract_csrf(html):
    match = re.search(
        r'name=["\']sb_token_csrf["\']\s+value=["\']([^"\']+)["\']',
        html,
        flags=re.I,
    )
    if not match:
        match = re.search(
            r'value=["\']([^"\']+)["\']\s+name=["\']sb_token_csrf["\']',
            html,
            flags=re.I,
        )
    if not match:
        raise SociaBuzzError("Could not find sb_token_csrf in donate form HTML.")
    return match.group(1)


def normalize_amount(amount):
    digits = re.sub(r"[^0-9]", "", str(amount))
    if not digits:
        raise SociaBuzzError("Amount must contain digits.")
    return f"{int(digits):,}"


def create_donation_order(session, username, amount, name, email, note, debug=False, logger=noop_log):
    tribe_url = f"{BASE_URL}/{username}/tribe"
    logger(f"[1/5] Opening TRIBE page: {tribe_url}")
    request_or_fail(session, "GET", tribe_url)

    form_url = f"{BASE_URL}/{username}/donate/queue?type=donate&currency=IDR&message="
    logger("[2/5] Loading donate form and CSRF token...")
    form_response = request_or_fail(
        session,
        "GET",
        form_url,
        headers={"Referer": tribe_url, "X-Requested-With": "XMLHttpRequest"},
    )
    csrf = extract_csrf(form_response.text)
    logger("[3/5] Creating donation order...")

    payload = {
        "sb_token_csrf": csrf,
        "currency": "IDR",
        "amount": normalize_amount(amount),
        "qty": "1",
        "support_duration": "30",
        "note": note,
        "fullname": name,
        "email": email,
        "is_agree": "1",
        "years18": "1",
        "is_vote": "0",
        "is_voice": "0",
        "is_mediashare": "0",
        "is_gif": "0",
        "is_sound": "0",
        "is_voicy": "0",
        "vote_id": "",
        "voice": "",
        "ms_maxtime": "",
        "start_from": "0",
        "ms_starthour": "0",
        "ms_startminute": "0",
        "ms_startsecond": "0",
        "spin_check": "0",
        "prev_url": tribe_url,
        "hide_email": "0",
        "is_tiktok": "0",
        "tiktok_duration": "0",
        "is_instagram": "0",
        "instagram_duration": "0",
        "wishlist_id": "",
        "quickpay": "0",
    }

    submit_url = f"{BASE_URL}/{username}/donate/get-form-queue"
    submit_response = request_or_fail(
        session,
        "POST",
        submit_url,
        data=payload,
        headers={
            "Referer": tribe_url,
            "Origin": BASE_URL,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        },
    )

    try:
        data = submit_response.json()
    except ValueError as exc:
        raise SociaBuzzError(f"Create order did not return JSON: {submit_response.text[:500]}") from exc

    if debug:
        logger("Create order response:")
        logger(json.dumps(data, indent=2, ensure_ascii=False))

    redirect = (
        data.get("content", {}).get("redirect")
        or data.get("redirect")
        or data.get("content", {}).get("url")
    )
    if not redirect:
        raise SociaBuzzError(f"Could not find payment redirect in response: {data}")

    payment_url = urljoin(BASE_URL, redirect)
    order_id = Path(urlparse(payment_url).path).name
    if not order_id:
        raise SociaBuzzError(f"Could not parse order id from payment URL: {payment_url}")

    return order_id, payment_url, data


def create_qris(session, order_id, payment_url, amount, source_payment="midtrans", debug=False, logger=noop_log):
    logger(f"[4/5] Opening payment page: {payment_url}")
    payment_response = request_or_fail(session, "GET", payment_url)
    payment_html = payment_response.text

    csrf = session.cookies.get("csrf_cookie_name")
    if not csrf:
        csrf_match = re.search(r"csrf[_-]hash['\"]?\s*[:=]\s*['\"]([^'\"]+)", payment_html, flags=re.I)
        csrf = csrf_match.group(1) if csrf_match else ""
    if not csrf:
        raise SociaBuzzError("Could not find payment CSRF token. Try passing cookie from browser.")

    logger("[5/5] Loading payment settings and creating QRIS...")
    plain_amount = re.sub(r"[^0-9]", "", str(amount)) or "1000"
    setting_url = (
        f"{BASE_URL}/payment/pay/setting"
        f"?amount={plain_amount}&currency=IDR&base_amount={plain_amount}&base_currency=IDR"
        f"&currency_def=IDR&convertion=IDR&country=Indonesia&feature=TRIBE"
        f"&is_borne_fee=1&risk=&message=&direct=&service_fee=0"
        f"&token={order_id}&country_account="
    )
    request_or_fail(session, "GET", setting_url, headers={"Referer": payment_url})

    payload = {
        "sb_token_csrf": csrf,
        "order_id": order_id,
        "final_currency": "IDR",
        "currency_def": "IDR",
        "payment_method": "qris",
        "type_payment": "qris",
        "source_payment": source_payment,
        "country": "ID",
        "country_pay": "Indonesia",
    }
    response = request_or_fail(
        session,
        "POST",
        f"{BASE_URL}/payment/send/create",
        json=payload,
        headers={
            "Referer": payment_url,
            "Origin": BASE_URL,
            "Accept": "application/json, text/plain, */*",
        },
    )

    try:
        data = response.json()
    except ValueError as exc:
        raise SociaBuzzError(f"Create QRIS did not return JSON: {response.text[:500]}") from exc

    if debug:
        logger("Create QRIS response:")
        logger(json.dumps(data, indent=2, ensure_ascii=False))

    if not data.get("status"):
        raise SociaBuzzError(f"Create QRIS failed: {data}")

    return data


def download_qr(session, qris_data, output_path):
    response = download_qr_response(session, qris_data)
    path = Path(output_path)
    path.write_bytes(response.content)
    return path


def download_qr_response(session, qris_data):
    qr_string = qris_data.get("data", {}).get("qr_string")
    if not qr_string:
        raise SociaBuzzError("QR response has no data.qr_string.")
    if re.match(r"^https?://", qr_string, flags=re.I):
        return request_or_fail(session, "GET", qr_string)
    return LocalResponse(render_qr_payload(qr_string))


class LocalResponse:
    def __init__(self, content):
        self.content = content


def render_qr_payload(qr_payload):
    try:
        import qrcode
    except ImportError as exc:
        raise SociaBuzzError(
            "Xendit returns a direct QRIS payload, not an image URL. "
            "Install QR renderer first: pip install qrcode[pil]"
        ) from exc
    image = qrcode.make(qr_payload)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def check_pending(session, inv_id, logger=noop_log):
    logger(f"Checking payment status for {inv_id}...")
    url = f"{BASE_URL}/payment/pending?type=qris&inv_id={inv_id}"
    start = time.perf_counter()
    response = request_or_fail(session, "GET", url)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    text = re.sub(r"\s+", " ", response.text).lower()

    if any(marker in text for marker in ("transaction success", "pembayaran berhasil", "terima kasih")):
        return "paid", url, elapsed_ms
    if any(marker in text for marker in ("pembayaran belum selesai", "transaction pending", "pending")):
        return "pending", url, elapsed_ms
    if any(marker in text for marker in ("expired", "kedaluwarsa", "failed", "gagal")):
        return "failed_or_expired", url, elapsed_ms
    return "unknown", url, elapsed_ms


def poll_status(session, inv_id, interval, max_polls, logger=noop_log):
    for index in range(1, max_polls + 1):
        status, status_url, elapsed_ms = check_pending(session, inv_id, logger=logger)
        logger(f"poll {index}/{max_polls}: {status} | {elapsed_ms} ms | {status_url}")
        if status != "pending":
            return status
        if index < max_polls:
            logger(f"Waiting {interval}s before next check...")
            time.sleep(interval)
    return "pending"
