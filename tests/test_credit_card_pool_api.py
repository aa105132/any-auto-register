import unittest

from fastapi.testclient import TestClient

from main import app


class CreditCardPoolApiTests(unittest.TestCase):
    def test_credit_card_pool_router_is_registered(self):
        paths = {route.path for route in app.routes}
        self.assertIn("/api/credit-card-pool", paths)
        self.assertIn("/api/credit-card-pool/import", paths)
        self.assertIn("/api/credit-card-pool/{card_id}/invalid", paths)

    def test_credit_card_pool_list_endpoint_returns_stats_shape(self):
        client = TestClient(app)
        response = client.get("/api/credit-card-pool")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("stats", data)
        self.assertIn("items", data)
        self.assertIn("source", data)


if __name__ == "__main__":
    unittest.main()