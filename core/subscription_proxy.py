from __future__ import annotations

import atexit
import base64
import json
import os
import platform
import secrets
import select
import shutil
import socket
import socketserver
import subprocess
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse
import zipfile

import requests
try:
    from curl_cffi import requests as curl_requests
except Exception:
    curl_requests = None


class SubscriptionProxyError(RuntimeError):
    pass


_DEFAULT_CONFIG = {
    "enabled": False,
    "url": "",
    "kernel_path": "auto",
    "listen": "http://127.0.0.1:18080",
    "strategy": "urltest",
    "check": "https://www.gstatic.com/generate_204",
    "check_interval": 30,
    "refresh_interval_min": 30,
    "max_nodes": 50,
    "fetch_via_proxy": True,
    "manual_node_tag": "",
    "whitelist_tags": [],
    "blacklist_tags": [],
}
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BUNDLED_SING_BOX_DIR = os.path.join(_BASE_DIR, "tools", "sing-box")
_RUNTIME_DIR = os.path.join(tempfile.gettempdir(), "any_auto_register_subscription_proxy")
_MAX_SUBSCRIPTION_SIZE = 8 << 20
_SING_BOX_RELEASE_API = "https://api.github.com/repos/SagerNet/sing-box/releases/latest"
_AUTO_SING_BOX_TOKENS = {"", "auto", "sing-box", "sing-box.exe"}


@dataclass
class NodeCandidate:
    protocol: str
    name: str
    outbound: Dict[str, Any]


@dataclass
class ParsedSubscription:
    outbounds: List[Dict[str, Any]] = field(default_factory=list)
    nodes: List[Dict[str, Any]] = field(default_factory=list)
    raw_node_count: int = 0
    node_count: int = 0
    available_node_count: int = 0
    unsupported_count: int = 0
    duplicate_count: int = 0
    trimmed_count: int = 0
    protocol_counts: Dict[str, int] = field(default_factory=dict)
    unsupported_protocols: Dict[str, int] = field(default_factory=dict)
    error_samples: List[str] = field(default_factory=list)


def normalize_subscription_config(config: Optional[dict] = None) -> dict:
    raw = dict((config or {}).get("proxy_subscription") or {})
    merged = dict(_DEFAULT_CONFIG)
    merged.update(raw)
    merged["enabled"] = bool(merged.get("enabled", False))
    merged["url"] = str(merged.get("url", "")).strip()
    merged["kernel_path"] = _normalize_kernel_path(raw)
    merged["listen"] = _normalize_listen(str(merged.get("listen", _DEFAULT_CONFIG["listen"])).strip())
    merged["strategy"] = _normalize_strategy(raw.get("strategy", merged.get("strategy")))
    merged["check"] = str(merged.get("check", _DEFAULT_CONFIG["check"])).strip() or _DEFAULT_CONFIG["check"]
    merged["check_interval"] = _to_positive_int(merged.get("check_interval"), 30)
    merged["refresh_interval_min"] = _to_positive_int(merged.get("refresh_interval_min"), 30)
    merged["max_nodes"] = _to_positive_int(merged.get("max_nodes"), 50)
    merged["fetch_via_proxy"] = bool(merged.get("fetch_via_proxy", True))
    merged["manual_node_tag"] = str(raw.get("manual_node_tag", "") or "").strip()
    merged["whitelist_tags"] = _normalize_tag_list(raw.get("whitelist_tags"))
    merged["blacklist_tags"] = _normalize_tag_list(raw.get("blacklist_tags"))
    return merged


def is_subscription_enabled(config: Optional[dict] = None) -> bool:
    cfg = normalize_subscription_config(config)
    return bool(cfg.get("enabled") and cfg.get("url"))


def _normalize_kernel_path(raw: Dict[str, Any]) -> str:
    value = _first_non_empty(
        raw.get("kernel_path"),
        raw.get("sing_box_path"),
        raw.get("glider_path"),
        "auto",
    )
    return str(value).strip() or "auto"


def _normalize_strategy(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"manual", "round_robin", "whitelist_round_robin", "blacklist_round_robin"}:
        return text
    return "urltest"


