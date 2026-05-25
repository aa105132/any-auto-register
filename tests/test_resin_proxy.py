from __future__ import annotations

import unittest

from core.resin_proxy import parse_resin_platform_map, resolve_resin_proxy_config


class ResinProxyTests(unittest.TestCase):
    def test_parse_platform_map_supports_comments_and_blank_lines(self):
        mapping = parse_resin_platform_map(
            """
            # comment
            venice = SeedancePool
            chatgpt: OpenAIPool

            invalid-line
            """
        )

        self.assertEqual(
            mapping,
            {
                "venice": "SeedancePool",
                "chatgpt": "OpenAIPool",
            },
        )

    def test_resolve_structured_proxy_uses_platform_mapping(self):
        result = resolve_resin_proxy_config(
            {
                "resin_enabled": "true",
                "resin_scheme": "http",
                "resin_host": "127.0.0.1",
                "resin_port": "2260",
                "resin_token": "my-token",
                "resin_default_platform": "Default",
                "resin_platform_map": "venice=SeedancePool",
            },
            task_platform="venice",
            require_enabled=True,
        )

        self.assertEqual(result["source"], "structured")
        self.assertEqual(result["resolved_platform"], "SeedancePool")
        self.assertEqual(result["proxy_url"], "http://SeedancePool:my-token@127.0.0.1:2260")

    def test_resolve_structured_proxy_falls_back_to_default_platform(self):
        result = resolve_resin_proxy_config(
            {
                "resin_enabled": "true",
                "resin_host": "resin.local",
                "resin_port": "2260",
                "resin_token": "my-token",
                "resin_default_platform": "Default",
            },
            task_platform="chatgpt",
            require_enabled=True,
        )

        self.assertEqual(result["resolved_platform"], "Default")
        self.assertEqual(result["proxy_url"], "http://Default:my-token@resin.local:2260")

    def test_resolve_falls_back_to_legacy_url_when_structured_host_missing(self):
        result = resolve_resin_proxy_config(
            {
                "resin_enabled": "true",
                "resin_proxy_url": "http://legacy-user:legacy-pass@127.0.0.1:2260",
            },
            task_platform="venice",
            require_enabled=True,
        )

        self.assertEqual(result["source"], "legacy_url")
        self.assertEqual(result["proxy_url"], "http://legacy-user:legacy-pass@127.0.0.1:2260")

    def test_resolve_returns_none_when_disabled_and_required(self):
        result = resolve_resin_proxy_config(
            {
                "resin_enabled": "false",
                "resin_host": "127.0.0.1",
                "resin_port": "2260",
                "resin_token": "my-token",
                "resin_default_platform": "Default",
            },
            task_platform="venice",
            require_enabled=True,
        )

        self.assertEqual(result["source"], "disabled")
        self.assertIsNone(result["proxy_url"])


if __name__ == "__main__":
    unittest.main()
