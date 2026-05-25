from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DIR = ROOT / "artifacts" / "zo_openai_proxy_source"
DEFAULT_RESULT_PATH = ROOT / "output" / "zo_e2e_result.json"
DEFAULT_OUTPUT_PATH = ROOT / "output" / "zo_proxy_fast_deploy.json"
API_BASE = "https://api.zo.computer"
SITE_ORIGIN = "https://www.zo.computer"
DEFAULT_PERSONA_NAME = "API Passthrough"

SOURCE_URLS: dict[Path, str] = {
    Path("README.md"): "https://zo.pub/azurerune/zo-openai-proxy/README.md",
    Path("persona") / "api-passthrough.md": "https://zo.pub/azurerune/zo-openai-proxy/persona/api-passthrough.md",
    Path("routes") / "zo-chat-completions.ts": "https://zo.pub/azurerune/zo-openai-proxy/routes/zo-chat-completions.ts",
    Path("routes") / "zo-models.ts": "https://zo.pub/azurerune/zo-openai-proxy/routes/zo-models.ts",
    Path("routes") / "anthropic-chat-completions.ts": "https://zo.pub/azurerune/zo-openai-proxy/routes/anthropic-chat-completions.ts",
    Path("routes") / "anthropic-models.ts": "https://zo.pub/azurerune/zo-openai-proxy/routes/anthropic-models.ts",
}


