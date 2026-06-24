"""Outlook 注册平台插件单测。

覆盖：
  - OutlookAccountPool 增删改查 + 幂等 + 线程安全
  - parse_outlook_pool_line 四段解析 + 异常输入
  - ProofSynthesizer.middle_proof / normalize_final 纯函数
  - PacketOrderController.order_packets / should_hold / hold-release 流
  - plugin.py 注册表加载 + outlook_self 身份模式
  - outlook_oauth PKCE 纯函数（verifier/challenge/_resolve_oauth_config/_build_authorize_url）

不测：真实浏览器注册、真实 Arkose 验证、真实 Microsoft OAuth（靠手动冒烟）。
"""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core.outlook_account_pool import OutlookAccountPool, parse_outlook_pool_line


# ---------------------------------------------------------------------------
# parse_outlook_pool_line
# ---------------------------------------------------------------------------

def test_parse_outlook_pool_line_valid():
    parsed = parse_outlook_pool_line("a@outlook.com----pw----cid----rt")
    assert parsed == ("a@outlook.com", "pw", "cid", "rt")


def test_parse_outlook_pool_line_strips_whitespace():
    parsed = parse_outlook_pool_line("  a@outlook.com ---- pw ---- cid ---- rt  ")
    assert parsed == ("a@outlook.com", "pw", "cid", "rt")


def test_parse_outlook_pool_line_rejects_wrong_segment_count():
    assert parse_outlook_pool_line("a@outlook.com----pw----cid") is None
    assert parse_outlook_pool_line("a@outlook.com----pw----cid----rt----extra") is None


def test_parse_outlook_pool_line_rejects_missing_fields():
    assert parse_outlook_pool_line("a@outlook.com--------cid----rt") is None  # 空 password
    assert parse_outlook_pool_line("a@outlook.com----pw--------rt") is None  # 空 client_id
    assert parse_outlook_pool_line("a@outlook.com----pw----cid----") is None  # 空 refresh_token


def test_parse_outlook_pool_line_rejects_non_email():
    assert parse_outlook_pool_line("notanemail----pw----cid----rt") is None


def test_parse_outlook_pool_line_rejects_empty_and_comment():
    assert parse_outlook_pool_line("") is None
    assert parse_outlook_pool_line("   ") is None
    assert parse_outlook_pool_line("# comment----pw----cid----rt") is None


# ---------------------------------------------------------------------------
# OutlookAccountPool
# ---------------------------------------------------------------------------

