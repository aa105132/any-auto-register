from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from services.twoapi.manager import get_twoapi_manager

management_router = APIRouter(prefix="/2api", tags=["2api"])
proxy_router = APIRouter(prefix="/zo/v1", tags=["2api-proxy"])


class TwoAPIKeyCreateRequest(BaseModel):
    plugin: str = "zo"
    note: str = ""


class TwoAPISettingsRequest(BaseModel):
    enabled: bool | None = None
    min_credit: float | None = None
    auto_wake: bool | None = None
    auto_refill: bool | None = None
    request_timeout: float | None = None
    wake_timeout: float | None = None
    max_retries: int | None = None


@management_router.get("/status")
def get_status():
    return get_twoapi_manager().status()


@management_router.get("/plugins")
def list_plugins():
    return {"items": get_twoapi_manager().list_plugins()}


@management_router.get("/logs")
def list_logs(plugin: str = "zo", limit: int = 200):
    manager = get_twoapi_manager()
    try:
        item = manager.get_plugin(plugin)
    except KeyError as exc:
        raise HTTPException(404, "插件不存在") from exc
    return {"plugin": plugin, "items": item.recent_logs(limit=limit)}


@management_router.post("/plugins/{plugin}/refresh-credits")
def refresh_plugin_credits(plugin: str):
    manager = get_twoapi_manager()
    try:
        item = manager.get_plugin(plugin)
    except KeyError as exc:
        raise HTTPException(404, "插件不存在") from exc
    accounts = item.refresh_credits() if hasattr(item, "refresh_credits") else item.load_accounts()
    return {"plugin": plugin, "accounts": [account.to_public() for account in accounts]}


@management_router.post("/plugins/{plugin}/recover")
def recover_plugin_proxy(plugin: str):
    manager = get_twoapi_manager()
    try:
        item = manager.get_plugin(plugin)
    except KeyError as exc:
        raise HTTPException(404, "插件不存在") from exc
    result = item.keepalive_once() if hasattr(item, "keepalive_once") else {}
    return {"plugin": plugin, "result": result}


@management_router.get("/keys")
def list_keys():
    return {"items": get_twoapi_manager().list_keys()}


@management_router.post("/keys")
def create_key(body: TwoAPIKeyCreateRequest):
    return get_twoapi_manager().create_key(plugin=body.plugin, note=body.note)


@management_router.delete("/keys/{key_id}")
def delete_key(key_id: str):
    ok = get_twoapi_manager().delete_key(key_id)
    if not ok:
        raise HTTPException(404, "API Key 不存在")
    return {"ok": True}


@management_router.get("/settings")
def get_settings():
    return get_twoapi_manager().settings.__dict__


@management_router.post("/settings")
def save_settings(body: TwoAPISettingsRequest):
    data = {key: value for key, value in body.model_dump().items() if value is not None}
    return get_twoapi_manager().save_settings(data)


def _openai_error(message: str, status_code: int = 400, code: str = "twoapi_error") -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": "invalid_request_error", "code": code}},
    )


def _extract_bearer(authorization: str = "") -> str:
    text = str(authorization or "").strip()
    if text.lower().startswith("bearer "):
        return text.split(" ", 1)[1].strip()
    return text


def _require_key(path_token: str = "", authorization: str = "", *, plugin: str = "zo") -> None:
    token = path_token.strip() or _extract_bearer(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="missing_twoapi_key")
    if not get_twoapi_manager().verify_key(token, plugin=plugin):
        raise HTTPException(status_code=401, detail="invalid_twoapi_key")


def _upstream_media_type(upstream: Any) -> str:
    try:
        return str(getattr(upstream, "headers", {}).get("content-type") or "application/json").split(";", 1)[0]
    except Exception:
        return "application/json"