def _ensure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _mask_token(value: str, *, prefix: int = 10, suffix: int = 6) -> str:
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= prefix + suffix + 3:
        return "***"
    return f"{value[:prefix]}...{value[-suffix:]}"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_handle(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = parsed.hostname or text
    for suffix in (".zo.computer", ".zo.space"):
        if host.endswith(suffix):
            return host[: -len(suffix)]
    return "".join(ch for ch in text if ch.isalnum())[:30]


def _first_string(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _find_nested_strings(data: Any, names: set[str]) -> Iterable[str]:
    if isinstance(data, dict):
        for key, value in data.items():
            if key in names and isinstance(value, str) and value.strip():
                yield value.strip()
            yield from _find_nested_strings(value, names)
    elif isinstance(data, list):
        for item in data:
            yield from _find_nested_strings(item, names)


def _extract_api_key(data: dict[str, Any]) -> str:
    direct = _first_string(
        data.get("api_key"),
        data.get("ai_api_token"),
        (data.get("key_create_result") or {}).get("api_key") if isinstance(data.get("key_create_result"), dict) else "",
        (data.get("api_key_info") or {}).get("key") if isinstance(data.get("api_key_info"), dict) else "",
    )
    if direct:
        return direct
    for value in _find_nested_strings(data, {"api_key", "ai_api_token", "key"}):
        if value.startswith("zo_sk_"):
            return value
    return ""


def _extract_workspace(data: dict[str, Any]) -> tuple[str, str]:
    candidates: list[Any] = []
    for key in ("workspace", "workspace_result", "create_result"):
        value = data.get(key)
        if isinstance(value, dict):
            candidates.append(value)
            nested = value.get("workspace")
            if isinstance(nested, dict):
                candidates.append(nested)
            nested = value.get("create_result")
            if isinstance(nested, dict):
                candidates.append(nested)
    for item in candidates:
        handle = _normalize_handle(item.get("handle") or "")
        origin = _first_string(item.get("origin"), item.get("workspace_origin"), item.get("workspace_url"), item.get("url"))
        if not handle and origin:
            handle = _normalize_handle(origin)
        if handle:
            return handle, origin.rstrip("/") if origin else f"https://{handle}.zo.computer"

    for value in _find_nested_strings(data, {"workspace_origin", "workspace_url", "origin", "url"}):
        handle = _normalize_handle(value)
        if handle:
            return handle, f"https://{handle}.zo.computer"
    return "", ""


@dataclass(frozen=True)
class ZoProxyDeployContext:
    result_path: Path
    handle: str
    workspace_origin: str
    access_token: str
    refresh_token: str
    api_key: str
    cookies: dict[str, str]

    @property
    def base_url(self) -> str:
        return f"https://{self.handle}.zo.space/v1/{self.api_key}" if self.handle and self.api_key else ""

    @property
    def base_url_preview(self) -> str:
        return f"https://{self.handle}.zo.space/v1/{_mask_token(self.api_key)}" if self.handle and self.api_key else ""

    @property
    def cookie_header(self) -> str:
        return "; ".join(f"{name}={value}" for name, value in self.cookies.items() if value)


def load_result_context(result_path: Path) -> ZoProxyDeployContext:
    result_path = Path(result_path)
    data = _read_json(result_path)
    if not isinstance(data, dict):
        raise RuntimeError(f"结果文件不是 JSON object: {result_path}")
    cookies = dict(data.get("cookies") or {})
    access_token = _first_string(cookies.get("access_token"), data.get("access_token"))
    refresh_token = _first_string(cookies.get("refresh_token"), data.get("refresh_token"))
    api_key = _extract_api_key(data)
    handle, origin = _extract_workspace(data)
    return ZoProxyDeployContext(
        result_path=result_path,
        handle=handle,
        workspace_origin=origin,
        access_token=access_token,
        refresh_token=refresh_token,
        api_key=api_key,
        cookies=cookies,
    )


def patch_zo_chat_route(source: str, persona_id: str) -> str:
    if not persona_id:
        raise RuntimeError("persona_id 为空")
    patched = source.replace("PERSONA_ID_PLACEHOLDER", persona_id)

    # 源仓库当前版本里直接使用 maxTokens，但没有定义；这里在同步前修补，避免 Space 运行时 500。
    if "const maxTokens =" not in patched:
        marker = "  const wantStream = body.stream === true;\n"
        max_tokens_block = (
            "  const maxTokens =\n"
            "    typeof body.max_completion_tokens === \"number\"\n"
            "      ? body.max_completion_tokens\n"
            "      : typeof body.max_tokens === \"number\"\n"
            "        ? body.max_tokens\n"
            "        : 64_000;\n"
        )
        if marker not in patched:
            raise RuntimeError("无法定位 wantStream 行，不能安全插入 maxTokens")
        patched = patched.replace(marker, marker + max_tokens_block, 1)

    # 使用常量，避免替换后出现两份硬编码 persona id，后续审计也更直观。
    patched = patched.replace(f'persona_id: "{persona_id}"', "persona_id: PERSONA_ID")
    return patched


def build_route_subset(source_dir: Path, persona_id: str) -> list[dict[str, Any]]:
    source_dir = Path(source_dir)
    routes_dir = source_dir / "routes"
    route_specs = [
        ("/v1/:token/chat/completions", routes_dir / "zo-chat-completions.ts", True),
        ("/v1/:token/models", routes_dir / "zo-models.ts", False),
        ("/anthropic/:apikey/v1/chat/completions", routes_dir / "anthropic-chat-completions.ts", False),
        ("/anthropic/:apikey/v1/models", routes_dir / "anthropic-models.ts", False),
    ]
    subset: list[dict[str, Any]] = []
    for path, file_path, needs_persona in route_specs:
        code = file_path.read_text(encoding="utf-8")
        if needs_persona:
            code = patch_zo_chat_route(code, persona_id)
        subset.append({"path": path, "type": "api", "public": True, "code": code})
    return subset


def fetch_source(source_dir: Path, *, timeout: float = 30.0) -> dict[str, Any]:
    source_dir = Path(source_dir)
    session = requests.Session()
    written: list[str] = []
    for rel_path, url in SOURCE_URLS.items():
        response = session.get(url, timeout=timeout)
        response.raise_for_status()
        target = source_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(response.text, encoding="utf-8")
        written.append(str(target.relative_to(ROOT)) if target.is_relative_to(ROOT) else str(target))
    return {"ok": True, "source_dir": str(source_dir), "files": written}


def _request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    timeout: float,
    **kwargs: Any,
) -> tuple[requests.Response, Any]:
    response = session.request(method, url, headers=headers, timeout=timeout, **kwargs)
    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text[:4000]}
    return response, data


def _workspace_headers(ctx: ZoProxyDeployContext, *, json_content: bool = True) -> dict[str, str]:
    if not ctx.access_token:
        raise RuntimeError("缺少 Zo 登录 access_token cookie，不能调用 workspace 接口")
    if not ctx.handle:
        raise RuntimeError("缺少 Zo workspace handle，不能调用 workspace 接口")
    origin = ctx.workspace_origin or f"https://{ctx.handle}.zo.computer"
    headers = {
        "Authorization": f"Bearer {ctx.access_token}",
        "Origin": origin,
        "Referer": f"{origin}/",
        "X-Zo-Workspace-Origin": origin,
        "x-zo-host-key": ctx.handle,
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "Mozilla/5.0 ZoProxyFastDeploy/1.0",
    }
    if ctx.cookie_header:
        headers["Cookie"] = ctx.cookie_header
    if json_content:
        headers["Content-Type"] = "application/json"
    return headers


