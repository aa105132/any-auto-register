import unittest
from pathlib import Path

from core.credit_card_pool import CreditCardPool
from platforms.zo.core import resolve_card_info


class CreditCardPoolTests(unittest.TestCase):
    def test_import_lists_full_card_for_local_web(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            pool_path = Path(tmp).joinpath("credit_cards_pool.json")
            pool = CreditCardPool(pool_path)

            result = pool.import_lines([
                "5555555555554444|12|2029|123|United States of America|Example Address|Example City|00000|Example State|Zo User",
                "5555555555554444|12|2029|999|US|New Address|Example City|00000|Example State|Zo User",
            ])

            self.assertEqual(result["created"], 1)
            self.assertEqual(result["updated"], 1)
            items = pool.list_all()
            self.assertEqual(len(items), 1)
            item = items[0]
            self.assertEqual(item["number"], "5555555555554444")
            self.assertEqual(item["cvv"], "999")
            self.assertEqual(item["last4"], "4444")
            self.assertEqual(item["exp_month"], "12")
            self.assertEqual(item["exp_year"], "2029")
            self.assertEqual(item["country"], "US")
            self.assertEqual(item["address"], "New Address")

    def test_default_card_can_be_used_by_zo(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            pool_path = Path(tmp).joinpath("credit_cards_pool.json")
            pool = CreditCardPool(pool_path)
            pool.import_lines([
                "5555555555554444|12|2029|123|US|Example Address|Example City|00000|Example State|Zo User",
            ])

            card = resolve_card_info({"credit_card_pool_path": str(pool_path)})

            self.assertEqual(card["number"], "5555555555554444")
            self.assertEqual(card["exp_month"], "12")
            self.assertEqual(card["exp_year"], "2029")
            self.assertEqual(card["cvv"], "123")
            self.assertTrue(card["_pool_id"])

    def test_marks_used(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            pool_path = Path(tmp).joinpath("credit_cards_pool.json")
            pool = CreditCardPool(pool_path)
            pool.import_lines([
                "5555555555554444|12|2029|123|US|Example Address|Example City|00000|Example State|Zo User",
            ])
            card = pool.get_default()

            self.assertIsNotNone(card)
            self.assertTrue(pool.mark_used(str(card["id"]), platform="zo", account_email="demo@example.com"))

            item = pool.list_all()[0]
            self.assertEqual(item["usage_count"], 1)
            self.assertEqual(item["used_platforms"], ["zo"])
            self.assertEqual(item["last_used_email"], "demo@example.com")


if __name__ == "__main__":
    unittest.main()