def _write_pool(path: Path, count: int) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "accounts": [
                    {
                        "email": f"user{index}@outlook.com",
                        "password": f"pw{index}",
                        "client_id": f"cid{index}",
                        "refresh_token": f"rt{index}",
                        "status": "valid",
                    }
                    for index in range(count)
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_add_account_creates_new(tmp_path: Path):
    pool_path = tmp_path / "outlook_pool.json"
    pool = OutlookAccountPool(str(pool_path))
    added = pool.add_account("a@outlook.com", "pw", client_id="cid", refresh_token="rt", source="auto_register")
    assert added is True
    accounts = pool.list_all()
    assert len(accounts) == 1
    assert accounts[0].email == "a@outlook.com"
    assert accounts[0].client_id == "cid"
    assert accounts[0].refresh_token == "rt"
    assert accounts[0].source == "auto_register"
    assert accounts[0].status == "valid"


def test_add_account_updates_existing_tokens(tmp_path: Path):
    pool_path = tmp_path / "outlook_pool.json"
    pool = OutlookAccountPool(str(pool_path))
    pool.add_account("a@outlook.com", "pw", client_id="cid1", refresh_token="rt1")
    # 再加同一邮箱，应更新令牌而非新增
    added = pool.add_account("a@outlook.com", "pw", client_id="cid2", refresh_token="rt2")
    assert added is False
    accounts = pool.list_all()
    assert len(accounts) == 1
    assert accounts[0].client_id == "cid2"
    assert accounts[0].refresh_token == "rt2"


def test_add_account_rejects_empty_email_or_password(tmp_path: Path):
    pool_path = tmp_path / "outlook_pool.json"
    pool = OutlookAccountPool(str(pool_path))
    assert pool.add_account("", "pw", client_id="cid", refresh_token="rt") is False
    assert pool.add_account("a@outlook.com", "", client_id="cid", refresh_token="rt") is False
    assert pool.list_all() == []


def test_get_by_email_case_insensitive(tmp_path: Path):
    pool_path = tmp_path / "outlook_pool.json"
    pool = OutlookAccountPool(str(pool_path))
    pool.add_account("A@Outlook.com", "pw", client_id="cid", refresh_token="rt")
    acct = pool.get_by_email("a@outlook.com")
    assert acct is not None
    assert acct.email == "A@Outlook.com"
    assert pool.get_by_email("nonexistent@outlook.com") is None


def test_mark_invalid_and_valid(tmp_path: Path):
    pool_path = tmp_path / "outlook_pool.json"
    pool = OutlookAccountPool(str(pool_path))
    pool.add_account("a@outlook.com", "pw", client_id="cid", refresh_token="rt")
    assert pool.mark_invalid("a@outlook.com", reason="test") is True
    assert pool.get_by_email("a@outlook.com").status == "invalid"
    assert pool.get_by_email("a@outlook.com").notes == "test"
    assert pool.mark_valid("a@outlook.com") is True
    assert pool.get_by_email("a@outlook.com").status == "valid"


def test_delete_invalid(tmp_path: Path):
    pool_path = tmp_path / "outlook_pool.json"
    pool = OutlookAccountPool(str(pool_path))
    pool.add_account("a@outlook.com", "pw", client_id="cid", refresh_token="rt")
    pool.add_account("b@outlook.com", "pw", client_id="cid", refresh_token="rt")
    pool.mark_invalid("a@outlook.com")
    result = pool.delete_invalid()
    assert result["deleted"] == 1
    assert result["remaining"] == 1
    accounts = pool.list_all()
    assert len(accounts) == 1
    assert accounts[0].email == "b@outlook.com"


def test_stats(tmp_path: Path):
    pool_path = tmp_path / "outlook_pool.json"
    pool = OutlookAccountPool(str(pool_path))
    pool.add_account("a@outlook.com", "pw", client_id="cid", refresh_token="rt")
    pool.add_account("b@outlook.com", "pw", client_id="cid", refresh_token="rt")
    pool.mark_invalid("b@outlook.com")
    stats = pool.stats()
    assert stats["total"] == 2
    assert stats["valid"] == 1
    assert stats["invalid"] == 1


def test_import_lines(tmp_path: Path):
    pool_path = tmp_path / "outlook_pool.json"
    pool = OutlookAccountPool(str(pool_path))
    result = pool.import_lines(
        [
            "a@outlook.com----pw----cid----rt",
            "b@outlook.com----pw----cid----rt",
            "invalid-line",
            "a@outlook.com----pw----cid----rt",  # 重复，应更新不新增
        ],
        source="manual",
    )
    assert result["created"] == 2
    assert result["updated"] == 1
    assert result["invalid"] == 1
    assert len(pool.list_all()) == 2


def test_concurrent_add_distinct_emails(tmp_path: Path):
    pool_path = tmp_path / "outlook_pool.json"
    pool = OutlookAccountPool(str(pool_path))

    def add_one(index: int) -> bool:
        return pool.add_account(f"user{index}@outlook.com", f"pw{index}", client_id=f"cid{index}", refresh_token=f"rt{index}")

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(add_one, range(8)))

    assert all(results)
    assert len(pool.list_all()) == 8


# ---------------------------------------------------------------------------
# ProofSynthesizer (纯函数)
# ---------------------------------------------------------------------------

