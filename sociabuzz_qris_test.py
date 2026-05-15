#!/usr/bin/env python3
import argparse
import sys

try:
    from sociabuzz_client import (
        DEFAULT_USERNAME,
        SociaBuzzError,
        create_donation_order,
        create_qris,
        download_qr,
        new_session,
        poll_status,
    )
except ImportError:
    print("Missing dependency: requests. Install with: pip install requests", file=sys.stderr)
    raise


def parse_args():
    parser = argparse.ArgumentParser(
        description="Test SociaBuzz TRIBE internal QRIS creation flow."
    )
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--amount", default="1000", help="Donation amount in IDR, e.g. 1000")
    parser.add_argument("--name", default="Tester API")
    parser.add_argument("--email", default="tester@example.com")
    parser.add_argument("--note", default="test api trace")
    parser.add_argument(
        "--cookie",
        default="",
        help="Optional raw Cookie header from browser if Cloudflare blocks terminal requests.",
    )
    parser.add_argument("--poll", action="store_true", help="Poll pending page for paid status.")
    parser.add_argument(
        "--wait-paid",
        action="store_true",
        help="After creating QRIS, immediately poll until paid, failed, or max polls is reached.",
    )
    parser.add_argument(
        "--check-inv",
        default="",
        help="Only check an existing SociaBuzz inv_id, without creating a new QRIS.",
    )
    parser.add_argument("--interval", type=int, default=10, help="Polling interval in seconds.")
    parser.add_argument("--max-polls", type=int, default=18, help="Max poll attempts.")
    parser.add_argument("--download-qr", default="", help="Optional output path for QR image.")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


def log(message):
    print(message, flush=True)


def main():
    args = parse_args()
    session = new_session(args.cookie)

    try:
        if args.check_inv:
            status = poll_status(session, args.check_inv, args.interval, args.max_polls, logger=log)
            log(f"final_status  : {status}")
            return 0

        order_id, payment_url, _ = create_donation_order(
            session,
            args.username,
            args.amount,
            args.name,
            args.email,
            args.note,
            debug=args.debug,
            logger=log,
        )
        qris = create_qris(session, order_id, payment_url, args.amount, debug=args.debug, logger=log)

        inv_id = qris.get("inv_id")
        qris_payload = qris.get("data", {})
        log("OK: QRIS created")
        log(f"order_id      : {order_id}")
        log(f"payment_url   : {payment_url}")
        log(f"inv_id        : {inv_id}")
        log(f"amount        : {qris_payload.get('amount')}")
        log(f"qr_string     : {qris_payload.get('qr_string')}")
        log(f"expires       : {qris_payload.get('countdown')}")

        if args.download_qr:
            path = download_qr(session, qris, args.download_qr)
            log(f"qr_saved      : {path.resolve()}")

        if args.wait_paid and inv_id:
            log("")
            log("Polling dimulai sekarang. Silakan scan/bayar QRIS, status akan dicek otomatis.")
            final_status = poll_status(session, inv_id, args.interval, args.max_polls, logger=log)
            log(f"final_status  : {final_status}")
        elif args.poll and inv_id:
            final_status = poll_status(session, inv_id, args.interval, args.max_polls, logger=log)
            log(f"final_status  : {final_status}")

    except SociaBuzzError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(
            "Tip: if this fails with Cloudflare/403, open the page in browser, copy Cookie header, "
            "then rerun with --cookie \"...\".",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