def _normalize_tag_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw_items = [str(item or "").strip() for item in value]
    else:
        text = str(value or "")
        raw_items = [part.strip() for part in text.replace(",", "\n").splitlines()]
    ordered: List[str] = []
    seen = set()
    for item in raw_items:
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _to_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(str(value).strip())
        return parsed if parsed > 0 else default
    except Exception:
        return default


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _normalize_listen(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return _DEFAULT_CONFIG["listen"]
    if "://" in value:
        return value
    return f"http://{value}"


def _decode_base64_text(value: str) -> str:
    raw = "".join(str(value or "").strip().split())
    if not raw:
        raise SubscriptionProxyError("base64 内容为空")
    padding = "=" * (-len(raw) % 4)
    last_error: Optional[Exception] = None
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            decoded = decoder((raw + padding).encode("utf-8"))
            return decoded.decode("utf-8").lstrip("\ufeff")
        except Exception as exc:
            last_error = exc
    raise SubscriptionProxyError(f"base64 解码失败: {last_error}")


def _decode_optional_base64(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return _decode_base64_text(raw)
    except Exception:
        return raw


def _normalize_host(value: Any) -> str:
    host = str(value or "").strip()
    if not host:
        raise SubscriptionProxyError("节点缺少 server")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1].strip()
    return host


def _normalize_host_port(host: Any, port: Any) -> Tuple[str, int]:
    host_text = _normalize_host(host)
    port_text = str(port or "").strip()
    if not port_text:
        raise SubscriptionProxyError("节点缺少 port")
    try:
        port_int = int(port_text)
    except Exception as exc:
        raise SubscriptionProxyError(f"节点端口无效: {port_text}") from exc
    if port_int <= 0:
        raise SubscriptionProxyError(f"节点端口无效: {port_text}")
    return host_text, port_int


def _strip_subscription_tag(raw: str) -> str:
    return str(raw or "").split("#", 1)[0].strip()


def _extract_node_name(raw: str, fallback: str = "") -> str:
    text = str(raw or "")
    if "#" not in text:
        return fallback
    return unquote(text.split("#", 1)[1]).strip() or fallback


def _normalize_subscription_text(content: bytes) -> str:
    text = content.decode("utf-8", errors="ignore").lstrip("\ufeff").strip()
    if not text:
        return ""
    compact = "".join(text.split())
    try:
        decoded = _decode_base64_text(compact).strip()
    except Exception:
        decoded = ""
    if decoded and _looks_like_subscription(decoded):
        return decoded
    return text


def _looks_like_subscription(text: str) -> bool:
    return "://" in text or _looks_like_clash_yaml(text)


def _looks_like_clash_yaml(text: str) -> bool:
    for line in str(text or "").splitlines():
        if line.lstrip("\ufeff").strip().startswith("proxies:"):
            return True
    return False


def _query_value(query: Dict[str, List[str]], *keys: str, default: str = "") -> str:
    for key in keys:
        values = query.get(key, [])
        if values:
            value = str(values[-1] or "").strip()
            if value:
                return value
    return default


def _mapping_value(mapping: Dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        if key in mapping:
            value = mapping.get(key)
            if value not in (None, ""):
                return value
    return default


def _parse_alpn(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value]
        return [item for item in items if item]
    text = str(value).strip()
    if not text:
        return []
    normalized = text.replace("|", ",").replace(";", ",")
    return [part.strip() for part in normalized.split(",") if part.strip()]


def _parse_optional_int(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = int(text)
        return parsed if parsed > 0 else None
    except Exception:
        return None


def _normalize_protocol_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text == "hy2":
        return "hysteria2"
    return text


def _record_protocol(mapping: Dict[str, int], protocol: str) -> None:
    key = protocol or "unknown"
    mapping[key] = mapping.get(key, 0) + 1


def _record_parse_error(result: ParsedSubscription, protocol: str, exc: Exception) -> None:
    result.unsupported_count += 1
    _record_protocol(result.unsupported_protocols, protocol)
    if len(result.error_samples) < 5:
        result.error_samples.append(f"{protocol or 'unknown'}: {exc}")


def _transport_ws(path: str = "/", host_header: str = "") -> Dict[str, Any]:
    clean_path = str(path or "/").strip() or "/"
    if not clean_path.startswith("/"):
        clean_path = "/" + clean_path
    transport: Dict[str, Any] = {"type": "ws", "path": clean_path}
    if host_header:
        transport["headers"] = {"Host": host_header}
    return transport


def _tls_config(
    server_name: str = "",
    insecure: bool = False,
    alpn: Optional[List[str]] = None,
    disable_sni: bool = False,
) -> Dict[str, Any]:
    config: Dict[str, Any] = {"enabled": True}
    if server_name:
        config["server_name"] = server_name
    if insecure:
        config["insecure"] = True
    if alpn:
        config["alpn"] = list(alpn)
    if disable_sni:
        config["disable_sni"] = True
    return config


def _outbound_dedupe_key(outbound: Dict[str, Any]) -> str:
    payload = dict(outbound)
    payload.pop("tag", None)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _build_tag(index: int, protocol: str) -> str:
    return f"sub-{index:03d}-{protocol or 'node'}"


def _safe_name(protocol: str, host: str, port: int, explicit_name: str = "") -> str:
    return explicit_name or f"{protocol.upper()} {host}:{port}"


def _parse_ss_credential(raw: str) -> str:
    value = unquote(str(raw or "").strip())
    if ":" in value:
        return value
    return _decode_base64_text(value)


def _split_plugin_value(raw: str) -> Tuple[str, str]:
    parts = [part.strip() for part in unquote(str(raw or "")).split(";") if part.strip()]
    if not parts:
        return "", ""
    name = parts[0].lower()
    return name, ";".join(parts[1:])


def _plugin_opts_from_mapping(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    parts: List[str] = []
    for key, value in raw.items():
        if value in (None, "", False):
            continue
        if value is True:
            parts.append(str(key))
            continue
        if isinstance(value, (list, tuple, set)):
            rendered = ",".join([str(item).strip() for item in value if str(item).strip()])
        else:
            rendered = str(value).strip()
        if not rendered:
            continue
        parts.append(f"{key}={rendered}")
    return ";".join(parts)


def _normalize_vmess_cipher(data: Dict[str, Any]) -> str:
    cipher = str(data.get("scy", "")).strip().lower()
    if not cipher:
        fallback = str(data.get("security", "")).strip().lower()
        if fallback in {"aes-128-gcm", "chacha20-poly1305", "none", "zero", "auto"}:
            cipher = fallback
    return cipher or "auto"


def _vmess_uses_tls(data: Dict[str, Any]) -> bool:
    for key in ("tls", "security"):
        value = str(data.get(key, "")).strip().lower()
        if value in {"tls", "1", "true"}:
            return True
    return False


def _extract_clash_ws_options(ws_opts: Any) -> Tuple[str, str]:
    if not isinstance(ws_opts, dict):
        return "", ""
    path = _first_non_empty(ws_opts.get("path"), "/")
    headers = ws_opts.get("headers")
    host_header = ""
    if isinstance(headers, dict):
        host_header = _first_non_empty(headers.get("Host"), headers.get("host"))
    if not host_header:
        host_header = _first_non_empty(ws_opts.get("host"))
    return path, host_header

def _build_shadowsocks_outbound(
    host: str,
    port: int,
    method: str,
    password: str,
    plugin: str = "",
    plugin_opts: str = "",
) -> Dict[str, Any]:
    if not method:
        raise SubscriptionProxyError("ss 节点缺少 cipher")
    if not password:
        raise SubscriptionProxyError("ss 节点缺少 password")
    outbound: Dict[str, Any] = {
        "type": "shadowsocks",
        "server": host,
        "server_port": port,
        "method": method,
        "password": password,
    }
    if plugin:
        outbound["plugin"] = plugin
    if plugin_opts:
        outbound["plugin_opts"] = plugin_opts
    return outbound


def _build_vmess_outbound(
    host: str,
    port: int,
    uuid: str,
    cipher: str = "auto",
    alter_id: int = 0,
    transport: str = "tcp",
    path: str = "",
    host_header: str = "",
    tls_enabled: bool = False,
    server_name: str = "",
    insecure: bool = False,
    alpn: Optional[List[str]] = None,
) -> Dict[str, Any]:
    if not uuid:
        raise SubscriptionProxyError("vmess 节点缺少 uuid")
    outbound: Dict[str, Any] = {
        "type": "vmess",
        "server": host,
        "server_port": port,
        "uuid": uuid,
        "security": cipher or "auto",
    }
    if alter_id > 0:
        outbound["alter_id"] = alter_id
    transport_name = str(transport or "tcp").strip().lower()
    if transport_name in {"", "tcp"}:
        pass
    elif transport_name == "ws":
        outbound["transport"] = _transport_ws(path=path, host_header=host_header)
    else:
        raise SubscriptionProxyError(f"暂不支持 vmess 传输类型: {transport_name}")
    if tls_enabled:
        outbound["tls"] = _tls_config(server_name=server_name, insecure=insecure, alpn=alpn)
    return outbound


def _build_vless_outbound(
    host: str,
    port: int,
    uuid: str,
    transport: str = "tcp",
    path: str = "",
    host_header: str = "",
    security: str = "none",
    server_name: str = "",
    insecure: bool = False,
    flow: str = "",
    alpn: Optional[List[str]] = None,
    packet_encoding: str = "",
) -> Dict[str, Any]:
    if not uuid:
        raise SubscriptionProxyError("vless 节点缺少 uuid")
    outbound: Dict[str, Any] = {
        "type": "vless",
        "server": host,
        "server_port": port,
        "uuid": uuid,
    }
    if flow:
        outbound["flow"] = flow
    if packet_encoding:
        outbound["packet_encoding"] = packet_encoding
    transport_name = str(transport or "tcp").strip().lower()
    if transport_name in {"", "tcp"}:
        pass
    elif transport_name == "ws":
        outbound["transport"] = _transport_ws(path=path, host_header=host_header)
    else:
        raise SubscriptionProxyError(f"暂不支持 vless 传输类型: {transport_name}")

    security_name = str(security or "none").strip().lower()
    if security_name in {"", "none"}:
        return outbound
    if security_name != "tls":
        raise SubscriptionProxyError(f"暂不支持 vless 安全类型: {security_name}")
    outbound["tls"] = _tls_config(server_name=server_name, insecure=insecure, alpn=alpn)
    return outbound


def _build_trojan_outbound(
    host: str,
    port: int,
    password: str,
    transport: str = "tcp",
    path: str = "",
    host_header: str = "",
    server_name: str = "",
    insecure: bool = False,
    alpn: Optional[List[str]] = None,
) -> Dict[str, Any]:
    if not password:
        raise SubscriptionProxyError("trojan 节点缺少 password")
    outbound: Dict[str, Any] = {
        "type": "trojan",
        "server": host,
        "server_port": port,
        "password": password,
        "tls": _tls_config(server_name=server_name, insecure=insecure, alpn=alpn),
    }
    transport_name = str(transport or "tcp").strip().lower()
    if transport_name in {"", "tcp"}:
        return outbound
    if transport_name == "ws":
        outbound["transport"] = _transport_ws(path=path, host_header=host_header)
        return outbound
    raise SubscriptionProxyError(f"暂不支持 trojan 传输类型: {transport_name}")


def _build_anytls_outbound(
    host: str,
    port: int,
    password: str,
    server_name: str = "",
    insecure: bool = False,
    alpn: Optional[List[str]] = None,
    idle_session_timeout: str = "",
    idle_session_check_interval: str = "",
    min_idle_session: Optional[int] = None,
) -> Dict[str, Any]:
    if not password:
        raise SubscriptionProxyError("anytls 节点缺少 password")
    outbound: Dict[str, Any] = {
        "type": "anytls",
        "server": host,
        "server_port": port,
        "password": password,
        "tls": _tls_config(server_name=server_name, insecure=insecure, alpn=alpn),
    }
    if idle_session_timeout:
        outbound["idle_session_timeout"] = idle_session_timeout
    if idle_session_check_interval:
        outbound["idle_session_check_interval"] = idle_session_check_interval
    if min_idle_session:
        outbound["min_idle_session"] = min_idle_session
    return outbound


def _build_tuic_outbound(
    host: str,
    port: int,
    uuid: str,
    password: str,
    server_name: str = "",
    insecure: bool = False,
    alpn: Optional[List[str]] = None,
    congestion_control: str = "",
    udp_relay_mode: str = "",
    zero_rtt_handshake: bool = False,
    heartbeat: str = "",
    disable_sni: bool = False,
) -> Dict[str, Any]:
    if not uuid:
        raise SubscriptionProxyError("tuic 节点缺少 uuid")
    if not password:
        raise SubscriptionProxyError("tuic 节点缺少 password")
    outbound: Dict[str, Any] = {
        "type": "tuic",
        "server": host,
        "server_port": port,
        "uuid": uuid,
        "password": password,
        "tls": _tls_config(
            server_name=server_name,
            insecure=insecure,
            alpn=alpn or ["h3"],
            disable_sni=disable_sni,
        ),
    }
    if congestion_control:
        outbound["congestion_control"] = congestion_control
    if udp_relay_mode:
        outbound["udp_relay_mode"] = udp_relay_mode
    if zero_rtt_handshake:
        outbound["zero_rtt_handshake"] = True
    if heartbeat:
        outbound["heartbeat"] = heartbeat
    return outbound


def _build_hysteria2_outbound(
    host: str,
    port: int,
    password: str,
    server_name: str = "",
    insecure: bool = False,
    alpn: Optional[List[str]] = None,
    obfs_type: str = "",
    obfs_password: str = "",
    up_mbps: Optional[int] = None,
    down_mbps: Optional[int] = None,
) -> Dict[str, Any]:
    if not password:
        raise SubscriptionProxyError("hysteria2 节点缺少 password")
    outbound: Dict[str, Any] = {
        "type": "hysteria2",
        "server": host,
        "server_port": port,
        "password": password,
        "tls": _tls_config(server_name=server_name, insecure=insecure, alpn=alpn or ["h3"]),
    }
    if obfs_type:
        obfs: Dict[str, Any] = {"type": obfs_type}
        if obfs_password:
            obfs["password"] = obfs_password
        outbound["obfs"] = obfs
    if up_mbps:
        outbound["up_mbps"] = up_mbps
    if down_mbps:
        outbound["down_mbps"] = down_mbps
    return outbound


def _parse_ss_candidate(raw: str) -> NodeCandidate:
    name = _extract_node_name(raw)
    clean = _strip_subscription_tag(raw)
    rest = clean[len("ss://"):]
    query_str = ""
    if "?" in rest:
        rest, query_str = rest.split("?", 1)
    if "@" in rest:
        credential, hostport = rest.rsplit("@", 1)
        credential = _parse_ss_credential(credential)
    else:
        decoded = _decode_base64_text(rest)
        if "@" not in decoded:
            raise SubscriptionProxyError("ss 节点格式无效")
        credential, hostport = decoded.rsplit("@", 1)
    if ":" not in credential:
        raise SubscriptionProxyError("ss 节点缺少 cipher/password")
    method, password = credential.split(":", 1)
    parsed = urlparse(f"//{hostport}")
    host, port = _normalize_host_port(parsed.hostname, parsed.port)
    query = parse_qs(query_str, keep_blank_values=True)
    plugin, plugin_opts = _split_plugin_value(_query_value(query, "plugin"))
    outbound = _build_shadowsocks_outbound(host, port, method, password, plugin=plugin, plugin_opts=plugin_opts)
    return NodeCandidate("ss", _safe_name("ss", host, port, name), outbound)


def _parse_vmess_candidate(raw: str) -> NodeCandidate:
    payload = _strip_subscription_tag(raw[len("vmess://"):]).strip()
    decoded = _decode_base64_text(payload)
    data = json.loads(decoded)
    host, port = _normalize_host_port(data.get("add"), data.get("port"))
    uuid = str(data.get("id", "")).strip()
    transport = _first_non_empty(data.get("net"), data.get("type"), "tcp").lower()
    host_header = str(data.get("host", "")).strip()
    server_name = _first_non_empty(data.get("sni"), host_header, host)
    insecure = _truthy(data.get("allowInsecure"))
    name = _first_non_empty(_extract_node_name(raw), data.get("ps"))
    outbound = _build_vmess_outbound(
        host=host,
        port=port,
        uuid=uuid,
        cipher=_normalize_vmess_cipher(data),
        alter_id=_parse_optional_int(data.get("aid")) or 0,
        transport=transport,
        path=str(data.get("path", "")).strip(),
        host_header=host_header,
        tls_enabled=_vmess_uses_tls(data),
        server_name=server_name,
        insecure=insecure,
        alpn=_parse_alpn(data.get("alpn")),
    )
    return NodeCandidate("vmess", _safe_name("vmess", host, port, name), outbound)


def _parse_vless_candidate(raw: str) -> NodeCandidate:
    clean = _strip_subscription_tag(raw)
    parsed = urlparse(clean)
    uuid = unquote(parsed.username or "")
    host, port = _normalize_host_port(parsed.hostname, parsed.port)
    query = parse_qs(parsed.query, keep_blank_values=True)
    transport = _query_value(query, "type", default="tcp").lower()
    security = _query_value(query, "security", default="none").lower()
    if security == "reality":
        raise SubscriptionProxyError("暂不支持 vless reality")
    outbound = _build_vless_outbound(
        host=host,
        port=port,
        uuid=uuid,
        transport=transport,
        path=_query_value(query, "path"),
        host_header=_query_value(query, "host"),
        security=security,
        server_name=_first_non_empty(
            _query_value(query, "sni"),
            _query_value(query, "serverName"),
            _query_value(query, "peer"),
            host,
        ),
        insecure=_truthy(_query_value(query, "allowInsecure", "insecure", "skipVerify")),
        flow=_query_value(query, "flow"),
        alpn=_parse_alpn(_query_value(query, "alpn")),
        packet_encoding=_query_value(query, "packetEncoding", "packet_encoding"),
    )
    return NodeCandidate("vless", _safe_name("vless", host, port, _extract_node_name(raw)), outbound)


def _parse_trojan_candidate(raw: str) -> NodeCandidate:
    clean = _strip_subscription_tag(raw)
    parsed = urlparse(clean)
    password = unquote(parsed.username or "")
    host, port = _normalize_host_port(parsed.hostname, parsed.port)
    query = parse_qs(parsed.query, keep_blank_values=True)
    security = _query_value(query, "security", default="tls").lower()
    if security not in {"", "tls"}:
        raise SubscriptionProxyError(f"暂不支持 trojan 安全类型: {security}")
    outbound = _build_trojan_outbound(
        host=host,
        port=port,
        password=password,
        transport=_query_value(query, "type", default="tcp").lower(),
        path=_query_value(query, "path"),
        host_header=_query_value(query, "host"),
        server_name=_first_non_empty(
            _query_value(query, "sni"),
            _query_value(query, "serverName"),
            _query_value(query, "peer"),
            host,
        ),
        insecure=_truthy(_query_value(query, "allowInsecure", "insecure", "skipVerify")),
        alpn=_parse_alpn(_query_value(query, "alpn")),
    )
    return NodeCandidate("trojan", _safe_name("trojan", host, port, _extract_node_name(raw)), outbound)


def _parse_anytls_candidate(raw: str) -> NodeCandidate:
    clean = _strip_subscription_tag(raw)
    parsed = urlparse(clean)
    password = unquote(parsed.username or "")
    host, port = _normalize_host_port(parsed.hostname, parsed.port)
    query = parse_qs(parsed.query, keep_blank_values=True)
    security = _query_value(query, "security", default="tls").lower()
    if security not in {"", "tls"}:
        raise SubscriptionProxyError(f"暂不支持 anytls 安全类型: {security}")
    outbound = _build_anytls_outbound(
        host=host,
        port=port,
        password=password,
        server_name=_first_non_empty(
            _query_value(query, "sni"),
            _query_value(query, "serverName"),
            _query_value(query, "peer"),
            host,
        ),
        insecure=_truthy(_query_value(query, "allowInsecure", "insecure", "skipVerify")),
        alpn=_parse_alpn(_query_value(query, "alpn")),
        idle_session_timeout=_query_value(query, "idle_session_timeout"),
        idle_session_check_interval=_query_value(query, "idle_session_check_interval"),
        min_idle_session=_parse_optional_int(_query_value(query, "min_idle_session")),
    )
    return NodeCandidate("anytls", _safe_name("anytls", host, port, _extract_node_name(raw)), outbound)


def _parse_tuic_candidate(raw: str) -> NodeCandidate:
    clean = _strip_subscription_tag(raw)
    parsed = urlparse(clean)
    uuid = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    host, port = _normalize_host_port(parsed.hostname, parsed.port)
    query = parse_qs(parsed.query, keep_blank_values=True)
    outbound = _build_tuic_outbound(
        host=host,
        port=port,
        uuid=uuid,
        password=password,
        server_name=_first_non_empty(
            _query_value(query, "sni"),
            _query_value(query, "serverName"),
            _query_value(query, "peer"),
            host,
        ),
        insecure=_truthy(_query_value(query, "allowInsecure", "insecure", "skipVerify")),
        alpn=_parse_alpn(_query_value(query, "alpn")),
        congestion_control=_query_value(query, "congestion_control", "congestion-controller"),
        udp_relay_mode=_query_value(query, "udp_relay_mode", "udp-relay-mode"),
        zero_rtt_handshake=_truthy(_query_value(query, "zero_rtt_handshake", "zero-rtt-handshake")),
        heartbeat=_query_value(query, "heartbeat"),
        disable_sni=_truthy(_query_value(query, "disable_sni", "disable-sni")),
    )
    return NodeCandidate("tuic", _safe_name("tuic", host, port, _extract_node_name(raw)), outbound)


def _parse_hysteria2_candidate(raw: str) -> NodeCandidate:
    clean = _strip_subscription_tag(raw)
    parsed = urlparse(clean)
    password = unquote(parsed.username or "")
    host, port = _normalize_host_port(parsed.hostname, parsed.port)
    query = parse_qs(parsed.query, keep_blank_values=True)
    outbound = _build_hysteria2_outbound(
        host=host,
        port=port,
        password=password,
        server_name=_first_non_empty(
            _query_value(query, "sni"),
            _query_value(query, "serverName"),
            _query_value(query, "peer"),
            host,
        ),
        insecure=_truthy(_query_value(query, "allowInsecure", "insecure", "skipVerify")),
        alpn=_parse_alpn(_query_value(query, "alpn")),
        obfs_type=_query_value(query, "obfs"),
        obfs_password=_query_value(query, "obfs-password", "obfs_password"),
        up_mbps=_parse_optional_int(_query_value(query, "up", "upmbps", "up_mbps")),
        down_mbps=_parse_optional_int(_query_value(query, "down", "downmbps", "down_mbps")),
    )
    return NodeCandidate("hysteria2", _safe_name("hysteria2", host, port, _extract_node_name(raw)), outbound)


def _parse_simple_candidate(raw: str) -> NodeCandidate:
    clean = _strip_subscription_tag(raw)
    parsed = urlparse(clean)
    scheme = parsed.scheme.lower()
    host, port = _normalize_host_port(parsed.hostname, parsed.port)
    if scheme in {"http", "https"}:
        outbound: Dict[str, Any] = {
            "type": "http",
            "server": host,
            "server_port": port,
        }
        if parsed.username:
            outbound["username"] = unquote(parsed.username)
        if parsed.password:
            outbound["password"] = unquote(parsed.password)
        if scheme == "https":
            outbound["tls"] = _tls_config(server_name=host)
        return NodeCandidate("http", _safe_name("http", host, port, _extract_node_name(raw)), outbound)
    if scheme in {"socks", "socks5", "socks5h"}:
        outbound = {
            "type": "socks",
            "server": host,
            "server_port": port,
            "version": "5",
        }
        if parsed.username:
            outbound["username"] = unquote(parsed.username)
        if parsed.password:
            outbound["password"] = unquote(parsed.password)
        return NodeCandidate("socks5", _safe_name("socks5", host, port, _extract_node_name(raw)), outbound)
    raise SubscriptionProxyError(f"订阅项协议不受支持: {scheme}")


def _normalize_entry(raw: str) -> NodeCandidate:
    line = str(raw or "").strip()
    if not line:
        raise SubscriptionProxyError("空订阅项")
    lower = line.lower()
    if lower.startswith("ss://"):
        return _parse_ss_candidate(line)
    if lower.startswith("vmess://"):
        return _parse_vmess_candidate(line)
    if lower.startswith("vless://"):
        return _parse_vless_candidate(line)
    if lower.startswith("trojan://"):
        return _parse_trojan_candidate(line)
    if lower.startswith("anytls://"):
        return _parse_anytls_candidate(line)
    if lower.startswith("tuic://"):
        return _parse_tuic_candidate(line)
    if lower.startswith("hysteria2://"):
        return _parse_hysteria2_candidate(line)
    if lower.startswith(("http://", "https://", "socks://", "socks5://", "socks5h://")):
        return _parse_simple_candidate(line)
    raise SubscriptionProxyError("订阅项协议不受支持")


def _require_yaml_module():
    try:
        import yaml  # type: ignore
    except Exception as exc:
        raise SubscriptionProxyError("解析 Clash/Mihomo YAML 需要安装 PyYAML") from exc
    return yaml


def _clash_tls_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return _truthy(value)

def _parse_clash_ss_candidate(proxy: Dict[str, Any]) -> NodeCandidate:
    host, port = _normalize_host_port(proxy.get("server"), proxy.get("port"))
    plugin = str(_mapping_value(proxy, "plugin")).strip().lower()
    plugin_opts = _plugin_opts_from_mapping(_mapping_value(proxy, "plugin-opts", "plugin_opts", default={}))
    outbound = _build_shadowsocks_outbound(
        host=host,
        port=port,
        method=str(proxy.get("cipher", "")).strip(),
        password=str(proxy.get("password", "")).strip(),
        plugin=plugin,
        plugin_opts=plugin_opts,
    )
    return NodeCandidate("ss", _safe_name("ss", host, port, str(proxy.get("name", "")).strip()), outbound)


def _parse_clash_vmess_candidate(proxy: Dict[str, Any]) -> NodeCandidate:
    if proxy.get("reality-opts") or proxy.get("grpc-opts") or proxy.get("http-opts"):
        raise SubscriptionProxyError("暂不支持 vmess reality/grpc/http 传输")
    host, port = _normalize_host_port(proxy.get("server"), proxy.get("port"))
    path, host_header = _extract_clash_ws_options(proxy.get("ws-opts"))
    outbound = _build_vmess_outbound(
        host=host,
        port=port,
        uuid=str(proxy.get("uuid", "")).strip(),
        cipher=str(_mapping_value(proxy, "cipher", default="auto")).strip().lower() or "auto",
        alter_id=_parse_optional_int(_mapping_value(proxy, "alterId", "alter_id")) or 0,
        transport=_first_non_empty(proxy.get("network"), "tcp").lower(),
        path=path,
        host_header=host_header,
        tls_enabled=_clash_tls_value(proxy.get("tls"), False),
        server_name=_first_non_empty(proxy.get("sni"), proxy.get("servername"), host_header, host),
        insecure=_clash_tls_value(_mapping_value(proxy, "skip-cert-verify", "skip_cert_verify"), False),
        alpn=_parse_alpn(_mapping_value(proxy, "alpn")),
    )
    return NodeCandidate("vmess", _safe_name("vmess", host, port, str(proxy.get("name", "")).strip()), outbound)


def _parse_clash_vless_candidate(proxy: Dict[str, Any]) -> NodeCandidate:
    if proxy.get("reality-opts") or proxy.get("grpc-opts") or proxy.get("http-opts"):
        raise SubscriptionProxyError("暂不支持 vless reality/grpc/http 传输")
    host, port = _normalize_host_port(proxy.get("server"), proxy.get("port"))
    path, host_header = _extract_clash_ws_options(proxy.get("ws-opts"))
    security = "tls" if _clash_tls_value(proxy.get("tls"), False) else "none"
    outbound = _build_vless_outbound(
        host=host,
        port=port,
        uuid=str(proxy.get("uuid", "")).strip(),
        transport=_first_non_empty(proxy.get("network"), "tcp").lower(),
        path=path,
        host_header=host_header,
        security=security,
        server_name=_first_non_empty(proxy.get("sni"), proxy.get("servername"), host_header, host),
        insecure=_clash_tls_value(_mapping_value(proxy, "skip-cert-verify", "skip_cert_verify"), False),
        flow=str(proxy.get("flow", "")).strip(),
        alpn=_parse_alpn(_mapping_value(proxy, "alpn")),
        packet_encoding=str(_mapping_value(proxy, "packet-encoding", "packet_encoding")).strip(),
    )
    return NodeCandidate("vless", _safe_name("vless", host, port, str(proxy.get("name", "")).strip()), outbound)


def _parse_clash_trojan_candidate(proxy: Dict[str, Any]) -> NodeCandidate:
    if proxy.get("reality-opts") or proxy.get("grpc-opts") or proxy.get("http-opts"):
        raise SubscriptionProxyError("暂不支持 trojan reality/grpc/http 传输")
    host, port = _normalize_host_port(proxy.get("server"), proxy.get("port"))
    path, host_header = _extract_clash_ws_options(proxy.get("ws-opts"))
    outbound = _build_trojan_outbound(
        host=host,
        port=port,
        password=str(proxy.get("password", "")).strip(),
        transport=_first_non_empty(proxy.get("network"), "tcp").lower(),
        path=path,
        host_header=host_header,
        server_name=_first_non_empty(proxy.get("sni"), proxy.get("servername"), host_header, host),
        insecure=_clash_tls_value(_mapping_value(proxy, "skip-cert-verify", "skip_cert_verify"), False),
        alpn=_parse_alpn(_mapping_value(proxy, "alpn")),
    )
    return NodeCandidate("trojan", _safe_name("trojan", host, port, str(proxy.get("name", "")).strip()), outbound)


def _parse_clash_anytls_candidate(proxy: Dict[str, Any]) -> NodeCandidate:
    host, port = _normalize_host_port(proxy.get("server"), proxy.get("port"))
    outbound = _build_anytls_outbound(
        host=host,
        port=port,
        password=str(_mapping_value(proxy, "password", "uuid")).strip(),
        server_name=_first_non_empty(proxy.get("sni"), proxy.get("servername"), host),
        insecure=_clash_tls_value(_mapping_value(proxy, "skip-cert-verify", "skip_cert_verify"), False),
        alpn=_parse_alpn(_mapping_value(proxy, "alpn")),
        idle_session_timeout=str(_mapping_value(proxy, "idle-session-timeout", "idle_session_timeout")).strip(),
        idle_session_check_interval=str(_mapping_value(proxy, "idle-session-check-interval", "idle_session_check_interval")).strip(),
        min_idle_session=_parse_optional_int(_mapping_value(proxy, "min-idle-session", "min_idle_session")),
    )
    return NodeCandidate("anytls", _safe_name("anytls", host, port, str(proxy.get("name", "")).strip()), outbound)


def _parse_clash_tuic_candidate(proxy: Dict[str, Any]) -> NodeCandidate:
    host, port = _normalize_host_port(proxy.get("server"), proxy.get("port"))
    outbound = _build_tuic_outbound(
        host=host,
        port=port,
        uuid=str(proxy.get("uuid", "")).strip(),
        password=str(proxy.get("password", "")).strip(),
        server_name=_first_non_empty(proxy.get("sni"), proxy.get("servername"), host),
        insecure=_clash_tls_value(_mapping_value(proxy, "skip-cert-verify", "skip_cert_verify"), False),
        alpn=_parse_alpn(_mapping_value(proxy, "alpn")),
        congestion_control=str(_mapping_value(proxy, "congestion-controller", "congestion_control")).strip(),
        udp_relay_mode=str(_mapping_value(proxy, "udp-relay-mode", "udp_relay_mode")).strip(),
        zero_rtt_handshake=_clash_tls_value(_mapping_value(proxy, "zero-rtt-handshake", "zero_rtt_handshake"), False),
        heartbeat=str(_mapping_value(proxy, "heartbeat")).strip(),
        disable_sni=_clash_tls_value(_mapping_value(proxy, "disable-sni", "disable_sni"), False),
    )
    return NodeCandidate("tuic", _safe_name("tuic", host, port, str(proxy.get("name", "")).strip()), outbound)


def _parse_clash_hysteria2_candidate(proxy: Dict[str, Any]) -> NodeCandidate:
    host, port = _normalize_host_port(proxy.get("server"), proxy.get("port"))
    outbound = _build_hysteria2_outbound(
        host=host,
        port=port,
        password=str(proxy.get("password", "")).strip(),
        server_name=_first_non_empty(proxy.get("sni"), proxy.get("servername"), host),
        insecure=_clash_tls_value(_mapping_value(proxy, "skip-cert-verify", "skip_cert_verify"), False),
        alpn=_parse_alpn(_mapping_value(proxy, "alpn")),
        obfs_type=str(_mapping_value(proxy, "obfs")).strip(),
        obfs_password=str(_mapping_value(proxy, "obfs-password", "obfs_password")).strip(),
        up_mbps=_parse_optional_int(_mapping_value(proxy, "up", "up-mbps", "up_mbps")),
        down_mbps=_parse_optional_int(_mapping_value(proxy, "down", "down-mbps", "down_mbps")),
    )
    return NodeCandidate("hysteria2", _safe_name("hysteria2", host, port, str(proxy.get("name", "")).strip()), outbound)


def _parse_clash_simple_candidate(proxy: Dict[str, Any]) -> NodeCandidate:
    proxy_type = _normalize_protocol_name(proxy.get("type"))
    host, port = _normalize_host_port(proxy.get("server"), proxy.get("port"))
    username = str(proxy.get("username", "")).strip()
    password = str(proxy.get("password", "")).strip()
    if proxy_type == "http":
        outbound: Dict[str, Any] = {
            "type": "http",
            "server": host,
            "server_port": port,
        }
        if username:
            outbound["username"] = username
        if password:
            outbound["password"] = password
        return NodeCandidate("http", _safe_name("http", host, port, str(proxy.get("name", "")).strip()), outbound)
    if proxy_type == "socks5":
        outbound = {
            "type": "socks",
            "server": host,
            "server_port": port,
            "version": "5",
        }
        if username:
            outbound["username"] = username
        if password:
            outbound["password"] = password
        return NodeCandidate("socks5", _safe_name("socks5", host, port, str(proxy.get("name", "")).strip()), outbound)
    raise SubscriptionProxyError(f"暂不支持 Clash 节点类型: {proxy_type or 'unknown'}")


def _parse_clash_candidate(proxy: Dict[str, Any]) -> NodeCandidate:
    proxy_type = _normalize_protocol_name(proxy.get("type"))
    if proxy_type == "ss":
        return _parse_clash_ss_candidate(proxy)
    if proxy_type == "vmess":
        return _parse_clash_vmess_candidate(proxy)
    if proxy_type == "vless":
        return _parse_clash_vless_candidate(proxy)
    if proxy_type == "trojan":
        return _parse_clash_trojan_candidate(proxy)
    if proxy_type == "anytls":
        return _parse_clash_anytls_candidate(proxy)
    if proxy_type == "tuic":
        return _parse_clash_tuic_candidate(proxy)
    if proxy_type == "hysteria2":
        return _parse_clash_hysteria2_candidate(proxy)
    if proxy_type in {"http", "socks5"}:
        return _parse_clash_simple_candidate(proxy)
    raise SubscriptionProxyError(f"暂不支持 Clash 节点类型: {proxy_type or 'unknown'}")


def parse_subscription_to_outbounds(text: str, max_nodes: int = 50) -> ParsedSubscription:
    normalized = str(text or "").strip()
    if not normalized:
        raise SubscriptionProxyError("订阅返回为空")
    result = ParsedSubscription()
    seen = set()
    candidates: List[NodeCandidate] = []

    if _looks_like_clash_yaml(normalized):
        yaml = _require_yaml_module()
        parsed = yaml.safe_load(normalized) or {}
        proxies = parsed.get("proxies") or []
        if not isinstance(proxies, list):
            raise SubscriptionProxyError("Clash/Mihomo YAML 中的 proxies 格式无效")
        for item in proxies:
            if not isinstance(item, dict):
                continue
            protocol = _normalize_protocol_name(item.get("type"))
            result.raw_node_count += 1
            _record_protocol(result.protocol_counts, protocol)
            try:
                candidate = _parse_clash_candidate(item)
                key = _outbound_dedupe_key(candidate.outbound)
                if key in seen:
                    result.duplicate_count += 1
                    continue
                seen.add(key)
                result.available_node_count += 1
                if len(candidates) < max_nodes:
                    candidates.append(candidate)
                else:
                    result.trimmed_count += 1
            except Exception as exc:
                _record_parse_error(result, protocol, exc)
    else:
        for line in normalized.splitlines():
            clean = line.strip()
            if not clean or clean.startswith("#") or clean.startswith(";"):
                continue
            protocol = _normalize_protocol_name(clean.split("://", 1)[0] if "://" in clean else "")
            result.raw_node_count += 1
            _record_protocol(result.protocol_counts, protocol)
            try:
                candidate = _normalize_entry(clean)
                key = _outbound_dedupe_key(candidate.outbound)
                if key in seen:
                    result.duplicate_count += 1
                    continue
                seen.add(key)
                result.available_node_count += 1
                if len(candidates) < max_nodes:
                    candidates.append(candidate)
                else:
                    result.trimmed_count += 1
            except Exception as exc:
                _record_parse_error(result, protocol, exc)

    for index, candidate in enumerate(candidates, 1):
        outbound = dict(candidate.outbound)
        tag = _build_tag(index, candidate.protocol)
        outbound["tag"] = tag
        result.outbounds.append(outbound)
        result.nodes.append(
            {
                "tag": tag,
                "name": candidate.name,
                "protocol": candidate.protocol,
            }
        )

    result.node_count = len(result.outbounds)
    if not result.outbounds:
        detail = result.error_samples[0] if result.error_samples else "未解析到可用节点"
        raise SubscriptionProxyError(detail)
    return result


def fetch_subscription(url: str, upstream_proxy: str = "", timeout: int = 20) -> str:
    request_url = str(url or "").strip()
    if not request_url:
        raise SubscriptionProxyError("订阅 URL 为空")
    proxy_url = str(upstream_proxy or "").strip()
    use_curl = bool(proxy_url and (urlparse(proxy_url).scheme or "").lower().startswith("socks") and curl_requests is not None)
    session = curl_requests.Session(impersonate="chrome124") if use_curl else requests.Session()
    try:
        session.verify = False
    except Exception:
        pass
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}
    try:
        response = session.get(
            request_url,
            timeout=timeout,
            headers={"User-Agent": "any-auto-register-subscription-proxy/1.0"},
        )
    except requests.exceptions.InvalidSchema as exc:
        if proxy_url and "Missing dependencies for SOCKS support" in str(exc) and curl_requests is not None:
            session = curl_requests.Session(impersonate="chrome124")
            try:
                session.verify = False
            except Exception:
                pass
            session.proxies = {"http": proxy_url, "https": proxy_url}
            response = session.get(
                request_url,
                timeout=timeout,
                headers={"User-Agent": "any-auto-register-subscription-proxy/1.0"},
            )
        else:
            raise
    response.raise_for_status()
    body = response.content[: _MAX_SUBSCRIPTION_SIZE + 1]
    if len(body) > _MAX_SUBSCRIPTION_SIZE:
        raise SubscriptionProxyError("订阅内容过大")
    return _normalize_subscription_text(body)


def _listen_host_port(listen: str) -> Tuple[str, int]:
    first = str(listen or "").split(",", 1)[0].strip()
    if not first:
        return "127.0.0.1", 18080
    if "://" not in first:
        first = "http://" + first
    parsed = urlparse(first)
    host = parsed.hostname or "127.0.0.1"
    port = int(parsed.port or 18080)
    return host, port


def render_sing_box_config(
    outbounds: Iterable[Dict[str, Any]],
    config: dict,
    *,
    listen: Optional[str] = None,
    controller_addr: str = "",
    controller_secret: str = "",
) -> str:
    nodes = [dict(item) for item in outbounds]
    if not nodes:
        raise SubscriptionProxyError("没有可写入的订阅节点")
    listen_host, listen_port = _listen_host_port(listen or config.get("listen", _DEFAULT_CONFIG["listen"]))
    node_tags = [str(item.get("tag", "")).strip() for item in nodes if str(item.get("tag", "")).strip()]
    if not node_tags:
        raise SubscriptionProxyError("订阅节点缺少 tag")

    dispatch_mode = str(config.get("strategy", "urltest")).strip().lower() or "urltest"
    group_type = "urltest" if dispatch_mode == "urltest" else "selector"
    group: Dict[str, Any] = {
        "type": group_type,
        "tag": "subscription-auto",
        "outbounds": node_tags,
    }
    if group_type == "selector":
        group["default"] = node_tags[0]
        group["interrupt_exist_connections"] = False
    else:
        group["url"] = str(config.get("check", _DEFAULT_CONFIG["check"])).strip() or _DEFAULT_CONFIG["check"]
        group["interval"] = f"{_to_positive_int(config.get('check_interval'), 30)}s"
        group["tolerance"] = 50
        group["interrupt_exist_connections"] = False

    payload = {
        "log": {
            "level": "info",
            "timestamp": True,
        },
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": listen_host,
                "listen_port": listen_port,
                "sniff": True,
            }
        ],
        "outbounds": nodes + [
            group,
            {"type": "direct", "tag": "direct"},
            {"type": "block", "tag": "block"},
        ],
        "route": {
            "auto_detect_interface": True,
            "final": "subscription-auto",
        },
    }
    if group_type == "selector" and controller_addr:
        payload["experimental"] = {
            "clash_api": {
                "external_controller": controller_addr,
                "secret": controller_secret,
            }
        }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


class _RotatingProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: Tuple[str, int], manager: "SubscriptionProxyManager"):
        self._manager = manager
        super().__init__(server_address, _RotatingProxyHandler)