def test_middle_proof_derives_from_base_event():
    from platforms.outlook.arkose_proof import ProofSynthesizer, LOGICAL_HOLD_DURATION
    final = {
        "type": "final-proof",
        "counter": 5,
        "time": 100.0,
        "sessionId": "s1",
        "qi": "q1",
        "events": [
            {"type": "pointerdown", "counter": 4, "time": 95.0, "clientX": 10, "clientY": 20, "userAgent": "UA"},
            {"type": "pointerup", "counter": 5, "time": 100.0, "clientX": 10, "clientY": 20},
        ],
    }
    mid = ProofSynthesizer.middle_proof(final)
    assert mid is not None
    assert mid["type"] == "middle-proof"
    # counter = base.counter + 1
    assert mid["counter"] == 5
    # time = base.time - LOGICAL_HOLD_DURATION（早于 final）
    assert mid["time"] == 95.0 - LOGICAL_HOLD_DURATION
    # 会话绑定字段保留
    assert mid["qi"] == "q1"
    assert mid["sessionId"] == "s1"
    # 稳定坐标字段复制
    assert mid["position"]["clientX"] == 10
    assert mid["environment"]["userAgent"] == "UA"


def test_middle_proof_returns_none_for_non_dict():
    from platforms.outlook.arkose_proof import ProofSynthesizer
    assert ProofSynthesizer.middle_proof("not a dict") is None
    assert ProofSynthesizer.middle_proof(None) is None


def test_normalize_final_removes_failure_family_fields():
    from platforms.outlook.arkose_proof import ProofSynthesizer, FAILURE_FAMILY_FIELDS
    final = {"type": "final-proof", "failureReason": "x", "retryCount": 3, "clickMarker": "y", "events": []}
    norm = ProofSynthesizer.normalize_final(final)
    for key in FAILURE_FAMILY_FIELDS:
        assert key not in norm
    assert "clickMarker" not in norm


def test_normalize_final_caps_interaction_counter():
    from platforms.outlook.arkose_proof import ProofSynthesizer, NATURAL_INTERACTION_COUNTER_MAX
    final = {"interactionCounter": 500, "events": []}
    norm = ProofSynthesizer.normalize_final(final)
    assert norm["interactionCounter"] == NATURAL_INTERACTION_COUNTER_MAX


def test_normalize_final_clamps_duration_to_natural_range():
    from platforms.outlook.arkose_proof import (
        ProofSynthesizer, NATURAL_HOLD_MIN_SECONDS, NATURAL_HOLD_MAX_SECONDS,
    )
    # 过大 -> 夹到 max
    norm_high = ProofSynthesizer.normalize_final({"duration": 50.0, "events": []})
    assert norm_high["duration"] == NATURAL_HOLD_MAX_SECONDS
    # 过小 -> 夹到 min
    norm_low = ProofSynthesizer.normalize_final({"duration": 2.0, "events": []})
    assert norm_low["duration"] == NATURAL_HOLD_MIN_SECONDS


def test_normalize_final_adjusts_final_timestamp_relation():
    from platforms.outlook.arkose_proof import ProofSynthesizer, LOGICAL_HOLD_DURATION
    # final - start < MIN -> 强制设为 start + LOGICAL_HOLD_DURATION
    final = {"startTime": 90.0, "finalTime": 91.0, "events": []}
    norm = ProofSynthesizer.normalize_final(final)
    assert norm["finalTime"] == 90.0 + LOGICAL_HOLD_DURATION


def test_normalize_final_sorts_events_by_time():
    from platforms.outlook.arkose_proof import ProofSynthesizer
    final = {
        "events": [
            {"type": "up", "time": 100.0},
            {"type": "down", "time": 95.0},
            {"type": "move", "time": 97.0},
        ]
    }
    norm = ProofSynthesizer.normalize_final(final)
    times = [ev["time"] for ev in norm["events"]]
    assert times == sorted(times)


