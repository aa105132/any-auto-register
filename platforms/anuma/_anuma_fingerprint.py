"""每次注册随机生成浏览器指纹，避免 Privy 按固定 UA 指纹限流。"""
from __future__ import annotations

import random
from typing import Any

_WIN_CHROME = {
    "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36",
    "platform": "\"Windows\"",
    "brands": "\"Google Chrome\";v=\"{ver}\", \"Not.A/Brand\";v=\"8\", \"Chromium\";v=\"{ver}\"",
}

_MAC_CHROME = {
    "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36",
    "platform": "\"macOS\"",
    "brands": "\"Google Chrome\";v=\"{ver}\", \"Not.A/Brand\";v=\"8\", \"Chromium\";v=\"{ver}\"",
}

_WIN_EDGE = {
    "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36 Edg/{ver}.0.0.0",
    "platform": "\"Windows\"",
    "brands": "\"Microsoft Edge\";v=\"{ver}\", \"Not.A/Brand\";v=\"8\", \"Chromium\";v=\"{ver}\"",
}

_LINUX_CHROME = {
    "ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36",
    "platform": "\"Linux\"",
    "brands": "\"Google Chrome\";v=\"{ver}\", \"Not.A/Brand\";v=\"8\", \"Chromium\";v=\"{ver}\"",
}

_TEMPLATES = [_WIN_CHROME, _MAC_CHROME, _WIN_EDGE, _LINUX_CHROME]

_CHROME_VERSIONS = [120, 121, 122, 123, 124, 125, 126, 127, 128, 129, 130, 131, 132]

_PRIVY_SDK_VERSIONS = ["react-auth:3.14.1", "react-auth:3.13.0", "react-auth:3.12.0", "react-auth:3.11.0"]

_ACCEPT_LANGUAGES = ["zh-CN,zh;q=0.9", "en-US,en;q=0.9", "en-GB,en;q=0.9", "ja-JP,ja;q=0.9", "ko-KR,ko;q=0.9", "de-DE,de;q=0.9", "fr-FR,fr;q=0.9", "zh-TW,zh;q=0.9"]


def build_anuma_fingerprint() -> dict[str, Any]:
    tmpl = random.choice(_TEMPLATES)
    ver = random.choice(_CHROME_VERSIONS)
    return {
        "ua": tmpl["ua"].format(ver=ver),
        "sec_ch_ua_platform": tmpl["platform"],
        "sec_ch_ua": tmpl["brands"].format(ver=ver),
        "privy_client": random.choice(_PRIVY_SDK_VERSIONS),
        "accept_language": random.choice(_ACCEPT_LANGUAGES),
    }