def _iter_upstream_bytes(upstream: Any):
    try:
        iterator = getattr(upstream, "iter_content", None)
        if callable(iterator):
            for chunk in iterator(chunk_size=None):
                if not chunk:
                    continue
                yield chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8")
            return
        line_iterator = getattr(upstream, "iter_lines", None)
        if callable(line_iterator):
            for line in line_iterator(decode_unicode=False):
                yield line if isinstance(line, bytes) else str(line).encode("utf-8")
                yield b"\n"
            return
        content = getattr(upstream, "content", None)
        if content is not None:
            yield content if isinstance(content, bytes) else str(content).encode("utf-8")
            return
        text = str(getattr(upstream, "text", "") or "")
        if text:
            yield text.encode("utf-8")
    finally:
        close = getattr(upstream, "close", None)
        if callable(close):
            close()


def _stream_headers(upstream: Any) -> dict[str, str]:
    source = getattr(upstream, "headers", {}) or {}
    headers: dict[str, str] = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    for key in ("cache-control", "x-request-id", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"):
        try:
            value = source.get(key) or source.get(key.title())
        except Exception:
            value = ""
        if value:
            headers[key] = str(value)
    return headers


def _response_from_upstream(upstream: Any, *, stream: bool = False) -> Response:
    media_type = _upstream_media_type(upstream)
    status_code = int(getattr(upstream, "status_code", 200) or 200)
    if stream or media_type == "text/event-stream":
        return StreamingResponse(
            _iter_upstream_bytes(upstream),
            status_code=status_code,
            media_type=media_type or "text/event-stream",
            headers=_stream_headers(upstream),
        )
    content = getattr(upstream, "content", None)
    if content is None:
        content = str(getattr(upstream, "text", "") or "").encode("utf-8")
    return Response(content=content, status_code=status_code, media_type=media_type)


def _handle_http_exception(exc: HTTPException) -> JSONResponse:
    message = "缺少 2API Key" if exc.detail == "missing_twoapi_key" else "2API Key 无效"
    return _openai_error(message, status_code=int(exc.status_code or 401), code=str(exc.detail or "auth_error"))


@proxy_router.get("/models")
def zo_models(authorization: str = Header(default="")):
    try:
        _require_key(authorization=authorization)
        upstream = get_twoapi_manager().get_plugin("zo").forward_models()
        return _response_from_upstream(upstream)
    except HTTPException as exc:
        return _handle_http_exception(exc)
    except Exception as exc:
        return _openai_error(str(exc), status_code=503, code="no_available_account")


@proxy_router.get("/{path_token}/models")
def zo_models_with_token(path_token: str, authorization: str = Header(default="")):
    try:
        _require_key(path_token=path_token, authorization=authorization)
        upstream = get_twoapi_manager().get_plugin("zo").forward_models()
        return _response_from_upstream(upstream)
    except HTTPException as exc:
        return _handle_http_exception(exc)
    except Exception as exc:
        return _openai_error(str(exc), status_code=503, code="no_available_account")


@proxy_router.post("/chat/completions")
async def zo_chat(request: Request, authorization: str = Header(default="")):
    try:
        _require_key(authorization=authorization)
        payload = await request.json()
        want_stream = bool(payload.get("stream")) if isinstance(payload, dict) else False
        upstream = get_twoapi_manager().get_plugin("zo").forward_chat(payload, stream=want_stream)
        return _response_from_upstream(upstream, stream=want_stream)
    except HTTPException as exc:
        return _handle_http_exception(exc)
    except Exception as exc:
        return _openai_error(str(exc), status_code=503, code="no_available_account")


@proxy_router.post("/{path_token}/chat/completions")
async def zo_chat_with_token(path_token: str, request: Request, authorization: str = Header(default="")):
    try:
        _require_key(path_token=path_token, authorization=authorization)
        payload = await request.json()
        want_stream = bool(payload.get("stream")) if isinstance(payload, dict) else False
        upstream = get_twoapi_manager().get_plugin("zo").forward_chat(payload, stream=want_stream)
        return _response_from_upstream(upstream, stream=want_stream)
    except HTTPException as exc:
        return _handle_http_exception(exc)
    except Exception as exc:
        return _openai_error(str(exc), status_code=503, code="no_available_account")