def test_normalize_final_compresses_long_pointer_queue():
    from platforms.outlook.arkose_proof import ProofSynthesizer
    queue = [{"type": f"ev{i}", "time": float(i)} for i in range(20)]
    final = {"events": list(queue)}
    norm = ProofSynthesizer.normalize_final(final)
    # 20 > 6 应被压缩：首 2 + 压缩标记 + 尾 3 = 6 个元素
    assert len(norm["events"]) == 6
    assert norm["events"][2]["type"] == "compressed-mid"


# ---------------------------------------------------------------------------
# PacketOrderController
# ---------------------------------------------------------------------------

def test_order_packets_sorts_by_success_sequence():
    from platforms.outlook.arkose_proof import (
        Packet, order_packets,
        PACKET_TYPE_INIT, PACKET_TYPE_BEHAVIOR, PACKET_TYPE_MIDDLE,
        PACKET_TYPE_FINAL, PACKET_TYPE_TAIL,
    )
    pkts = [
        Packet(seq=1, packet_type=PACKET_TYPE_TAIL),
        Packet(seq=2, packet_type=PACKET_TYPE_FINAL),
        Packet(seq=3, packet_type=PACKET_TYPE_MIDDLE),
        Packet(seq=4, packet_type=PACKET_TYPE_INIT),
        Packet(seq=5, packet_type=PACKET_TYPE_BEHAVIOR),
    ]
    ordered = order_packets(pkts)
    assert [p.packet_type for p in ordered] == [
        PACKET_TYPE_INIT, PACKET_TYPE_BEHAVIOR, PACKET_TYPE_MIDDLE,
        PACKET_TYPE_FINAL, PACKET_TYPE_TAIL,
    ]


def test_order_packets_preserves_seq_within_same_type():
    from platforms.outlook.arkose_proof import Packet, order_packets, PACKET_TYPE_MIDDLE
    pkts = [
        Packet(seq=5, packet_type=PACKET_TYPE_MIDDLE),
        Packet(seq=2, packet_type=PACKET_TYPE_MIDDLE),
        Packet(seq=8, packet_type=PACKET_TYPE_MIDDLE),
    ]
    ordered = order_packets(pkts)
    assert [p.seq for p in ordered] == [2, 5, 8]


def test_classify_packet():
    from platforms.outlook.arkose_proof import classify_packet
    assert classify_packet({"type": "middle-proof"}) == "middle-proof"
    assert classify_packet({"type": "final-proof"}) == "final-proof"
    assert classify_packet({"type": "tail-state"}) == "tail-state"
    assert classify_packet({"type": "init"}) == "init"
    assert classify_packet({"type": "behavior"}) == "behavior"
    assert classify_packet({"type": "unknown-xxx"}) == "unknown"
    assert classify_packet({}) == "unknown"
    assert classify_packet("not a dict") == "unknown"


def test_packet_order_controller_hold_release_flow():
    from platforms.outlook.arkose_proof import (
        PacketOrderController, Packet,
        PACKET_TYPE_MIDDLE, PACKET_TYPE_FINAL, PACKET_TYPE_TAIL,
    )
    ctrl = PacketOrderController()
    ctrl.mark_sandbox_ready()
    # middle 可立即释放（无前置）
    assert ctrl.should_hold(Packet(seq=1, packet_type=PACKET_TYPE_MIDDLE)) is False
    # final 需 sandbox + middle_sent
    assert ctrl.should_hold(Packet(seq=1, packet_type=PACKET_TYPE_FINAL)) is True
    ctrl.mark_middle_sent()
    assert ctrl.should_hold(Packet(seq=1, packet_type=PACKET_TYPE_FINAL)) is False
    # tail 需 final_sent
    assert ctrl.should_hold(Packet(seq=1, packet_type=PACKET_TYPE_TAIL)) is True
    ctrl.mark_final_sent()
    assert ctrl.should_hold(Packet(seq=1, packet_type=PACKET_TYPE_TAIL)) is False


