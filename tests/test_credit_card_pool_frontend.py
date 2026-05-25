import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_TSX = ROOT / "frontend" / "src" / "App.tsx"
PAGE_TSX = ROOT / "frontend" / "src" / "pages" / "CreditCardPool.tsx"


class CreditCardPoolFrontendTests(unittest.TestCase):
    def test_app_exposes_credit_card_pool_route_and_sidebar_entry(self):
        source = APP_TSX.read_text(encoding="utf-8")
        self.assertIn("CreditCardPool", source)
        self.assertIn('to="/credit-card-pool"', source)
        self.assertIn('path="/credit-card-pool"', source)
        self.assertIn("信用卡池", source)

    def test_credit_card_pool_page_shows_full_card_fields_for_local_use(self):
        source = PAGE_TSX.read_text(encoding="utf-8")
        self.assertIn("/credit-card-pool", source)
        self.assertIn("item.number", source)
        self.assertIn("item.cvv", source)
        self.assertIn("item.address", source)
        self.assertIn("批量导入", source)


if __name__ == "__main__":
    unittest.main()