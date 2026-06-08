from __future__ import annotations

import unittest

from core.base_captcha import CdpTurnstileSolver


class _TimeoutPage:
    def __init__(self):
        self.goto_calls = []
        self.wait_calls = []

    def goto(self, page_url, wait_until=None, timeout=None):
        self.goto_calls.append({"url": page_url, "wait_until": wait_until, "timeout": timeout})
        raise TimeoutError("Page.goto: Timeout 30000ms exceeded")

    def wait_for_timeout(self, value):
        self.wait_calls.append(value)

    @property
    def frames(self):
        return []

    def locator(self, selector):
        return _EmptyLocator()

    def evaluate(self, script):
        return "ts-token-after-slow-navigation"


class _EmptyLocator:
    def count(self):
        return 0


class CdpTurnstileSolverTests(unittest.TestCase):
    def test_non_clerk_solver_continues_after_domcontentloaded_timeout(self):
        solver = CdpTurnstileSolver(navigation_timeout_ms=90000)
        page = _TimeoutPage()

        token = solver._solve_regular_turnstile(page, "https://www.hpc-ai.com/account/signup", "site-key")

        self.assertEqual(token, "ts-token-after-slow-navigation")
        self.assertEqual(page.goto_calls[0]["timeout"], 90000)
        self.assertEqual(page.goto_calls[0]["wait_until"], "domcontentloaded")
        self.assertIn(1000, page.wait_calls)


if __name__ == "__main__":
    unittest.main()
