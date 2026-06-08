"""Fireworks AI HTTP / Next.js 协议客户端。

已跑通端到端协议注册（无浏览器、无 Turnstile）。
关键：Next.js Server Action 需要完整的 RSC 协议头。
"""

from __future__ import annotations

import time
import random
import json
import base64
from typing import Any, Callable
from urllib.parse import urljoin, urlparse, parse_qs

import requests


FIREWORKS_BASE = "https://app.fireworks.ai"
FIREWORKS_API = "https://api.fireworks.ai"
DEPLOY_ID = "dpl_3rG473UqWURx1ugPdrkMsdaCWP6U"
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
SEC_CH_UA = '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"'
COGNITO_CLIENT = "sueas7prsfrdp16nantbeqcjv"
COGNITO_REGION = "us-west-2"

# Next.js RSC router state tree（浏览器抓包截取，部署间稳定）
_RSC_SIGNUP = (
    "%5B%22%22%2C%7B%22children%22%3A%5B%22(v2-auth)%22%2C%7B%22children%22"
    "%3A%5B%22signup%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2C"
    "null%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull"
    "%2Ctrue%5D"
)
_RSC_LOGIN = (
    "%5B%22%22%2C%7B%22children%22%3A%5B%22(v2-auth)%22%2C%7B%22children%22"
    "%3A%5B%22login%22%2C%7B%22children%22%3A%5B%22email%22%2C%7B%22children%22"
    "%3A%5B%22__PAGE__%22%2C%7B%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2C"
    "null%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%2Ctrue%5D"
)
_RSC_ONBOARDING = (
    "%5B%22%22%2C%7B%22children%22%3A%5B%22(v2-auth)%22%2C%7B%22children%22"
    "%3A%5B%22onboarding%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2C"
    "null%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%2Ctrue%5D"
)
_RSC_API_KEYS = (
    "%5B%22%22%2C%7B%22children%22%3A%5B%22(console)%22%2C%7B%22children%22"
    "%3A%5B%22settings%22%2C%7B%22children%22%3A%5B%22users%22%2C%7B%22children%22"
    "%3A%5B%22api-keys%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2Cnull%2Cnull%5D%7D%2C"
    "null%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%2Ctrue%5D"
)
_RSC_BILLING = (
    "%5B%22%22%2C%7B%22children%22%3A%5B%22(console)%22%2C%7B%22children%22"
    "%3A%5B%22account%22%2C%7B%22children%22%3A%5B%22billing%22%2C%7B%22children%22"
    "%3A%5B%22__PAGE__%22%2C%7B%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2C"
    "null%2Cnull%5D%7D%2Cnull%2Cnull%2Ctrue%5D"
)

_KNOWN_ACTIONS: dict[str, str] = {
    "signup": "40f70e8f64fe25eef75d07d5425f93e2e5e3b469e3",
    "login": "40af31f0d373035b237acbdc32067e092c769ad4e9",
    "onboarding": "60ef813996d3b015bf7eb12a1e1eaac1d95a4fb0ff",
    # generateApiKey(name, undefined, setExpiration, undefined)
    "create_api_key": "783f7561ae446ec556e8373b49fe014d47afbc230d",
    # redeemCreditCodeAction(code) — adds promo credits to Prepaid Credits
    "redeem_credit_code": "7ff5d24595b984797aa61a904e1ce040e65d3c19d9",
}


def _server_action_headers(action_id: str, rsc_state: str, referer: str) -> dict[str, str]:
    """Next.js Server Action 必需的完整请求头。"""
    return {
        "Content-Type": "text/plain;charset=UTF-8",
        "Accept": "text/x-component",
        "next-action": action_id,
        "next-router-state-tree": rsc_state,
        "x-deployment-id": DEPLOY_ID,
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Referer": referer,
        "Origin": "https://app.fireworks.ai",
    }


