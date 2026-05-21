import unittest
from types import SimpleNamespace

from telegram_vip_bot import (
    format_referral_code,
    main_menu_keyboard_text,
    paid_message,
    referral_user_log_text,
    parse_referral_payload,
    parse_withdrawal_amount,
    internal_telegram_chat_url,
    referral_commission,
    should_create_referral,
    updated_referral_counters,
    valid_withdrawal_amount,
    withdrawal_details_text,
)


class ReferralWithdrawalTest(unittest.TestCase):
    def test_referral_code_is_short_alphanumeric(self):
        code = format_referral_code(123456789)
        self.assertRegex(code, r"^[A-Z0-9]{5,6}$")

    def test_parse_referral_payload_accepts_short_code(self):
        self.assertEqual(parse_referral_payload("ref_AB12C"), "AB12C")
        self.assertEqual(parse_referral_payload("AB12C"), "AB12C")

    def test_referral_commission_is_half_package_amount(self):
        self.assertEqual(referral_commission({"package_amount": 50000, "amount": 51000}), 25000)

    def test_parse_withdrawal_amount_accepts_dots(self):
        self.assertEqual(parse_withdrawal_amount("50.000"), 50000)
        self.assertEqual(parse_withdrawal_amount("50000"), 50000)

    def test_withdrawal_details_text_requires_three_lines(self):
        data = withdrawal_details_text("No Hp: 0812\nNama E-Wallet: Dana\nAtas Nama: Budi")
        self.assertEqual(data["phone"], "0812")
        self.assertEqual(data["wallet_name"], "Dana")
        self.assertEqual(data["account_name"], "Budi")

    def test_existing_invited_user_is_not_reassigned_to_new_referrer(self):
        self.assertFalse(should_create_referral(invited_user_id=123, referrer_user_id=999, existing_referral={"referrer_user_id": 111}))

    def test_self_referral_is_ignored(self):
        self.assertFalse(should_create_referral(invited_user_id=123, referrer_user_id=123, existing_referral=None))

    def test_new_invited_user_can_create_referral(self):
        self.assertTrue(should_create_referral(invited_user_id=123, referrer_user_id=999, existing_referral=None))

    def test_referral_counters_never_go_below_zero(self):
        counters = updated_referral_counters({"pending_referrals": 0, "successful_referrals": 2, "balance": 10000}, commission=5000)
        self.assertEqual(counters["pending_referrals"], 0)
        self.assertEqual(counters["successful_referrals"], 3)
        self.assertEqual(counters["balance"], 15000)

    def test_withdrawal_amount_must_not_exceed_balance(self):
        self.assertTrue(valid_withdrawal_amount(50000, 50000))
        self.assertFalse(valid_withdrawal_amount(50001, 50000))
        self.assertFalse(valid_withdrawal_amount(0, 50000))

    def test_withdrawal_amount_minimum_is_ten_thousand(self):
        self.assertTrue(valid_withdrawal_amount(10000, 10000))
        self.assertFalse(valid_withdrawal_amount(9999, 10000))

    def test_main_menu_keyboard_text_greets_user(self):
        user = SimpleNamespace(first_name="Budi", last_name="", username="budi")
        text = main_menu_keyboard_text(user)
        self.assertEqual(text, "Hi Budi, Welcome di Bot Payment @boboinaja.")
        self.assertNotIn("Menu tersedia", text)

    def test_referral_user_log_text_includes_name_username_profile_and_id(self):
        text = referral_user_log_text({"user_id": 1007265448, "full_name": "Neal Troy", "username": "nealmtroy"})
        self.assertIn("Neal Troy", text)
        self.assertIn("@nealmtroy", text)
        self.assertIn('tg://user?id=1007265448', text)
        self.assertIn("<code>1007265448</code>", text)

    def test_internal_telegram_chat_url_opens_first_post(self):
        self.assertEqual(internal_telegram_chat_url(-1003906637568), "https://t.me/c/3906637568/4")

    def test_paid_message_uses_package_specific_clickable_group_link(self):
        text = paid_message(
            "https://t.me/+invite",
            package_name="Group A",
            invite_hours=24,
            group_url="https://t.me/c/3906637568/4",
        )
        self.assertIn('<a href="https://t.me/c/3906637568/4">Buka Group A</a>', text)
        self.assertNotIn("tombol di bawah", text)


if __name__ == "__main__":
    unittest.main()