def resolve_workspace_from_login_state(ctx: ZoProxyDeployContext, session: requests.Session, *, timeout: float = 30.0) -> ZoProxyDeployContext:
    if ctx.handle:
        return ctx
    if not ctx.cookie_header:
        return ctx
    headers = {
        "Origin": SITE_ORIGIN,
        "Referer": f"{SITE_ORIGIN}/",
        "Accept": "application/json",
        "Cookie": ctx.cookie_header,
        "User-Agent": "Mozilla/5.0 ZoProxyFastDeploy/1.0",
    }
    if ctx.access_token:
        headers["Authorization"] = f"Bearer {ctx.access_token}"
    response, data = _request_json(session, "GET", f"{SITE_ORIGIN}/api/login-state", headers=headers, timeout=timeout)
    if not response.ok or not isinstance(data, dict):
        return ctx
    workspaces = data.get("workspaces")
    if isinstance(workspaces, list) and workspaces:
        first = workspaces[0] if isinstance(workspaces[0], dict) else {}
        handle = _normalize_handle(first.get("handle") or first.get("url") or "")
        if handle:
            origin = _first_string(first.get("url"), f"https://{handle}.zo.computer").rstrip("/")
            return replace(ctx, handle=handle, workspace_origin=origin)
    return ctx


def create_or_update_persona(
    ctx: ZoProxyDeployContext,
    source_dir: Path,
    *,
    session: requests.Session,
    name: str = DEFAULT_PERSONA_NAME,
    persona_id: str = "",
    timeout: float = 30.0,
) -> dict[str, Any]:
    prompt_path = Path(source_dir) / "persona" / "api-passthrough.md"
    prompt = prompt_path.read_text(encoding="utf-8")
    headers = _workspace_headers(ctx)
    attempts: list[dict[str, Any]] = []

    if not persona_id:
        response, data = _request_json(session, "GET", f"{API_BASE}/personas/", headers=headers, timeout=timeout)
        attempts.append({"method": "GET", "path": "/personas/", "status": response.status_code, "ok": response.ok})
        if response.ok and isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("name") == name and item.get("id"):
                    persona_id = str(item["id"])
                    break

    payload = {"name": name, "prompt": prompt, "scopes": []}
    if persona_id:
        response, data = _request_json(session, "PUT", f"{API_BASE}/personas/{persona_id}", headers=headers, timeout=timeout, json=payload)
        attempts.append({"method": "PUT", "path": f"/personas/{persona_id}", "status": response.status_code, "ok": response.ok})
        if response.ok:
            return {"ok": True, "persona_id": persona_id, "source": "update", "data": data, "attempts": attempts}

    create_payload = {"name": name, "prompt": prompt}
    response, data = _request_json(session, "POST", f"{API_BASE}/personas/", headers=headers, timeout=timeout, json=create_payload)
    attempts.append({"method": "POST", "path": "/personas/", "status": response.status_code, "ok": response.ok})
    if not response.ok or not isinstance(data, dict) or not data.get("id"):
        raise RuntimeError(f"创建 persona 失败: status={response.status_code} body={str(data)[:1200]}")
    persona_id = str(data["id"])
    response2, data2 = _request_json(session, "PUT", f"{API_BASE}/personas/{persona_id}", headers=headers, timeout=timeout, json=payload)
    attempts.append({"method": "PUT", "path": f"/personas/{persona_id}", "status": response2.status_code, "ok": response2.ok})
    if not response2.ok:
        raise RuntimeError(f"清空 persona scopes 失败: status={response2.status_code} body={str(data2)[:1200]}")
    return {"ok": True, "persona_id": persona_id, "source": "create", "data": data2, "attempts": attempts}


