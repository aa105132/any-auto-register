"""gettoken.dev API actions — verified endpoints 2026-05-19"""
import requests


SESSION = requests.Session()
SESSION.headers.update({
    "accept": "application/json",
    "content-type": "application/json",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
})


def _api_get(path: str, api_key: str = "", cookies: str = "") -> dict:
    headers = {}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    if cookies:
        headers["cookie"] = cookies
    try:
        resp = SESSION.get(f"https://gettoken.dev{path}", headers=headers, timeout=30)
        data = resp.json()
        if resp.status_code >= 400:
            return {"ok": False, "error": f"HTTP {resp.status_code}", "data": data}
        return {"ok": True, "data": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _api_post(path: str, api_key: str = "", cookies: str = "", json_data: dict = None) -> dict:
    headers = {}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    if cookies:
        headers["cookie"] = cookies
    try:
        resp = SESSION.post(f"https://gettoken.dev{path}", headers=headers, json=json_data or {}, timeout=30)
        data = resp.json()
        if resp.status_code >= 400:
            return {"ok": False, "error": f"HTTP {resp.status_code}", "data": data}
        return {"ok": True, "data": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_user(api_key: str = "", cookies: str = "") -> dict:
    """Get authenticated user info via GET /api/user/me"""
    return _api_get("/api/user/me", api_key=api_key, cookies=cookies)


def portal_login(login_token: str, referral_code: str = "", referral_slug: str = "") -> dict:
    """POST /api/auth/portal-login to establish session"""
    body = {"loginToken": login_token}
    if referral_code:
        body["referralCode"] = referral_code
    if referral_slug:
        body["referralSlug"] = referral_slug
    body["referralHost"] = "gettoken.dev"
    return _api_post("/api/auth/portal-login", json_data=body)