class FireworksClient:
    """Fireworks AI Next.js 协议客户端。"""

    def __init__(
        self,
        timeout: float = 30.0,
        proxy: str | None = None,
        log_fn: Callable[[str], None] | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.timeout = timeout
        self.proxy = proxy
        self.log_fn = log_fn
        self.session = session or requests.Session()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        self.session.headers.update({
            "User-Agent": CHROME_UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            "sec-ch-ua": SEC_CH_UA,
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua-mobile": "?0",
        })

    def _log(self, message: str) -> None:
        if self.log_fn:
            self.log_fn(message)

    def _request(self, method: str, url: str, *, headers=None, json_body=None,
                 data=None, allow_redirects=True, label="", timeout=None) -> requests.Response:
        merged = dict(self.session.headers)
        if headers:
            merged.update(headers)
        _timeout = timeout if timeout is not None else self.timeout
        for attempt in range(4):
            try:
                if method == "GET":
                    resp = self.session.get(url, headers=merged, timeout=_timeout,
                                            allow_redirects=allow_redirects)
                else:
                    kwargs = {"headers": merged, "timeout": _timeout, "allow_redirects": allow_redirects}
                    if json_body is not None:
                        kwargs["json"] = json_body
                    if data is not None:
                        kwargs["data"] = data
                    resp = self.session.post(url, **kwargs)
                return resp
            except requests.RequestException:
                if attempt < 3:
                    wait = min(2 ** attempt + random.uniform(0, 1), 10)
                    self._log(f"Fireworks {label} retry {attempt+1}/4 in {wait:.1f}s")
                    time.sleep(wait)
                    continue
                raise

    def signup(self, email: str, password: str) -> dict[str, Any]:
        self._log(f"Fireworks signup: {email}")

        # 先访问 signup 页面获取 cookie（Next.js 需要）
        self._request("GET", urljoin(FIREWORKS_BASE, "/signup"), label="GET signup page").raise_for_status()

        action_id = _KNOWN_ACTIONS["signup"]
        body = json.dumps([{"email": email, "password": password}], separators=(",", ":"))
        headers = _server_action_headers(action_id, _RSC_SIGNUP, f"{FIREWORKS_BASE}/signup")

        resp = self._request("POST", urljoin(FIREWORKS_BASE, "/signup"),
                             headers=headers, data=body, label="signup")
        if resp.status_code != 200:
            raise RuntimeError(f"Fireworks signup HTTP {resp.status_code}: {str(resp.text)[:500]}")

        self._log("Fireworks signup 完成 → 等待邮箱验证")
        return {"email": email, "password": password, "status": "verification_sent"}

    def verify_email(self, verification_url: str) -> bool:
        self._log("Fireworks: 邮箱验证")
        parsed = urlparse(verification_url)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        user_name = params.get("user_name", "")
        code = params.get("confirmation_code", "")

        if user_name and code:
            self._log(f"Fireworks: Cognito ConfirmSignUp user={user_name[:20]}...")
            try:
                cognito_resp = self._request(
                    "POST",
                    f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/",
                    headers={
                        "X-Amz-Target": "AWSCognitoIdentityProviderService.ConfirmSignUp",
                        "Content-Type": "application/x-amz-json-1.1",
                    },
                    json_body={"ClientId": COGNITO_CLIENT, "Username": user_name, "ConfirmationCode": code},
                    label="cognito_confirm",
                )
                self._log(f"Fireworks Cognito: {cognito_resp.status_code}")
            except Exception as e:
                self._log(f"Fireworks Cognito 异常（继续）: {e}")

        resp = self._request("GET", verification_url, label="verify_email")
        self._log(f"Fireworks 验证完成 -> {resp.url}")
        return True

    def login(self, email: str, password: str) -> dict[str, Any]:
        self._log(f"Fireworks login: {email}")

        # 先访问登录页面获取 cookie
        self._request("GET", urljoin(FIREWORKS_BASE, "/login/email"),
                      label="GET login page").raise_for_status()

        action_id = _KNOWN_ACTIONS["login"]
        body = json.dumps([{"email": email, "password": password}], separators=(",", ":"))
        headers = _server_action_headers(
            action_id, _RSC_LOGIN,
            f"{FIREWORKS_BASE}/login/email?redirectURI=%2Faccount%2Fhome",
        )

        login_url = urljoin(FIREWORKS_BASE, "/login/email?redirectURI=%2Faccount%2Fhome")
        resp = self._request("POST", login_url, headers=headers, data=body, label="login")
        if resp.status_code != 200:
            raise RuntimeError(f"Fireworks login HTTP {resp.status_code}: {str(resp.text)[:500]}")

        cookies = requests.utils.dict_from_cookiejar(self.session.cookies)
        self._log("Fireworks login 完成")
        return {"email": email, "password": password, "cookies": cookies}

    def onboarding(self, account_id: str, first_name: str = "Auto",
                   last_name: str = "Register", company_name: str = "",
                   goals: list[int] | None = None, use_cases: list[int] | None = None,
                   other_goal: str = "", other_use_cases: str = "",
                   skip_questionnaire: bool = False) -> dict[str, Any]:
        """提交 onboarding questionnaire（multipart + RSC `$W` 引用语法）。

        必须用 multipart/form-data，goals/useCases 通过 `$W1`/`$W2` 指向
        multipart 中的 name="1"/name="2" 字段（值为 JSON 数组）。
        如果传空数组（即跳过问卷），server 不会发放 $5 promotional credit。
        默认 goals=[1] use_cases=[1] 触发 credit 到账。
        """
        self._log(f"Fireworks onboarding: accountId={account_id}")

        self._request("GET", urljoin(FIREWORKS_BASE, "/onboarding"),
                      label="GET onboarding page").raise_for_status()

        # 默认勾选 goals=[5,6,7] (Fine-tune, Reliability, Migrate-to-open) 与
        # use_cases=[2,3] (Conversational AI, Agentic AI)，与 web UI "Submit to get
        # $5 Credits" 一致。indices 为 1-based，对应 onboarding 页面的 checkbox 顺序。
        if goals is None:
            goals = [5, 6, 7]
        if use_cases is None:
            use_cases = [2, 3]

        action_id = _KNOWN_ACTIONS["onboarding"]
        form_data = {
            "accountId": account_id,
            "companyName": company_name,
            "firstName": first_name,
            "lastName": last_name,
            "agreeToTerms": True,
            "step": "questionnaire",
            "goals": "$W1",       # RSC 引用 -> multipart name="1"
            "useCases": "$W2",    # RSC 引用 -> multipart name="2"
            "otherGoal": other_goal,
            "otherUseCases": other_use_cases,
        }
        outer = f"[{json.dumps(form_data, separators=(',', ':'))}," \
                f"{'true' if skip_questionnaire else 'false'}]"
        # multipart 字段顺序与名字必须严格匹配 Next.js RSC 期望
        files = {
            "1": (None, json.dumps(goals, separators=(",", ":")), "text/plain"),
            "2": (None, json.dumps(use_cases, separators=(",", ":")), "text/plain"),
            "0": (None, outer, "text/plain"),
        }
        # 不能预设 Content-Type，requests 要自己生成包含 boundary 的头
        headers = _server_action_headers(action_id, _RSC_ONBOARDING, f"{FIREWORKS_BASE}/onboarding")
        headers.pop("Content-Type", None)

        resp = None
        last_error = None
        for attempt in range(4):
            try:
                # 用 session.post 直接传 files= 让 requests 处理 multipart
                merged_headers = dict(self.session.headers)
                merged_headers.update(headers)
                resp = self.session.post(
                    urljoin(FIREWORKS_BASE, "/onboarding"),
                    headers=merged_headers, files=files,
                    timeout=self.timeout * 4, allow_redirects=True,
                )
                if resp.status_code == 200:
                    break
                if resp.status_code in (504, 502, 503):
                    self._log(f"Fireworks onboarding {resp.status_code} (attempt {attempt+1}/4), retrying...")
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"Fireworks onboarding HTTP {resp.status_code}: {str(resp.text)[:500]}")
            except requests.RequestException as e:
                last_error = e
                if attempt < 3:
                    wait = min(2 ** attempt + random.uniform(0, 1), 10)
                    self._log(f"Fireworks onboarding retry {attempt+1}/4 in {wait:.1f}s")
                    time.sleep(wait)
                    continue
                raise

        if resp is None and last_error:
            raise last_error
        if resp is None or resp.status_code != 200:
            raise RuntimeError(
                f"Fireworks onboarding HTTP {resp.status_code if resp else 'N/A'}: "
                f"{str(resp.text)[:500] if resp else 'no response'}"
            )

        # Check user_context cookie for account ID
        uc_raw = self.session.cookies.get("auth_v2_user_context", "")
        account_info: dict[str, Any] = {"success": False}
        if uc_raw:
            try:
                uc = json.loads(requests.utils.unquote(uc_raw))
                account_info["success"] = uc.get("hasAccount", False)
                account_info["account_id"] = uc.get("accountID", "")
                account_info["account_state"] = uc.get("accountState", 0)
                self._log(f"Fireworks onboarding → accountID={uc.get('accountID')} hasAccount={uc.get('hasAccount')}")
            except Exception:
                pass

        if not account_info.get("success"):
            self._log("Fireworks onboarding: 响应中未检测到 hasAccount")

        return account_info

    def _get_access_token(self) -> str | None:
        """从 cookie 中提取 access token (JWT)。"""
        raw = self.session.cookies.get("auth_v2_access_token", "")
        if raw:
            return raw
        # 也尝试从 auth_v2_id_token 提取
        return self.session.cookies.get("auth_v2_id_token", "") or None

    def _parse_jwt_payload(self, token: str) -> dict[str, Any]:
        """解析 JWT payload（不验证签名）。"""
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        try:
            padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
            decoded = base64.urlsafe_b64decode(padded)
            return json.loads(decoded)
        except Exception:
            return {}

    def get_account_info(self) -> dict[str, Any]:
        self._log("Fireworks: 获取 account info")

        info: dict[str, Any] = {}

        # 从 cookie 中读取 account_id
        uc_raw = self.session.cookies.get("auth_v2_user_context", "")
        if uc_raw:
            try:
                uc = json.loads(requests.utils.unquote(uc_raw))
                info["account_id"] = uc.get("accountID", "")
                info["has_account"] = uc.get("hasAccount", False)
                self._log(f"  cookie account_id={info['account_id']}")
            except Exception:
                pass

        # 从 JWT access token 提取 user_id (sub claim)
        access_token = self._get_access_token()
        if access_token:
            payload = self._parse_jwt_payload(access_token)
            user_id = payload.get("sub", "")
            if user_id:
                info["user_id"] = user_id
                self._log(f"  JWT user_id={user_id}")

        return info

    def create_api_key(self, name: str = "default") -> dict[str, Any]:
        """通过 Next.js Server Action 创建 API key（全 HTTP，无浏览器）。

        对应 web UI: Create API Key → API Key → Generate Key。
        Action body: [name, undefined, setExpiration, undefined]
        Response: 0:RSC frame; 1:{"keyId":"key_xxx","key":"fw_xxx"}
        """
        self._log(f"Fireworks: 创建 API key name={name}")

        # 预热页面，刷新 cookie 与 RSC route state
        self._request(
            "GET",
            urljoin(FIREWORKS_BASE, "/settings/users/api-keys"),
            label="GET api-keys page",
        )

        action_id = _KNOWN_ACTIONS["create_api_key"]
        headers = _server_action_headers(
            action_id, _RSC_API_KEYS,
            urljoin(FIREWORKS_BASE, "/settings/users/api-keys"),
        )
        # Next.js RSC 把 undefined 序列化成字符串 "$undefined"
        body = json.dumps([name, "$undefined", False, "$undefined"], separators=(",", ":"))

        resp = self._request(
            "POST",
            urljoin(FIREWORKS_BASE, "/settings/users/api-keys"),
            headers=headers, data=body, label="create_api_key",
        )
        if resp.status_code != 200:
            self._log(
                f"Fireworks API key 创建失败 HTTP {resp.status_code}: "
                f"{str(resp.text)[:200]}"
            )
            return {"api_key": "", "error": f"HTTP {resp.status_code}"}

        api_key_value = ""
        key_id = ""
        # RSC 响应每行一条 "<idx>:<json>"，目标字段在 idx=1 行
        for line in resp.text.splitlines():
            if not line or ":" not in line:
                continue
            _, _, payload = line.partition(":")
            payload = payload.strip()
            if not payload.startswith("{"):
                continue
            try:
                data = json.loads(payload)
            except Exception:
                continue
            if isinstance(data, dict) and data.get("key", "").startswith("fw_"):
                api_key_value = data.get("key", "")
                key_id = data.get("keyId", "")
                break

        if not api_key_value:
            self._log(f"Fireworks API key 响应未含 fw_ 字段: {resp.text[:300]}")
            return {"api_key": "", "error": "no key in response"}

        self._log(f"Fireworks API key 创建成功: {api_key_value[:16]}... id={key_id}")
        return {
            "key_id": key_id,
            "display_name": name,
            "api_key": api_key_value,
            "prefix": api_key_value[:8],
        }

    def redeem_credit_code(self, code: str) -> dict[str, Any]:
        """兑换 fireworks 信用代码 -> Prepaid Credits（全 HTTP）。

        对应 web UI 的 Billing → Redeem Promo 弹窗。Server action 名为
        `redeemCreditCodeAction`，body 为 `[code]`，响应行 1 形如：
          {"amount":"<cents>"} 或 {"error":"credit code X not found"}。

        Google OAuth 新用户的 $5 credit 不通过该端点发放（由 server 在
        OAuth callback 内部 grant），因此邮箱注册账号要拿到 credit 必须
        通过真实的 promo code 调用此方法。
        """
        self._log(f"Fireworks: 兑换 credit code {code}")
        # 预热 billing 页面，刷新 cookie / RSC state
        self._request("GET", urljoin(FIREWORKS_BASE, "/account/billing"),
                      label="GET billing page")

        action_id = _KNOWN_ACTIONS["redeem_credit_code"]
        headers = _server_action_headers(
            action_id, _RSC_BILLING,
            urljoin(FIREWORKS_BASE, "/account/billing"),
        )
        body = json.dumps([code], separators=(",", ":"))

        resp = self._request(
            "POST",
            urljoin(FIREWORKS_BASE, "/account/billing"),
            headers=headers, data=body, label="redeem_credit_code",
        )
        if resp.status_code != 200:
            return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

        amount = ""
        error_msg = ""
        for line in resp.text.splitlines():
            if not line or ":" not in line:
                continue
            _, _, payload = line.partition(":")
            payload = payload.strip()
            if not payload.startswith("{"):
                continue
            try:
                data = json.loads(payload)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("error"):
                error_msg = data["error"]
                break
            if "amount" in data:
                amount = str(data.get("amount", ""))
                break

        if error_msg:
            self._log(f"Fireworks credit 兑换失败: {error_msg}")
            return {"success": False, "error": error_msg, "code": code}

        self._log(f"Fireworks credit 兑换成功 amount={amount}")
        return {"success": True, "amount": amount, "code": code}