def sync_routes(
    ctx: ZoProxyDeployContext,
    routes: list[dict[str, Any]],
    *,
    session: requests.Session,
    timeout: float = 180.0,
) -> dict[str, Any]:
    subset_json = json.dumps(routes, ensure_ascii=False, separators=(",", ":"))
    encoded = base64.b64encode(subset_json.encode("utf-8")).decode("ascii")
    remote_id = uuid.uuid4().hex
    b64_path = f"/tmp/zo-openai-proxy-routes-{remote_id}.b64"
    json_path = f"/tmp/zo-openai-proxy-routes-{remote_id}.json"
    command = (
        "set -e\n"
        f"cat > {b64_path} <<'EOF_ZO_PROXY_ROUTES'\n"
        f"{encoded}\n"
        "EOF_ZO_PROXY_ROUTES\n"
        f"base64 -d {b64_path} > {json_path}\n"
        f"bun /__substrate/space-sync.ts --subset {json_path}\n"
    )
    response, data = _request_json(
        session,
        "POST",
        f"{API_BASE}/exec",
        headers=_workspace_headers(ctx),
        timeout=timeout,
        json={"command": command},
    )
    if not response.ok:
        raise RuntimeError(f"/exec 同步路由失败: status={response.status_code} body={str(data)[:1200]}")
    returncode = data.get("returncode") if isinstance(data, dict) else None
    stdout = str(data.get("stdout") or "") if isinstance(data, dict) else ""
    stderr = str(data.get("stderr") or "") if isinstance(data, dict) else ""
    if returncode not in (0, None):
        raise RuntimeError(f"space-sync 失败: returncode={returncode} stderr={stderr[:1200]} stdout={stdout[:1200]}")
    return {"ok": True, "status": response.status_code, "returncode": returncode, "stdout": stdout[-4000:], "stderr": stderr[-2000:]}


def verify_proxy(ctx: ZoProxyDeployContext, *, model: str = "zo:openai/gpt-5.5", chat: bool = True, timeout: float = 90.0) -> dict[str, Any]:
    if not ctx.api_key:
        return {"ok": False, "reason": "missing_api_key"}
    session = requests.Session()
    base_url = ctx.base_url.rstrip("/")
    headers = {"Accept": "application/json", "User-Agent": "python-requests zo-proxy-verify"}
    models_response = session.get(f"{base_url}/models", headers=headers, timeout=timeout)
    models_text = models_response.text[:4000]
    result: dict[str, Any] = {
        "base_url": ctx.base_url,
        "base_url_preview": ctx.base_url_preview,
        "models": {"ok": models_response.ok, "status": models_response.status_code, "text": models_text},
    }
    if chat:
        chat_payload = {
            "model": model,
            "messages": [{"role": "user", "content": "只回复 SYNC_OK"}],
            "stream": False,
            "max_tokens": 32,
        }
        chat_response = session.post(
            f"{base_url}/chat/completions",
            headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "python-requests zo-proxy-verify"},
            json=chat_payload,
            timeout=timeout,
        )
        result["chat"] = {"ok": chat_response.ok, "status": chat_response.status_code, "text": chat_response.text[:4000]}
    result["ok"] = bool(result["models"].get("ok") and (not chat or result.get("chat", {}).get("ok")))
    return result


def deploy_proxy(
    ctx: ZoProxyDeployContext,
    source_dir: Path,
    *,
    persona_id: str = "",
    persona_name: str = DEFAULT_PERSONA_NAME,
    verify: bool = True,
    verify_chat: bool = True,
    verify_model: str = "zo:openai/gpt-5.5",
    timeout: float = 180.0,
) -> dict[str, Any]:
    session = requests.Session()
    ctx = resolve_workspace_from_login_state(ctx, session, timeout=min(timeout, 30.0))
    if not ctx.handle:
        raise RuntimeError("无法解析 workspace handle；请传 --handle 或确保 result JSON 内有 cookies/workspace_result")
    if not ctx.workspace_origin:
        ctx = replace(ctx, workspace_origin=f"https://{ctx.handle}.zo.computer")
    if not ctx.api_key:
        raise RuntimeError("无法解析 Zo API key；请传 --api-key 或使用包含 api_key 的 result JSON")

    persona_result = create_or_update_persona(
        ctx,
        source_dir,
        session=session,
        name=persona_name,
        persona_id=persona_id,
        timeout=min(timeout, 60.0),
    )
    actual_persona_id = str(persona_result.get("persona_id") or "")
    routes = build_route_subset(source_dir, actual_persona_id)
    sync_result = sync_routes(ctx, routes, session=session, timeout=timeout)
    verify_result = verify_proxy(ctx, model=verify_model, chat=verify_chat, timeout=min(timeout, 120.0)) if verify else {"ok": True, "skipped": True}
    return {
        "ok": bool(sync_result.get("ok") and verify_result.get("ok")),
        "saved_at": int(time.time()),
        "handle": ctx.handle,
        "workspace_origin": ctx.workspace_origin,
        "persona_id": actual_persona_id,
        "persona_result": persona_result,
        "sync_result": sync_result,
        "verify_result": verify_result,
        "base_url": ctx.base_url,
        "base_url_preview": ctx.base_url_preview,
        "models_url": f"{ctx.base_url}/models" if ctx.base_url else "",
        "chat_url": f"{ctx.base_url}/chat/completions" if ctx.base_url else "",
    }