class _RotatingProxyHandler(socketserver.BaseRequestHandler):
    _HEADER_LIMIT = 65536

    @property
    def manager(self) -> "SubscriptionProxyManager":
        return self.server._manager  # type: ignore[attr-defined]

    def handle(self) -> None:
        client = self.request
        client.settimeout(15)
        upstream: Optional[socket.socket] = None
        try:
            initial = self._read_initial_payload(client)
            if not initial:
                return
            upstream = self.manager.open_dispatched_proxy_socket(initial)
            client.settimeout(None)
            upstream.settimeout(None)
            self._relay(client, upstream)
        except Exception:
            self._write_error(client, b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except Exception:
                    pass

    def _read_initial_payload(self, client: socket.socket) -> bytes:
        data = b""
        while b"\r\n\r\n" not in data and len(data) < self._HEADER_LIMIT:
            chunk = client.recv(8192)
            if not chunk:
                break
            data += chunk
        return data

    def _relay(self, client: socket.socket, upstream: socket.socket) -> None:
        sockets = [client, upstream]
        while True:
            readable, _writable, _errors = select.select(sockets, [], [], 60)
            if not readable:
                continue
            for source in readable:
                try:
                    chunk = source.recv(65536)
                except Exception:
                    return
                if not chunk:
                    return
                target = upstream if source is client else client
                target.sendall(chunk)

    def _write_error(self, client: socket.socket, payload: bytes) -> None:
        try:
            client.sendall(payload)
        except Exception:
            pass

class SubscriptionProxyManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._process: Optional[subprocess.Popen] = None
        self._proxy_server: Optional[_RotatingProxyServer] = None
        self._proxy_thread: Optional[threading.Thread] = None
        self._stdout_handle = None
        self._config_path = ""
        self._log_path = ""
        self._outbounds: List[Dict[str, Any]] = []
        self._nodes: List[Dict[str, Any]] = []
        self._listen = ""
        self._internal_listen = ""
        self._controller_addr = ""
        self._controller_secret = ""
        self._current_node_tag = ""
        self._dispatch_cursor = 0
        self._active_config: Dict[str, Any] = dict(_DEFAULT_CONFIG)
        self._last_refresh_ts = 0.0
        self._last_error = ""
        self._last_kernel_path = ""
        self._fingerprint = ""
        self._raw_node_count = 0
        self._available_node_count = 0
        self._duplicate_count = 0
        self._trimmed_count = 0
        self._unsupported_count = 0
        self._protocol_counts: Dict[str, int] = {}
        self._unsupported_protocols: Dict[str, int] = {}

    def ensure_proxy(self, config: Optional[dict] = None, upstream_proxy: str = "") -> str:
        cfg = normalize_subscription_config(config)
        if not cfg["enabled"] or not cfg["url"]:
            return ""
        with self._lock:
            now = time.time()
            refresh_interval_sec = cfg["refresh_interval_min"] * 60
            next_fingerprint = self._fingerprint_for_config(cfg)
            if (
                self._process_alive()
                and self._listen == cfg["listen"]
                and self._fingerprint == next_fingerprint
                and self._last_refresh_ts > 0
                and now - self._last_refresh_ts < refresh_interval_sec
            ):
                return cfg["listen"]
            try:
                return self._refresh_locked(cfg, upstream_proxy)
            except Exception as exc:
                self._last_error = str(exc)
                raise

    def refresh(self, config: Optional[dict] = None, upstream_proxy: str = "") -> str:
        cfg = normalize_subscription_config(config)
        if not cfg["enabled"] or not cfg["url"]:
            raise SubscriptionProxyError("机场订阅代理池未启用")
        with self._lock:
            try:
                return self._refresh_locked(cfg, upstream_proxy)
            except Exception as exc:
                self._last_error = str(exc)
                raise

    def status(self, config: Optional[dict] = None) -> dict:
        cfg = normalize_subscription_config(config)
        with self._lock:
            nodes = self._status_nodes_locked(cfg)
            return {
                "enabled": bool(cfg.get("enabled") and cfg.get("url")),
                "running": self._process_alive(),
                "kernel": "sing-box",
                "mode": cfg.get("strategy", "urltest"),
                "listen": cfg.get("listen", ""),
                "node_count": len(self._outbounds),
                "nodes": nodes,
                "manual_node_tag": cfg.get("manual_node_tag", ""),
                "whitelist_tags": list(cfg.get("whitelist_tags") or []),
                "blacklist_tags": list(cfg.get("blacklist_tags") or []),
                "current_node_tag": self._current_node_tag,
                "current_node_name": self._node_name_by_tag(self._current_node_tag),
                "available_node_count": self._available_node_count,
                "raw_node_count": self._raw_node_count,
                "duplicate_count": self._duplicate_count,
                "trimmed_count": self._trimmed_count,
                "unsupported_count": self._unsupported_count,
                "protocol_counts": dict(self._protocol_counts),
                "unsupported_protocols": dict(self._unsupported_protocols),
                "last_refresh_ts": int(self._last_refresh_ts) if self._last_refresh_ts else 0,
                "last_error": self._last_error,
                "url": self._redact_url(cfg.get("url", "")),
                "kernel_path": self._last_kernel_path or cfg.get("kernel_path", ""),
                "config_path": self._config_path,
                "log_path": self._log_path,
            }

    def rotate_proxy(self, config: Optional[dict] = None, upstream_proxy: str = "") -> str:
        cfg = normalize_subscription_config(config)
        if not cfg.get("enabled") or not cfg.get("url"):
            return str(upstream_proxy or "").strip()
        with self._lock:
            self._maybe_refresh_locked(cfg, upstream_proxy=upstream_proxy)
            if not self._listen:
                return ""
            if not self._outbounds:
                return self._listen
            if cfg.get("strategy") == "urltest":
                return self._listen
            next_tag = self._next_dispatch_tag_locked(cfg, advance=True)
            if not next_tag:
                return self._listen
            self._set_selector_outbound_locked(next_tag)
            return self._listen

    def _maybe_refresh_locked(self, cfg: dict, upstream_proxy: str = "") -> str:
        now = time.time()
        refresh_interval_sec = cfg["refresh_interval_min"] * 60
        next_fingerprint = self._fingerprint_for_config(cfg)
        if (
            self._process_alive()
            and self._listen == cfg["listen"]
            and self._fingerprint == next_fingerprint
            and self._last_refresh_ts > 0
            and now - self._last_refresh_ts < refresh_interval_sec
        ):
            return cfg["listen"]
        return self._refresh_locked(cfg, upstream_proxy)

    def stop(self) -> None:
        with self._lock:
            self._stop_process_locked()

    def _refresh_locked(self, cfg: dict, upstream_proxy: str) -> str:
        os.makedirs(_RUNTIME_DIR, exist_ok=True)
        effective_upstream = upstream_proxy if cfg.get("fetch_via_proxy", True) else ""
        kernel_path = self._resolve_kernel_path(cfg["kernel_path"])
        content = fetch_subscription(cfg["url"], upstream_proxy=effective_upstream)
        parsed = parse_subscription_to_outbounds(content, max_nodes=cfg["max_nodes"])
        internal_listen = self._make_loopback_listen()
        controller_addr = ""
        controller_secret = ""
        if cfg["strategy"] != "urltest":
            controller_addr = self._make_loopback_controller()
            controller_secret = secrets.token_hex(16)
        config_text = render_sing_box_config(
            parsed.outbounds,
            cfg,
            listen=internal_listen,
            controller_addr=controller_addr,
            controller_secret=controller_secret,
        )

        self._config_path = os.path.join(_RUNTIME_DIR, "sing-box-subscription.json")
        self._log_path = os.path.join(_RUNTIME_DIR, "sing-box-subscription.log")
        with open(self._config_path, "w", encoding="utf-8") as handle:
            handle.write(config_text)

        self._stop_process_locked()

        self._stdout_handle = open(self._log_path, "a", encoding="utf-8", errors="ignore")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._process = subprocess.Popen(
            [kernel_path, "run", "-c", self._config_path],
            stdout=self._stdout_handle,
            stderr=subprocess.STDOUT,
            cwd=_RUNTIME_DIR,
            creationflags=creationflags,
        )

        if not self._wait_until_ready(internal_listen, self._process, timeout_sec=10):
            self._last_error = "sing-box 启动失败"
            self._stop_process_locked()
            raise SubscriptionProxyError(self._last_error)

        try:
            self._start_proxy_server_locked(cfg["listen"])
        except Exception as exc:
            self._last_error = f"本地代理监听失败: {exc}"
            self._stop_process_locked()
            raise SubscriptionProxyError(self._last_error) from exc

        self._outbounds = parsed.outbounds
        self._nodes = list(parsed.nodes)
        self._listen = cfg["listen"]
        self._internal_listen = internal_listen
        self._controller_addr = controller_addr
        self._controller_secret = controller_secret
        self._last_refresh_ts = time.time()
        self._last_error = ""
        self._last_kernel_path = kernel_path
        self._fingerprint = self._fingerprint_for_config(cfg)
        self._raw_node_count = parsed.raw_node_count
        self._available_node_count = parsed.available_node_count
        self._duplicate_count = parsed.duplicate_count
        self._trimmed_count = parsed.trimmed_count
        self._unsupported_count = parsed.unsupported_count
        self._protocol_counts = dict(parsed.protocol_counts)
        self._unsupported_protocols = dict(parsed.unsupported_protocols)
        self._dispatch_cursor = 0
        self._current_node_tag = ""
        self._active_config = dict(cfg)
        try:
            self._ensure_initial_selection_locked(cfg)
        except Exception as exc:
            self._last_error = str(exc)
            self._stop_process_locked()
            raise
        return cfg["listen"]

    def _stop_process_locked(self) -> None:
        server = self._proxy_server
        self._proxy_server = None
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        self._proxy_thread = None
        self._listen = ""
        self._outbounds = []
        self._nodes = []
        self._internal_listen = ""
        self._controller_addr = ""
        self._controller_secret = ""
        self._current_node_tag = ""
        self._dispatch_cursor = 0
        self._active_config = dict(_DEFAULT_CONFIG)
        process = self._process
        self._process = None
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        if self._stdout_handle is not None:
            try:
                self._stdout_handle.close()
            except Exception:
                pass
            self._stdout_handle = None
        self._raw_node_count = 0
        self._available_node_count = 0
        self._duplicate_count = 0
        self._trimmed_count = 0
        self._unsupported_count = 0
        self._protocol_counts = {}
        self._unsupported_protocols = {}

    def _process_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _start_proxy_server_locked(self, listen: str) -> None:
        host, port = _listen_host_port(listen)
        server = _RotatingProxyServer((host, port), self)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._proxy_server = server
        self._proxy_thread = thread

    def _make_loopback_listen(self) -> str:
        return f"http://127.0.0.1:{self._allocate_loopback_port()}"

    def _make_loopback_controller(self) -> str:
        return f"127.0.0.1:{self._allocate_loopback_port()}"

    def _allocate_loopback_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _status_nodes_locked(self, cfg: dict) -> List[Dict[str, Any]]:
        whitelist = set(cfg.get("whitelist_tags") or [])
        blacklist = set(cfg.get("blacklist_tags") or [])
        eligible = set(self._eligible_tags_locked(cfg, fail_on_empty=False))
        rendered: List[Dict[str, Any]] = []
        for node in self._nodes:
            item = dict(node)
            tag = str(item.get("tag", "")).strip()
            item["current"] = tag == self._current_node_tag
            item["manual"] = tag == str(cfg.get("manual_node_tag", "")).strip()
            item["whitelisted"] = tag in whitelist
            item["blacklisted"] = tag in blacklist
            item["eligible"] = tag in eligible if eligible else False
            rendered.append(item)
        return rendered

    def _node_name_by_tag(self, tag: str) -> str:
        target = str(tag or "").strip()
        for node in self._nodes:
            if str(node.get("tag", "")).strip() == target:
                return str(node.get("name", "")).strip()
        return ""

    def _open_internal_proxy_socket_locked(self) -> socket.socket:
        host, port = _listen_host_port(self._internal_listen)
        return socket.create_connection((host, port), timeout=10)

    def open_dispatched_proxy_socket(self, initial_payload: bytes) -> socket.socket:
        with self._lock:
            if not self._process_alive():
                raise SubscriptionProxyError("订阅代理池未运行")
            if self._nodes and self._controller_addr:
                tag = self._next_dispatch_tag_locked(self._active_config)
                if tag and tag != self._current_node_tag:
                    self._set_selector_outbound_locked(tag)
            upstream = self._open_internal_proxy_socket_locked()
            try:
                if initial_payload:
                    upstream.sendall(initial_payload)
                return upstream
            except Exception:
                try:
                    upstream.close()
                except Exception:
                    pass
                raise

    def _ensure_initial_selection_locked(self, cfg: dict) -> None:
        if cfg["strategy"] == "urltest":
            self._current_node_tag = ""
            return
        initial = self._next_dispatch_tag_locked(cfg, advance=False)
        if not initial:
            raise SubscriptionProxyError("当前名单配置下没有可用节点")
        self._set_selector_outbound_locked(initial)

    def _next_dispatch_tag_locked(self, cfg: Optional[dict] = None, *, advance: bool = True) -> str:
        effective_cfg = cfg or {
            "strategy": "manual" if self._controller_addr else "urltest",
            "manual_node_tag": "",
            "whitelist_tags": [],
            "blacklist_tags": [],
        }
        mode = str(effective_cfg.get("strategy", "urltest")).strip().lower() or "urltest"
        tags = self._eligible_tags_locked(effective_cfg, fail_on_empty=True)
        if mode == "manual":
            manual_tag = str(effective_cfg.get("manual_node_tag", "")).strip()
            if manual_tag and manual_tag in tags:
                return manual_tag
            return tags[0]
        if not tags:
            return ""
        index = self._dispatch_cursor % len(tags)
        tag = tags[index]
        if advance:
            self._dispatch_cursor = (self._dispatch_cursor + 1) % max(len(tags), 1)
        return tag

    def _eligible_tags_locked(self, cfg: dict, *, fail_on_empty: bool) -> List[str]:
        all_tags = [str(node.get("tag", "")).strip() for node in self._nodes if str(node.get("tag", "")).strip()]
        mode = str(cfg.get("strategy", "urltest")).strip().lower() or "urltest"
        if mode in {"urltest", "round_robin"}:
            tags = list(all_tags)
        elif mode == "manual":
            tags = list(all_tags)
        elif mode == "whitelist_round_robin":
            whitelist = [tag for tag in (cfg.get("whitelist_tags") or []) if tag in all_tags]
            tags = list(whitelist)
        elif mode == "blacklist_round_robin":
            blacklist = set(cfg.get("blacklist_tags") or [])
            tags = [tag for tag in all_tags if tag not in blacklist]
        else:
            tags = list(all_tags)
        if fail_on_empty and not tags:
            if mode == "whitelist_round_robin":
                raise SubscriptionProxyError("白名单模式下没有可用节点")
            if mode == "blacklist_round_robin":
                raise SubscriptionProxyError("黑名单模式排除后没有可用节点")
            raise SubscriptionProxyError("当前没有可用节点")
        return tags

    def _set_selector_outbound_locked(self, tag: str) -> None:
        target = str(tag or "").strip()
        if not target:
            raise SubscriptionProxyError("目标节点不能为空")
        headers = {"Authorization": f"Bearer {self._controller_secret}"} if self._controller_secret else {}
        response = None
        last_error = ""
        for _ in range(10):
            try:
                response = requests.put(
                    f"http://{self._controller_addr}/proxies/subscription-auto",
                    headers=headers,
                    json={"name": target},
                    timeout=5,
                )
                if response.status_code in {200, 204}:
                    self._current_node_tag = target
                    return
                last_error = f"HTTP {response.status_code}"
            except Exception as exc:
                last_error = str(exc)
            time.sleep(0.2)
        raise SubscriptionProxyError(f"切换订阅节点失败: {last_error or 'unknown'}")

    def _resolve_kernel_path(self, raw_path: str) -> str:
        value = str(raw_path or "").strip()
        explicit = value.lower() not in _AUTO_SING_BOX_TOKENS
        candidates = self._explicit_kernel_candidates(value) if explicit else self._auto_kernel_candidates()
        for candidate in candidates:
            if candidate and os.path.exists(candidate):
                return os.path.abspath(candidate)
        if explicit:
            raise SubscriptionProxyError(f"未找到 sing-box 可执行文件: {value}")
        return self._download_kernel_binary()

    def _explicit_kernel_candidates(self, value: str) -> List[str]:
        candidates: List[str] = []
        resolved = shutil.which(value)
        if resolved:
            candidates.append(resolved)
        if os.path.isabs(value):
            candidates.append(value)
        else:
            candidates.append(os.path.join(_BASE_DIR, value))
            candidates.append(os.path.join(os.getcwd(), value))
        return self._dedupe_paths(candidates)

    def _auto_kernel_candidates(self) -> List[str]:
        binary_name = self._kernel_binary_name()
        candidates = [
            os.path.join(_BUNDLED_SING_BOX_DIR, binary_name),
            os.path.join(_BASE_DIR, binary_name),
        ]
        resolved = shutil.which("sing-box")
        if resolved:
            candidates.append(resolved)
        return self._dedupe_paths(candidates)

    def _download_kernel_binary(self) -> str:
        os.makedirs(_BUNDLED_SING_BOX_DIR, exist_ok=True)
        target_path = os.path.join(_BUNDLED_SING_BOX_DIR, self._kernel_binary_name())
        if os.path.exists(target_path):
            return target_path

        release = requests.get(
            _SING_BOX_RELEASE_API,
            timeout=20,
            headers={"User-Agent": "any-auto-register-subscription-proxy/1.0"},
        )
        release.raise_for_status()
        payload = release.json()
        asset = self._select_release_asset(payload.get("assets") or [])
        if not asset:
            raise SubscriptionProxyError("未找到匹配当前平台的 sing-box 发布包")

        os.makedirs(_RUNTIME_DIR, exist_ok=True)
        archive_path = os.path.join(_RUNTIME_DIR, asset["name"])
        extract_dir = os.path.join(_RUNTIME_DIR, "sing_box_extract")
        if os.path.isdir(extract_dir):
            shutil.rmtree(extract_dir, ignore_errors=True)
        os.makedirs(extract_dir, exist_ok=True)

        with requests.get(
            asset["browser_download_url"],
            stream=True,
            timeout=60,
            headers={"User-Agent": "any-auto-register-subscription-proxy/1.0"},
        ) as response:
            response.raise_for_status()
            with open(archive_path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=1 << 15):
                    if chunk:
                        handle.write(chunk)

        self._extract_archive(archive_path, extract_dir)
        binary_path = self._find_kernel_binary(extract_dir)
        if not binary_path:
            raise SubscriptionProxyError("sing-box 压缩包中未找到可执行文件")
        shutil.copy2(binary_path, target_path)
        if os.name != "nt":
            os.chmod(target_path, 0o755)
        return target_path

    def _select_release_asset(self, assets: List[dict]) -> Optional[dict]:
        system_name = platform.system().lower()
        machine = platform.machine().lower()
        arch_tokens = self._kernel_arch_tokens(machine)
        suffixes: List[str] = []

        if system_name.startswith("linux"):
            for arch in arch_tokens:
                suffixes.extend(
                    [
                        f"-linux-{arch}.tar.gz",
                        f"-linux-{arch}-glibc.tar.gz",
                        f"-linux-{arch}-musl.tar.gz",
                        f"-linux-{arch}-softfloat.tar.gz",
                    ]
                )
        elif system_name.startswith("windows"):
            for arch in arch_tokens:
                suffixes.extend(
                    [
                        f"-windows-{arch}.zip",
                        f"-windows-{arch}-legacy-windows-7.zip",
                    ]
                )
        elif system_name.startswith("darwin"):
            for arch in arch_tokens:
                suffixes.extend(
                    [
                        f"-darwin-{arch}.tar.gz",
                        f"-darwin-{arch}-legacy-macos-10.13.tar.gz",
                    ]
                )
        else:
            raise SubscriptionProxyError(f"当前平台暂不支持自动下载 sing-box: {platform.system()}")

        normalized_assets = []
        for asset in assets:
            name = str(asset.get("name", "")).lower()
            if name.startswith("sing-box-"):
                normalized_assets.append((name, asset))

        for suffix in suffixes:
            for name, asset in normalized_assets:
                if name.endswith(suffix):
                    return asset
        return None

    def _kernel_arch_tokens(self, machine: str) -> List[str]:
        text = str(machine or "").lower()
        if text in {"x86_64", "amd64"}:
            return ["amd64"]
        if text in {"arm64", "aarch64"}:
            return ["arm64"]
        if text in {"x86", "i386", "i686"}:
            return ["386"]
        if text.startswith("armv7"):
            return ["armv7", "arm"]
        if text.startswith("armv6"):
            return ["armv6", "arm"]
        return [text, "amd64"]

    def _extract_archive(self, archive_path: str, extract_dir: str) -> None:
        lower_name = os.path.basename(archive_path).lower()
        if lower_name.endswith(".zip"):
            with zipfile.ZipFile(archive_path) as handle:
                handle.extractall(extract_dir)
            return
        if lower_name.endswith(".tar.gz") or lower_name.endswith(".tgz"):
            with tarfile.open(archive_path, "r:gz") as handle:
                base_dir = os.path.abspath(extract_dir)
                for member in handle.getmembers():
                    target = os.path.abspath(os.path.join(base_dir, member.name))
                    if not target.startswith(base_dir + os.sep) and target != base_dir:
                        raise SubscriptionProxyError("sing-box 压缩包路径非法")
                handle.extractall(extract_dir)
            return
        raise SubscriptionProxyError(f"不支持的 sing-box 压缩包格式: {os.path.basename(archive_path)}")

    def _find_kernel_binary(self, root_dir: str) -> str:
        binary_name = self._kernel_binary_name()
        for current_root, _dirs, files in os.walk(root_dir):
            for file_name in files:
                if file_name == binary_name:
                    return os.path.join(current_root, file_name)
        return ""

    def _kernel_binary_name(self) -> str:
        return "sing-box.exe" if os.name == "nt" else "sing-box"

    def _dedupe_paths(self, paths: List[str]) -> List[str]:
        ordered: List[str] = []
        seen = set()
        for item in paths:
            normalized = os.path.abspath(item) if item else ""
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
        return ordered

    def _wait_until_ready(self, listen: str, process: subprocess.Popen, timeout_sec: int = 10) -> bool:
        deadline = time.time() + timeout_sec
        host, port = _listen_host_port(listen)
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        while time.time() < deadline:
            if process.poll() is not None:
                return False
            try:
                with socket.create_connection((host, port), timeout=1):
                    return True
            except Exception:
                time.sleep(0.4)
        return False

    def _fingerprint_for_config(self, cfg: dict) -> str:
        payload = json.dumps(
            {
                "url": cfg["url"],
                "listen": cfg["listen"],
                "strategy": cfg["strategy"],
                "check": cfg["check"],
                "check_interval": cfg["check_interval"],
                "max_nodes": cfg["max_nodes"],
                "kernel_path": cfg["kernel_path"],
                "fetch_via_proxy": bool(cfg.get("fetch_via_proxy", True)),
                "manual_node_tag": cfg.get("manual_node_tag", ""),
                "whitelist_tags": list(cfg.get("whitelist_tags") or []),
                "blacklist_tags": list(cfg.get("blacklist_tags") or []),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return payload

    def _redact_url(self, raw: str) -> str:
        try:
            parsed = urlparse(str(raw or "").strip())
        except Exception:
            return str(raw or "")
        if not parsed.scheme or not parsed.netloc:
            return str(raw or "")
        username = parsed.username
        password = parsed.password
        netloc = parsed.netloc
        if username or password:
            host = parsed.hostname or ""
            port = f":{parsed.port}" if parsed.port else ""
            netloc = f"**:**@{host}{port}"
        query = "..." if parsed.query else ""
        return parsed._replace(netloc=netloc, query=query, fragment="").geturl()


subscription_proxy_manager = SubscriptionProxyManager()
atexit.register(subscription_proxy_manager.stop)