def test_packet_order_controller_release_ready_orders_held_packets():
    from platforms.outlook.arkose_proof import (
        PacketOrderController, Packet,
        PACKET_TYPE_MIDDLE, PACKET_TYPE_FINAL, PACKET_TYPE_TAIL,
    )
    ctrl = PacketOrderController()
    ctrl.mark_sandbox_ready()
    # 乱序 hold
    ctrl.hold(Packet(seq=1, packet_type=PACKET_TYPE_FINAL))
    ctrl.hold(Packet(seq=2, packet_type=PACKET_TYPE_TAIL))
    ctrl.hold(Packet(seq=3, packet_type=PACKET_TYPE_MIDDLE))
    # 第一轮 release：middle 可走（自动标记 middle_sent）
    r1 = ctrl.release_ready()
    assert [p.packet_type for p in r1] == [PACKET_TYPE_MIDDLE]
    # 第二轮：final 可走（sandbox + middle_sent），释放 final 时标记 final_sent，
    # tail 的前置也随之满足，故同一轮级联释放 final + tail（按 order_packets 排序）。
    r2 = ctrl.release_ready()
    assert [p.packet_type for p in r2] == [PACKET_TYPE_FINAL, PACKET_TYPE_TAIL]
    # 第三轮：无剩余
    r3 = ctrl.release_ready()
    assert r3 == []
    assert ctrl.pending_count() == 0


# ---------------------------------------------------------------------------
# outlook_oauth 纯函数
# ---------------------------------------------------------------------------

def test_pkce_verifier_length_and_charset():
    from platforms.outlook.outlook_oauth import generate_code_verifier
    v = generate_code_verifier()
    assert len(v) == 128
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")
    assert set(v).issubset(allowed)


def test_pkce_challenge_no_padding():
    from platforms.outlook.outlook_oauth import generate_code_challenge, generate_code_verifier
    v = generate_code_verifier()
    c = generate_code_challenge(v)
    assert "=" not in c
    assert len(c) == 43  # sha256 -> 32 bytes -> 43 base64url chars


def test_resolve_oauth_config_defaults():
    from platforms.outlook.outlook_oauth import _resolve_oauth_config
    from platforms.outlook.constants import DEFAULT_CLIENT_ID, DEFAULT_REDIRECT_URL, DEFAULT_SCOPES
    cid, rurl, scopes, aurl, turl = _resolve_oauth_config({})
    assert cid == DEFAULT_CLIENT_ID
    assert rurl == DEFAULT_REDIRECT_URL
    assert tuple(scopes) == tuple(DEFAULT_SCOPES)
    assert "login.microsoftonline.com" in aurl


def test_resolve_oauth_config_custom_scopes_and_consumers_tenant():
    from platforms.outlook.outlook_oauth import _resolve_oauth_config
    cid, rurl, scopes, aurl, turl = _resolve_oauth_config({
        "outlook_client_id": "custom-cid",
        "outlook_scopes": ["offline_access", "https://outlook.office.com/IMAP.AccessAsUser.All"],
        "outlook_use_consumers_tenant": "true",
    })
    assert cid == "custom-cid"
    assert scopes == ("offline_access", "https://outlook.office.com/IMAP.AccessAsUser.All")
    assert "consumers" in aurl
    assert "consumers" in turl


def test_build_authorize_url_has_pkce_params():
    from platforms.outlook.outlook_oauth import _build_authorize_url, generate_code_verifier, generate_code_challenge
    v = generate_code_verifier()
    c = generate_code_challenge(v)
    url = _build_authorize_url("cid", "https://redirect.example", ("offline_access", "https://outlook.office.com/IMAP.AccessAsUser.All"), c)
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    assert "response_type=code" in url
    assert "offline_access" in url
    assert "IMAP.AccessAsUser.All" in url


# ---------------------------------------------------------------------------
# plugin + identity mode
# ---------------------------------------------------------------------------