def _apply_cli_overrides(ctx: ZoProxyDeployContext, args: argparse.Namespace) -> ZoProxyDeployContext:
    cookies = dict(ctx.cookies)
    if args.access_token:
        cookies["access_token"] = args.access_token
    if args.refresh_token:
        cookies["refresh_token"] = args.refresh_token
    access = _first_string(args.access_token, cookies.get("access_token"), ctx.access_token)
    refresh = _first_string(args.refresh_token, cookies.get("refresh_token"), ctx.refresh_token)
    handle = _normalize_handle(args.handle) or ctx.handle
    origin = _first_string(args.workspace_origin, ctx.workspace_origin, f"https://{handle}.zo.computer" if handle else "")
    api_key = _first_string(args.api_key, ctx.api_key)
    return ZoProxyDeployContext(
        result_path=ctx.result_path,
        handle=handle,
        workspace_origin=origin.rstrip("/") if origin else "",
        access_token=access,
        refresh_token=refresh,
        api_key=api_key,
        cookies=cookies,
    )


def main() -> None:
    _ensure_utf8_stdio()
    parser = argparse.ArgumentParser(description="快速把 zo-openai-proxy 通过 Zo HTTP /exec 同步到当前账号的 zo.space")
    parser.add_argument("--result-path", type=Path, default=DEFAULT_RESULT_PATH, help="Zo 注册结果 JSON，默认 output/zo_e2e_result.json")
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR, help="zo-openai-proxy 源码目录")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH, help="部署结果保存路径")
    parser.add_argument("--refresh-source", action="store_true", help="从 zo.pub 重新拉取 6 个源文件")
    parser.add_argument("--handle", default="", help="workspace handle；缺省从 result JSON 或 login-state 解析")
    parser.add_argument("--workspace-origin", default="", help="workspace origin，例如 https://handle.zo.computer")
    parser.add_argument("--access-token", default="", help="Zo 登录态 access_token cookie；缺省从 result JSON 读取")
    parser.add_argument("--refresh-token", default="", help="Zo 登录态 refresh_token cookie；仅用于补齐 Cookie header")
    parser.add_argument("--api-key", default="", help="Zo access token / API key；缺省从 result JSON 读取")
    parser.add_argument("--persona-id", default="", help="复用指定 persona_id；缺省按名称复用或创建")
    parser.add_argument("--persona-name", default=DEFAULT_PERSONA_NAME)
    parser.add_argument("--verify-model", default="zo:openai/gpt-5.5")
    parser.add_argument("--skip-verify", action="store_true")
    parser.add_argument("--skip-chat-verify", action="store_true")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    if args.refresh_source:
        fetch_info = fetch_source(args.source_dir)
        print(json.dumps({"refresh_source": fetch_info}, ensure_ascii=False, indent=2), flush=True)

    ctx = load_result_context(args.result_path)
    ctx = _apply_cli_overrides(ctx, args)
    result = deploy_proxy(
        ctx,
        args.source_dir,
        persona_id=args.persona_id,
        persona_name=args.persona_name,
        verify=not args.skip_verify,
        verify_chat=not args.skip_chat_verify,
        verify_model=args.verify_model,
        timeout=args.timeout,
    )
    _write_json(args.output, result)
    summary = {
        "ok": result.get("ok"),
        "handle": result.get("handle"),
        "persona_id": result.get("persona_id"),
        "base_url_preview": result.get("base_url_preview"),
        "models_ok": (result.get("verify_result") or {}).get("models", {}).get("ok"),
        "chat_ok": (result.get("verify_result") or {}).get("chat", {}).get("ok"),
        "output": str(args.output),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if not result.get("ok"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