def test_plugin_loads_and_declares_outlook_self():
    from core.registry import load_all, get, list_platforms
    load_all()
    cls = get("outlook")
    assert cls.name == "outlook"
    assert "outlook_self" in cls.supported_identity_modes
    assert "headless" in cls.supported_executors
    plats = list_platforms()
    assert any(p["name"] == "outlook" for p in plats)


def test_outlook_self_identity_provider_returns_empty_email():
    from core.base_identity import create_identity_provider, normalize_identity_provider
    assert normalize_identity_provider("outlook_self") == "outlook_self"
    provider = create_identity_provider("outlook_self", extra={"outlook_email_suffix": "@hotmail.com"})
    material = provider.resolve()
    assert material.identity_provider == "outlook_self"
    assert material.email == ""  # email 由 worker 注册过程中生成
    assert material.metadata["outlook_email_suffix"] == "@hotmail.com"


def test_platform_does_not_require_external_email():
    from core.base_platform import RegisterConfig
    from core.registry import load_all, get
    load_all()
    cls = get("outlook")
    cfg = RegisterConfig(executor_type="headed", extra={"identity_provider": "outlook_self"})
    platform = cls(cfg)
    assert platform._should_require_identity_email() is False


def test_platform_builds_browser_adapter_without_email_requirement():
    from core.base_platform import RegisterConfig
    from core.registry import load_all, get
    load_all()
    cls = get("outlook")
    cfg = RegisterConfig(executor_type="headed", extra={"identity_provider": "outlook_self"})
    platform = cls(cfg)
    adapter = platform.build_browser_registration_adapter()
    assert adapter is not None
    assert adapter.capability.browser_mailbox_requires_email is False
    assert adapter.capability.browser_mailbox_requires_mailbox is False


def test_check_valid_requires_refresh_token_and_client_id():
    from core.base_platform import Account, RegisterConfig
    from core.registry import load_all, get
    load_all()
    cls = get("outlook")
    platform = cls(RegisterConfig(executor_type="headed"))
    # 有 refresh_token + client_id -> 有效
    acct_ok = Account(platform="outlook", email="a@outlook.com", password="pw", token="rt",
                      extra={"client_id": "cid"})
    assert platform.check_valid(acct_ok) is True
    # 缺 client_id -> 无效
    acct_no_cid = Account(platform="outlook", email="a@outlook.com", password="pw", token="rt", extra={})
    assert platform.check_valid(acct_no_cid) is False
    # 缺 refresh_token -> 无效
    acct_no_rt = Account(platform="outlook", email="a@outlook.com", password="pw", token="", extra={"client_id": "cid"})
    assert platform.check_valid(acct_no_rt) is False


def test_map_result_marks_registered_when_refresh_token_present():
    from core.base_platform import AccountStatus
    from core.registry import load_all, get
    load_all()
    cls = get("outlook")
    platform = cls.__new__(cls)  # 跳过 __init__ 避免执行器校验
    result = {
        "ok": True,
        "email": "a@outlook.com",
        "password": "pw",
        "client_id": "cid",
        "refresh_token": "rt",
        "access_token": "at",
        "expires_at": "123",
        "scope": "offline_access IMAP",
    }
    reg = platform._map_result(result)
    assert reg.email == "a@outlook.com"
    assert reg.password == "pw"
    assert reg.token == "rt"
    assert reg.status == AccountStatus.REGISTERED
    assert reg.extra["refresh_token"] == "rt"
    assert reg.extra["client_id"] == "cid"
    assert reg.extra["imap_server"] == "outlook.live.com"


def test_map_result_marks_invalid_when_refresh_token_missing():
    from core.base_platform import AccountStatus
    from core.registry import load_all, get
    load_all()
    cls = get("outlook")
    platform = cls.__new__(cls)
    result = {"ok": False, "email": "a@outlook.com", "password": "pw", "error": "captcha_failed"}
    reg = platform._map_result(result)
    assert reg.status == AccountStatus.INVALID
    assert reg.extra["register_error"] == "captcha_failed"
