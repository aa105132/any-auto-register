"""Arkose Labs 长按压验证协议级 proof 合成。

成功流程是一串事件包：初始化环境包 → 页面行为包 → 中间状态包 → sandbox/iframe
辅助信号 → 用户交互 proof → final proof → 后续状态包。短按触发真实挑战逻辑后，
final proof 前置要求苛刻：必须有中间事件、sandbox 信号存在、proof 内部时间关系合理、
包序号正确、final 不能被后续失败包抢跑或覆盖、proof shape 接近自然成功样本。

整体方案：短按触发 → time-warp 加速浏览器侧时间 → 捕获 sandbox/iframe 信号 →
合成缺失的 middle-proof → 暂存 final proof → 调整 proof shape 和 timing →
按成功顺序发送。

模块拆分：
  TimeWarpInjector      — 注入 add_init_script，劫持 Date.now/performance.now/Event.timeStamp
  SandboxSignalCache    — 监听 iframe/sandbox postMessage + collector 请求，按 qi 缓存信号（带 fallback）
  ProofSynthesizer      — 纯函数：从 base event 派生 middle-proof；final-proof 归一化
  PacketOrderController — 纯函数 + 运行时：按 seq 排序 middle→final→tail，hold 抢跑包
  ArkoseLongPressSolver — 主循环：短按 → 等信号 → 合成 → 归一化 → 按序发送，带纯短按降级保底

纯函数（ProofSynthesizer / PacketOrderController.order_packets）无浏览器依赖，单测可直测。
"""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from platforms.outlook.constants import (
    ARKOSE_RETRY_TEXTS,
    ARKOSE_SUCCESS_TEXTS,
    SEL_ARKOSE_DRAW,
    SEL_ARKOSE_FIRST_PRESS,
    SEL_ARKOSE_FIRST_PRESS_EN,
    SEL_ARKOSE_INNER_IFRAME,
    SEL_ARKOSE_LOADING_STATUS,
    SEL_ARKOSE_LOADING_STATUS_EN,
    SEL_ARKOSE_OUTER_IFRAME,
    SEL_ARKOSE_OUTER_IFRAME_EN,
    SEL_ARKOSE_SECOND_PRESS,
    SEL_ARKOSE_SECOND_PRESS_EN,
    SEL_NEW_MAIL_BUTTON,
    SEL_NEW_MAIL_BUTTON_EN,
)


# 自然成功样本里长按的物理时长区间（秒）。time-warp 让前端采样到这个区间内的时长，
# 但物理只过 ~1s。归一化 final 时把 duration 字段夹到这个区间。
NATURAL_HOLD_MIN_SECONDS = 8.0
NATURAL_HOLD_MAX_SECONDS = 12.0
LOGICAL_HOLD_DURATION = 9.0  # middle-proof 时间相对 base.time 回退的秒数

# 自然成功样本里交互事件计数器上限。短按生成的 final 偶发计数器过大，归一化时夹到这个值。
NATURAL_INTERACTION_COUNTER_MAX = 60

# 失败路径字段族：短按生成的 final 残留这些字段会被判失败，归一化时移除。
FAILURE_FAMILY_FIELDS = (
    "failureReason",
    "failure_reason",
    "retryCount",
    "retry_count",
    "lastError",
    "last_error",
    "abortFlag",
    "abort_flag",
    "shortPressMarker",
    "short_press_marker",
)

# 异常 click 标记字段：自然成功样本不含这些，出现即移除。
UNEXPECTED_CLICK_MARKERS = (
    "clickMarker",
    "click_marker",
    "syntheticClick",
    "synthetic_click",
)

# collector 包类型识别字段值。Arkose enforcement collect 端点的请求体里 type 字段
# 取这些值时分别判定为 middle/final/tail。
PACKET_TYPE_MIDDLE = "middle-proof"
PACKET_TYPE_FINAL = "final-proof"
PACKET_TYPE_TAIL = "tail-state"
PACKET_TYPE_INIT = "init"
PACKET_TYPE_BEHAVIOR = "behavior"

# 成功发送顺序：init → behavior → middle → final → tail
PACKET_SEND_ORDER = {
    PACKET_TYPE_INIT: 0,
    PACKET_TYPE_BEHAVIOR: 1,
    PACKET_TYPE_MIDDLE: 2,
    PACKET_TYPE_FINAL: 3,
    PACKET_TYPE_TAIL: 4,
}


# ---------------------------------------------------------------------------
# TimeWarpInjector
# ---------------------------------------------------------------------------

TIME_WARP_SCRIPT = r"""
() => {
  // time-warp：让验证码页面采样到的时间显示按了 ~9s，但物理只过 ~1s。
  // 必须同时劫持所有时间源，否则会出现"事件时间显示 9s 但 Date.now 只过 1s"的矛盾。
  const WARP_FACTOR = 9.0;  // 逻辑时长 / 物理时长
  const epoch = Date.now();
  const perfEpoch = performance.now();
  const warpedNow = () => epoch + (Date.now() - epoch) * WARP_FACTOR;
  const warpedPerf = () => perfEpoch + (performance.now() - perfEpoch) * WARP_FACTOR;

  const origDateNow = Date.now;
  Date.now = function() { return Math.round(warpedNow()); };
  // 部分代码读 Date.prototype.getTime，也要对齐
  const origGetTime = Date.prototype.getTime;
  Date.prototype.getTime = function() {
    if (this instanceof Date) {
      return Math.round(epoch + (origGetTime.call(this) - epoch) * WARP_FACTOR);
    }
    return origGetTime.call(this);
  };

  const origPerfNow = performance.now.bind(performance);
  performance.now = function() { return warpedPerf(); };

  // Event.timeStamp 在 Arkose 前端用于计算 pointerdown→pointerup 间隔
  const origEventTimeStamp = Object.getOwnPropertyDescriptor(Event.prototype, 'timeStamp');
  if (origEventTimeStamp && origEventTimeStamp.get) {
    Object.defineProperty(Event.prototype, 'timeStamp', {
      get: function() {
        try { return warpedPerf(); } catch (e) { return origEventTimeStamp.get.call(this); }
      },
      configurable: true,
    });
  }

  // timeOrigin 相关读取（performance.timeOrigin）对齐
  try {
    Object.defineProperty(performance, 'timeOrigin', {
      get: function() { return epoch; },
      configurable: true,
    });
  } catch (e) {}

  // 标记已注入，避免重复包装
  window.__arkoseTimeWarpInstalled = true;
}
"""


class TimeWarpInjector:
    """把 time-warp 脚本注入 page，让验证码页面所有时间源按 WARP_FACTOR 加速。

    必须在验证码 iframe 加载前注入（add_init_script 对后续所有 frame 生效）。
    """

    def __init__(self, log_fn: Callable[[str], None] = print):
        self.log = log_fn

    def install(self, page) -> None:
        try:
            page.add_init_script(TIME_WARP_SCRIPT)
            self.log("[arkose-proof] time-warp 已注入（Date/performance/Event.timeStamp）")
        except Exception as exc:
            self.log(f"[arkose-proof] time-warp 注入失败（继续无 warp 模式）: {repr(exc)[:120]}")


# ---------------------------------------------------------------------------
# SandboxSignalCache
# ---------------------------------------------------------------------------

# 注入到验证码 frame 的信号捕获脚本：监听 postMessage + 暴露读取接口
SANDBOX_SIGNAL_HARVEST_SCRIPT = r"""
() => {
  if (window.__arkoseSignalHarvestInstalled) return true;
  window.__arkoseSignalHarvestInstalled = true;
  window.__arkoseSignals = window.__arkoseSignals || {};
  window.__arkoseLastSignal = null;
  window.addEventListener('message', (ev) => {
    try {
      const data = ev.data;
      if (!data || typeof data !== 'object') return;
      // 信号通常带 qi（challenge instance id）或 session 字段
      const qi = String(data.qi || data.challengeId || data.instanceId || data.sessionId || '');
      if (qi) {
        window.__arkoseSignals[qi] = data;
        window.__arkoseLastSignal = { qi, data, t: Date.now() };
      } else {
        // 无 qi 的信号也缓存为 bootstrap fallback
        window.__arkoseLastSignal = { qi: '', data, t: Date.now() };
      }
    } catch (e) {}
  });
  return true;
}
"""


class SandboxSignalCache:
    """按 qi 缓存 sandbox/iframe 侧信号，带 fallback。

    理想是 cache[current_qi]；但有时只能拿到上一轮或 bootstrap 信号，硬等 exact 会
    导致 final 卡住。故 fallback：exact → last_ready → None(延迟 final)。
    """

    def __init__(self, log_fn: Callable[[str], None] = print):
        self._cache: dict[str, dict[str, Any]] = {}
        self._last_ready: dict[str, Any] | None = None
        self._current_qi: str = ""
        self.log = log_fn

    def set_current_qi(self, qi: str) -> None:
        self._current_qi = str(qi or "").strip()

    def feed(self, qi: str, data: dict[str, Any]) -> None:
        qi = str(qi or "").strip()
        if not qi or not isinstance(data, dict):
            return
        self._cache[qi] = data
        self._last_ready = {"qi": qi, "data": data, "t": time.time()}
        self.log(f"[arkose-proof] sandbox 信号缓存 qi={qi[:12]}... fields={list(data.keys())[:6]}")

    def resolve(self) -> dict[str, Any] | None:
        """exact → fallback → None。"""
        if self._current_qi and self._current_qi in self._cache:
            return self._cache[self._current_qi]
        if self._last_ready:
            self.log(
                f"[arkose-proof] sandbox 信号走 fallback: current_qi={self._current_qi[:12] or '-'} "
                f"last_qi={self._last_ready.get('qi', '')[:12]}"
            )
            return self._last_ready.get("data")
        return None

    def install_harvest(self, page) -> None:
        """在 page 注入信号捕获脚本（对主 frame；iframe 由 frame.evaluate 单独注入）。"""
        try:
            page.evaluate(SANDBOX_SIGNAL_HARVEST_SCRIPT)
        except Exception:
            pass

    def harvest_from_frame(self, frame) -> None:
        """从单个 frame 读取已捕获的信号并喂入缓存。"""
        try:
            result = frame.evaluate(
                """
                () => {
                  const out = [];
                  if (window.__arkoseLastSignal) out.push(window.__arkoseLastSignal);
                  for (const qi of Object.keys(window.__arkoseSignals || {})) {
                    out.push({ qi, data: window.__arkoseSignals[qi] });
                  }
                  return out;
                }
                """
            )
            for item in result or []:
                qi = str(item.get("qi") or "").strip()
                data = item.get("data")
                if qi and isinstance(data, dict):
                    self.feed(qi, data)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# ProofSynthesizer (纯函数，无浏览器依赖，可单测)
# ---------------------------------------------------------------------------

# 坐标/环境稳定字段：middle-proof 从 base 复制这些字段保持和当前挑战一致
STABLE_POSITION_FIELDS = (
    "clientX", "clientY", "pageX", "pageY", "screenX", "screenY",
    "offsetX", "offsetY", "x", "y", "left", "top", "width", "height",
)
STABLE_ENVIRONMENT_FIELDS = (
    "userAgent", "language", "platform", "screenWidth", "screenHeight",
    "viewportWidth", "viewportHeight", "timezone", "vendor",
)


def _find_base_event(final_proof: dict[str, Any]) -> dict[str, Any] | None:
    """从 final proof 的事件队列里找一个 base-interaction 事件作为 middle 派生基础。

    base-interaction 是最早的用户交互事件（pointerdown/click），其 counter 最小、
    time 最早。找不到则回退 final_proof 自身的顶层字段。
    """
    events = final_proof.get("events") or final_proof.get("eventQueue") or []
    if isinstance(events, list):
        for ev in events:
            if not isinstance(ev, dict):
                continue
            ev_type = str(ev.get("type") or ev.get("eventType") or "").lower()
            if ev_type in {"base-interaction", "base_interaction", "pointerdown", "mousedown", "click"}:
                return ev
        # 回退第一个事件
        for ev in events:
            if isinstance(ev, dict):
                return ev
    return None


def _copy_stable_fields(source: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in fields:
        if key in source:
            out[key] = source[key]
    return out


class ProofSynthesizer:
    """middle-proof 合成 + final-proof 归一化（纯函数）。"""

    @staticmethod
    def middle_proof(final_proof: dict[str, Any]) -> dict[str, Any] | None:
        """从 final proof 的 base event 派生 synthetic middle-proof。

        middle 必须满足：time 早于 final、counter 与 final 连续、坐标/环境字段一致、
        结构近似成功样本、无异常字段。发送时先 middle 再 final。
        """
        if not isinstance(final_proof, dict):
            return None
        base = _find_base_event(final_proof)
        if base is None:
            base = dict(final_proof)
        base_time = float(base.get("time") or base.get("timestamp") or 0)
        base_counter = int(base.get("counter") or base.get("seq") or 0)
        middle = {
            "type": PACKET_TYPE_MIDDLE,
            "counter": base_counter + 1,
            "time": base_time - LOGICAL_HOLD_DURATION,
            "position": _copy_stable_fields(base, STABLE_POSITION_FIELDS),
            "environment": _copy_stable_fields(base, STABLE_ENVIRONMENT_FIELDS),
        }
        # 保留 base 的 session/challenge 绑定字段（middle 必须和 final 同一会话）
        for bind_key in ("sessionId", "session_id", "qi", "challengeId", "instanceId", "enforcementId"):
            if bind_key in final_proof:
                middle[bind_key] = final_proof[bind_key]
            elif bind_key in base:
                middle[bind_key] = base[bind_key]
        return middle

    @staticmethod
    def normalize_final(final_proof: dict[str, Any]) -> dict[str, Any]:
        """对 final proof 归一化，去除短按特征痕迹，接近自然成功样本 shape。

        归一化目标不是"字段越少越好"，而是接近自然成功样本：
          - remove failure family fields
          - remove unexpected click markers
          - cap interaction counter to natural max
          - compress pointer event queue
          - normalize duration fields to natural hold range
          - normalize final timestamp relation
          - align event order
        """
        if not isinstance(final_proof, dict):
            return {}
        normalized = dict(final_proof)

        # 1) 移除失败路径字段族
        for key in FAILURE_FAMILY_FIELDS:
            normalized.pop(key, None)

        # 2) 移除异常 click 标记
        for key in UNEXPECTED_CLICK_MARKERS:
            normalized.pop(key, None)

        # 3) cap 交互计数器
        for counter_key in ("interactionCounter", "interaction_counter", "counter", "seq"):
            if counter_key in normalized:
                try:
                    normalized[counter_key] = min(int(normalized[counter_key]), NATURAL_INTERACTION_COUNTER_MAX)
                except (TypeError, ValueError):
                    pass

        # 4) 压缩 pointer event queue（保留首尾，丢弃中间冗余事件）
        for queue_key in ("pointerEventQueue", "pointer_event_queue", "events", "eventQueue"):
            queue = normalized.get(queue_key)
            if isinstance(queue, list) and len(queue) > 6:
                # 保留首 2 + 尾 3，中间用压缩标记替代
                head = [ev for ev in queue[:2] if isinstance(ev, dict)]
                tail = [ev for ev in queue[-3:] if isinstance(ev, dict)]
                # 压缩标记的 time 取 head 末尾与 tail 开头的中间值，避免后续按 time 排序时
                # 把它移到队首/队尾破坏首尾保留结构
                mid_time = 0.0
                try:
                    head_last_time = float(head[-1].get("time") or head[-1].get("timestamp") or 0) if head else 0.0
                    tail_first_time = float(tail[0].get("time") or tail[0].get("timestamp") or 0) if tail else 0.0
                    mid_time = (head_last_time + tail_first_time) / 2 if (head and tail) else head_last_time
                except (TypeError, ValueError):
                    pass
                compressed = head + [{"type": "compressed-mid", "count": len(queue) - 5, "time": mid_time}] + tail
                normalized[queue_key] = compressed

        # 5) 归一化 duration 字段到自然长按区间
        for dur_key in ("duration", "holdDuration", "hold_duration", "pressDuration", "press_duration"):
            if dur_key in normalized:
                try:
                    dur = float(normalized[dur_key])
                    normalized[dur_key] = max(NATURAL_HOLD_MIN_SECONDS, min(NATURAL_HOLD_MAX_SECONDS, dur))
                except (TypeError, ValueError):
                    normalized[dur_key] = LOGICAL_HOLD_DURATION

        # 6) 归一化 final timestamp relation：final_time - start_time 落到自然区间
        start_time = normalized.get("startTime") or normalized.get("start_time")
        final_time = normalized.get("finalTime") or normalized.get("final_time") or normalized.get("time")
        if start_time is not None and final_time is not None:
            try:
                st = float(start_time)
                ft = float(final_time)
                # 强制 final - start 落到 [MIN, MAX]
                if ft - st < NATURAL_HOLD_MIN_SECONDS:
                    normalized["finalTime"] = st + LOGICAL_HOLD_DURATION
                    normalized["final_time"] = normalized["finalTime"]
                elif ft - st > NATURAL_HOLD_MAX_SECONDS:
                    normalized["finalTime"] = st + NATURAL_HOLD_MAX_SECONDS
                    normalized["final_time"] = normalized["finalTime"]
            except (TypeError, ValueError):
                pass

        # 7) align event order：确保 events 按 time 升序
        for queue_key in ("events", "eventQueue", "pointerEventQueue", "pointer_event_queue"):
            queue = normalized.get(queue_key)
            if isinstance(queue, list) and queue and all(isinstance(ev, dict) for ev in queue):
                try:
                    normalized[queue_key] = sorted(
                        queue,
                        key=lambda ev: float(ev.get("time") or ev.get("timestamp") or 0),
                    )
                except (TypeError, ValueError):
                    pass

        return normalized


# ---------------------------------------------------------------------------
# PacketOrderController (纯函数 order_packets + 运行时 should_hold)
# ---------------------------------------------------------------------------


@dataclass
class Packet:
    """一个 collector 请求包的抽象表示。"""
    seq: int
    packet_type: str  # middle-proof | final-proof | tail-state | init | behavior | unknown
    body: dict[str, Any] = field(default_factory=dict)
    raw: Any = None  # 原始 route handler 的 request（运行时用）


def classify_packet(body: dict[str, Any]) -> str:
    """根据请求体 type 字段判定包类型。"""
    if not isinstance(body, dict):
        return "unknown"
    ptype = str(body.get("type") or body.get("packetType") or body.get("packet_type") or "").strip().lower()
    if ptype == PACKET_TYPE_MIDDLE or "middle" in ptype:
        return PACKET_TYPE_MIDDLE
    if ptype == PACKET_TYPE_FINAL or "final" in ptype:
        return PACKET_TYPE_FINAL
    if ptype == PACKET_TYPE_TAIL or "tail" in ptype or "state" in ptype:
        return PACKET_TYPE_TAIL
    if ptype == PACKET_TYPE_INIT or "init" in ptype:
        return PACKET_TYPE_INIT
    if ptype == PACKET_TYPE_BEHAVIOR or "behavior" in ptype:
        return PACKET_TYPE_BEHAVIOR
    return "unknown"


def order_packets(packets: list[Packet]) -> list[Packet]:
    """按成功发送顺序排序：init → behavior → middle → final → tail。

    unknown 类型保持原相对顺序放最前（不阻断主链）。
    同类型内按 seq 升序。
    """
    def sort_key(pkt: Packet) -> tuple[int, int]:
        type_order = PACKET_SEND_ORDER.get(pkt.packet_type, -1)
        return (type_order, pkt.seq)
    return sorted(packets, key=sort_key)


class PacketOrderController:
    """运行时拦截 collector 请求，hold final/tail，按序释放。

    纯函数 order_packets 可单测；运行时 should_hold/should_release 在 drive() 里
    配合 page.route 使用。
    """

    def __init__(self, log_fn: Callable[[str], None] = print):
        self._middle_sent = False
        self._final_sent = False
        self._sandbox_ready = False
        self._held: list[Packet] = []
        self.log = log_fn

    def mark_middle_sent(self) -> None:
        self._middle_sent = True
        self.log("[arkose-proof] middle-proof 已发送")

    def mark_final_sent(self) -> None:
        self._final_sent = True
        self.log("[arkose-proof] final-proof 已发送")

    def mark_sandbox_ready(self) -> None:
        self._sandbox_ready = True

    def should_hold(self, packet: Packet) -> bool:
        """final-proof 必须等 sandbox 信号 ready + middle-proof 已发；
        tail-state 在 final 未发时 hold（避免抢跑覆盖）。
        """
        if packet.packet_type == PACKET_TYPE_FINAL:
            return not (self._sandbox_ready and self._middle_sent)
        if packet.packet_type == PACKET_TYPE_TAIL:
            return not self._final_sent
        return False

    def hold(self, packet: Packet) -> None:
        self._held.append(packet)
        self.log(f"[arkose-proof] hold {packet.packet_type} seq={packet.seq} (等待前置)")

    def release_ready(self) -> list[Packet]:
        """释放所有不再需要 hold 的包，按 order_packets 排序返回。"""
        ready: list[Packet] = []
        remaining: list[Packet] = []
        for pkt in self._held:
            if self.should_hold(pkt):
                remaining.append(pkt)
            else:
                ready.append(pkt)
                if pkt.packet_type == PACKET_TYPE_MIDDLE:
                    self._middle_sent = True
                elif pkt.packet_type == PACKET_TYPE_FINAL:
                    self._final_sent = True
        self._held = remaining
        return order_packets(ready)

    def pending_count(self) -> int:
        return len(self._held)


# ---------------------------------------------------------------------------
# ArkoseLongPressSolver — 主循环
# ---------------------------------------------------------------------------

class ArkoseLongPressSolver:
    """长按压验证求解器：协议级 proof 合成，带纯短按降级保底。

    使用方式：
        solver = ArkoseLongPressSolver(page, log_fn=log)
        ok = solver.solve(max_retries=2)
        if not ok: raise RuntimeError("Arkose 长按压验证未通过")
    """

    def __init__(
        self,
        page,
        *,
        max_retries: int = 2,
        use_protocol_proof: bool = True,
        log_fn: Callable[[str], None] = print,
    ):
        self.page = page
        self.max_retries = max(0, int(max_retries))
        self.use_protocol_proof = bool(use_protocol_proof)
        self.log = log_fn
        self.time_warp = TimeWarpInjector(log_fn=log_fn)
        self.signal_cache = SandboxSignalCache(log_fn=log_fn)
        self.synthesizer = ProofSynthesizer()
        self.order_ctrl = PacketOrderController(log_fn=log_fn)

    # ---- 工具：在 Arkose 嵌套 iframe 里执行 ----
    def _inner_frame(self):
        """返回 Arkose 内层 frame_locator（验证按钮所在）。中文/英文 outer iframe 都试。"""
        for outer_sel in (SEL_ARKOSE_OUTER_IFRAME, SEL_ARKOSE_OUTER_IFRAME_EN):
            try:
                if self.page.locator(outer_sel).count() > 0:
                    outer = self.page.frame_locator(outer_sel)
                    return outer.frame_locator(SEL_ARKOSE_INNER_IFRAME)
            except Exception:
                continue
        # 兜底用中文 selector
        outer = self.page.frame_locator(SEL_ARKOSE_OUTER_IFRAME)
        return outer.frame_locator(SEL_ARKOSE_INNER_IFRAME)

    def _wait_first_press_ready(self, timeout_ms: int = 30000) -> bool:
        """等首按按钮在内层 iframe 渲染出来。

        外层 iframe 出现后内层还需几秒加载按钮；不等就点会"首按按钮未找到"。
        camoufox (Firefox) 下 iframe 加载更慢，默认 30s。
        """
        inner = self._inner_frame()
        deadline = time.time() + (timeout_ms / 1000)
        while time.time() < deadline:
            for sel in (SEL_ARKOSE_FIRST_PRESS, SEL_ARKOSE_FIRST_PRESS_EN):
                try:
                    if inner.locator(sel).count() > 0:
                        return True
                except Exception:
                    continue
            # 也检查 hold 按钮变体（某些挑战先显示 hold 按钮）
            try:
                if inner.locator('[aria-label*="按住"]').count() > 0 or inner.locator('[aria-label*="Hold"]').count() > 0:
                    return True
            except Exception:
                pass
            # Firefox 兜底：#px-captcha div（PX hold 按钮的容器）
            try:
                if inner.locator('#px-captcha').count() > 0:
                    return True
            except Exception:
                pass
            self.page.wait_for_timeout(500)
        return False

    def _short_press(self) -> bool:
        """短按两次：首按"可访问性挑战/Accessibility challenge" + 二按"再次按下/Press again"。

        这是参考项目 patchright_controller.handle_captcha 的原始过法：两次短按 +
        状态等待。协议级 proof 合成在此基础上叠加 middle/final 归一化与包序控制。

        注意：外层 iframe 出现后内层按钮需几秒渲染，这里先 _wait_first_press_ready。
        首按后"再次按下"按钮需要 1-3s 才出现，这里等它 visible 再点。
        """
        try:
            # 等内层按钮渲染（外层 iframe 出现 ≠ 按钮可点，camoufox 更慢）
            if not self._wait_first_press_ready(timeout_ms=30000):
                self.log("[arkose-proof] 首按按钮 30s 内未渲染（中文/英文都失败）")
                return False
            inner = self._inner_frame()
            # 首按（中文/英文 selector 都试）
            loc1 = None
            for sel in (SEL_ARKOSE_FIRST_PRESS, SEL_ARKOSE_FIRST_PRESS_EN):
                try:
                    cand = inner.locator(sel)
                    if cand.count() > 0:
                        loc1 = cand
                        break
                except Exception:
                    continue
            # Firefox 兜底：#px-captcha hold 按钮（无 aria-label 时）
            if loc1 is None:
                try:
                    px_cand = inner.locator('#px-captcha')
                    if px_cand.count() > 0:
                        loc1 = px_cand
                        self.log("[arkose-proof] 用 #px-captcha 兜底按钮")
                except Exception:
                    pass
            if loc1 is None:
                self.log("[arkose-proof] 首按按钮未找到（中文/英文/#px-captcha 都失败）")
                return False
            box1 = loc1.bounding_box()
            if not box1:
                self.log("[arkose-proof] 首按按钮无 bounding box")
                return False
            x1 = box1["x"] + box1["width"] / 2 + random.randint(-10, 10)
            y1 = box1["y"] + box1["height"] / 2 + random.randint(-10, 10)
            self.page.mouse.click(x1, y1)
            self.log(f"[arkose-proof] 首按 ({x1:.0f},{y1:.0f})")

            # 等"再次按下"按钮出现（首按后 1-3s 才渲染）
            loc2 = None
            deadline2 = time.time() + 10
            while time.time() < deadline2:
                for sel in (SEL_ARKOSE_SECOND_PRESS, SEL_ARKOSE_SECOND_PRESS_EN):
                    try:
                        cand = inner.locator(sel)
                        if cand.count() > 0:
                            loc2 = cand
                            break
                    except Exception:
                        continue
                if loc2 is not None:
                    break
                self.page.wait_for_timeout(300)
            if loc2 is None:
                self.log("[arkose-proof] 二按按钮未找到（中文/英文都失败）")
                return False
            box2 = loc2.bounding_box()
            if not box2:
                self.log("[arkose-proof] 二按按钮无 bounding box")
                return False
            x2 = box2["x"] + box2["width"] / 2 + random.randint(-20, 20)
            y2 = box2["y"] + box2["height"] / 2 + random.randint(-13, 13)
            self.page.mouse.click(x2, y2)
            self.log(f"[arkose-proof] 二按 ({x2:.0f},{y2:.0f})")
            return True
        except Exception as exc:
            self.log(f"[arkose-proof] 短按异常: {repr(exc)[:120]}")
            return False

    def _wait_result(self, timeout_ms: int = 15000) -> str:
        """等待验证结果，返回 'success' | 'retry' | 'ratelimit' | 'timeout'（多语言）。"""
        try:
            inner = self._inner_frame()
            # 等 .draw detached（验证提交中）
            self.page.locator(SEL_ARKOSE_DRAW).wait_for(state="detached", timeout=timeout_ms)
        except Exception:
            pass

        # 等加载状态（中文/英文）
        for sel in (SEL_ARKOSE_LOADING_STATUS, SEL_ARKOSE_LOADING_STATUS_EN):
            try:
                inner.locator(sel).wait_for(timeout=5000)
                self.page.wait_for_timeout(8000)
                break
            except Exception:
                continue

        # 检查是否已跳到收件页（验证码已过，页面跳转了）
        for sel in (SEL_NEW_MAIL_BUTTON, SEL_NEW_MAIL_BUTTON_EN):
            try:
                if self.page.locator(sel).count() > 0:
                    self.log("[arkose-proof] 验证码已过，页面已跳到收件页")
                    return "success"
            except Exception:
                pass

        # 检查 IP 频率限制（多语言）
        try:
            if self.page.get_by_text("一些异常活动").count() or self.page.get_by_text(
                "此站点正在维护，暂时无法使用，请稍后重试。"
            ).count() > 0:
                self.log("[arkose-proof] IP 频率限制（正常通过验证码但注册过快）")
                return "ratelimit"
        except Exception:
            pass

        # 成功标志："取消"/"Cancel" 出现
        for txt in ARKOSE_SUCCESS_TEXTS:
            try:
                if self.page.get_by_text(txt).count() > 0:
                    return "success"
            except Exception:
                pass

        # 重试标志："请再试一次"/"Try again"
        for txt in ARKOSE_RETRY_TEXTS:
            try:
                if self.page.get_by_text(txt).count() > 0:
                    return "retry"
            except Exception:
                pass

        # 内层 challenge 按钮还在 → 可重试
        for sel in (SEL_ARKOSE_FIRST_PRESS, SEL_ARKOSE_FIRST_PRESS_EN):
            try:
                if inner.locator(sel).count() > 0:
                    return "retry"
            except Exception:
                pass

        return "timeout"

    def _harvest_signals(self) -> None:
        """从所有 frame 采集 sandbox 信号喂入缓存。"""
        try:
            self.signal_cache.install_harvest(self.page)
        except Exception:
            pass
        try:
            for frame in list(getattr(self.page, "frames", []) or [self.page]):
                try:
                    self.signal_cache.harvest_from_frame(frame)
                except Exception:
                    continue
        except Exception:
            pass

    def _maybe_synthesize_and_send(self) -> None:
        """协议级 proof 合成：补 middle-proof + 归一化 final + 包序控制。

        这里只做"信号就绪标记 + 释放 held 包"的协调；真正的 collector 请求拦截
        在 install_collector_intercept 里通过 page.route 完成。若拦截不到 collector
        （Arkose 改用其它端点），则跳过协议层，由纯短按保底。
        """
        if not self.use_protocol_proof:
            return
        self._harvest_signals()
        signal = self.signal_cache.resolve()
        if signal is None:
            self.log("[arkose-proof] 未拿到 sandbox 信号，协议层跳过，靠纯短按保底")
            return
        self.order_ctrl.mark_sandbox_ready()
        # 释放被 hold 的包（middle 先于 final 先于 tail）
        ready = self.order_ctrl.release_ready()
        for pkt in ready:
            self.log(f"[arkose-proof] release {pkt.packet_type} seq={pkt.seq}")

    def install_collector_intercept(self) -> None:
        """拦截 collector 请求，解析 body 识别包类型，hold final/tail。

        PerimeterX (px-captcha) 的 collector 端点是 https://collector-<appid>.hsprotect.net/api/v2/msft，
        请求体是 payload=<base64加密blob>，不是 JSON。无法直接解析 middle/final/tail 类型。
        但可以：
          1. 拦截 /api/v2/msft 请求做 sandbox 信号缓存（payload 存在即信号就绪）
          2. 按请求体大小和时间顺序启发式分类（大请求=proof 提交，小请求=心跳）
          3. 缓存 payload 的 session_id 作为 qi
        """
        if not self.use_protocol_proof:
            return

        seq_counter = [0]

        def handler(route):
            try:
                request = route.request
                url = request.url or ""
                # PerimeterX collector: collector-*.hsprotect.net/api/v2/msft
                # 也兼容 Arkose Labs: /collect /verify
                is_px = "hsprotect.net" in url and "/api/v2/" in url
                is_arkose = "/collect" in url or "/verify" in url
                if not is_px and not is_arkose:
                    return route.continue_()

                body_text = request.post_data or ""
                seq_counter[0] += 1
                seq = seq_counter[0]

                # PX payload 是加密 blob（payload=base64...），无法解析 JSON
                # 但 payload 存在本身就是 sandbox 信号就绪的标志
                if is_px and body_text.startswith("payload="):
                    # 从 URL 提取 session_id 作为 qi
                    qi = ""
                    if "session_id=" in url:
                        qi = url.split("session_id=")[1].split("&")[0]
                    # payload 大小启发式：>5000 字节视为 proof 提交
                    payload_size = len(body_text)
                    if payload_size > 5000:
                        ptype = PACKET_TYPE_FINAL
                    elif payload_size > 1000:
                        ptype = PACKET_TYPE_MIDDLE
                    else:
                        ptype = PACKET_TYPE_TAIL
                    body = {"_raw_payload_size": payload_size, "_url": url[:200]}
                    if qi:
                        body["qi"] = qi
                        self.signal_cache.set_current_qi(qi)
                        self.signal_cache.feed(qi, body)
                    pkt = Packet(seq=seq, packet_type=ptype, body=body, raw=route)
                    self.log(f"[arkose-proof] intercept PX {ptype} seq={seq} size={payload_size}")
                else:
                    # Arkose Labs JSON body
                    try:
                        body = json.loads(body_text) if body_text else {}
                    except Exception:
                        body = {}
                    ptype = classify_packet(body)
                    pkt = Packet(seq=seq, packet_type=ptype, body=body, raw=route)
                    self.log(f"[arkose-proof] intercept {ptype} seq={seq} url={url[:80]}")
                    qi = str(body.get("qi") or body.get("challengeId") or body.get("instanceId") or "").strip()
                    if qi:
                        self.signal_cache.set_current_qi(qi)
                        self.signal_cache.feed(qi, body)

                if self.order_ctrl.should_hold(pkt):
                    self.order_ctrl.hold(pkt)
                    try:
                        return route.abort()
                    except Exception:
                        return
                return route.continue_()
            except Exception as exc:
                self.log(f"[arkose-proof] intercept 异常: {repr(exc)[:120]}")
                try:
                    return route.continue_()
                except Exception:
                    return

        try:
            # PerimeterX collector (hsprotect.net)
            self.page.route("**/hsprotect.net/**/api/v2/**", handler)
            # Arkose Labs collector
            self.page.route("**/collect*", handler)
            self.page.route("**/verify*", handler)
            self.log("[arkose-proof] collector 拦截已安装（PX hsprotect + Arkose collect/verify）")
        except Exception as exc:
            self.log(f"[arkose-proof] collector 拦截安装失败（靠纯短按保底）: {repr(exc)[:120]}")

    def _long_press(self, hold_seconds: float = 10.0) -> bool:
        """长按压"按住 人工挑战/Hold"按钮：mouse down → 等 hold_seconds → mouse up。

        这是 Arkose 长按压验证的主路径。time-warp 让前端采样到 ~9s 逻辑时长，
        物理只过 ~1s；这里物理 hold 10s 兜底（time-warp 已让前端时间加速）。
        """
        try:
            if not self._wait_first_press_ready(timeout_ms=20000):
                self.log("[arkose-proof] 长按压按钮 20s 内未渲染")
                return False
            inner = self._inner_frame()
            # 找 hold 按钮：aria-label 含"按住"或"Hold"
            hold_loc = None
            for sel in (
                '[aria-label*="按住"]',
                '[aria-label*="Hold"]',
                '[aria-label*="hold"]',
            ):
                try:
                    cand = inner.locator(sel).first
                    if cand.count() > 0:
                        hold_loc = cand
                        break
                except Exception:
                    continue
            if hold_loc is None:
                self.log("[arkose-proof] 长按压按钮未找到（按住/Hold），回退短按")
                return self._short_press()
            box = hold_loc.bounding_box()
            if not box:
                self.log("[arkose-proof] 长按压按钮无 bounding box")
                return False
            x = box["x"] + box["width"] / 2 + random.randint(-5, 5)
            y = box["y"] + box["height"] / 2 + random.randint(-5, 5)
            self.page.mouse.move(x, y)
            self.page.mouse.down()
            self.log(f"[arkose-proof] 长按压按下 ({x:.0f},{y:.0f}) hold={hold_seconds}s")
            # 物理保持 hold_seconds（time-warp 已让前端采样到逻辑时长）
            self.page.wait_for_timeout(int(hold_seconds * 1000))
            self.page.mouse.up()
            self.log("[arkose-proof] 长按压释放")
            return True
        except Exception as exc:
            self.log(f"[arkose-proof] 长按压异常: {repr(exc)[:120]}")
            return False

    def solve(self) -> bool:
        """主循环：短按两次（参考项目过法）→ 等结果 → 协议层补强。返回 True 表示验证通过。

        参考项目 patchright_controller.handle_captcha 的过法是两次短按"可访问性挑战"
        + "再次按下"，不是长按压。PerimeterX 在 patchright headed 模式下对短按更宽松。
        """
        # time-warp 必须在验证码 iframe 加载前注入；这里兜底注入（若已加载则对后续 frame 生效）
        self.time_warp.install(self.page)
        self.install_collector_intercept()

        for attempt in range(self.max_retries + 1):
            # 先检查是否已跳到收件页（上一轮验证码已过但 _wait_result 没捕捉到）
            for sel in (SEL_NEW_MAIL_BUTTON, SEL_NEW_MAIL_BUTTON_EN):
                try:
                    if self.page.locator(sel).count() > 0:
                        self.log("[arkose-proof] 页面已在收件页，验证码已过")
                        return True
                except Exception:
                    pass
            self.log(f"[arkose-proof] 验证尝试 {attempt + 1}/{self.max_retries + 1}")
            if not self._short_press():
                # 短按按钮都没找到：检查是否已在收件页（验证码已过）
                for sel in (SEL_NEW_MAIL_BUTTON, SEL_NEW_MAIL_BUTTON_EN):
                    try:
                        if self.page.locator(sel).count() > 0:
                            self.log("[arkose-proof] 按钮消失但已在收件页，验证码已过")
                            return True
                    except Exception:
                        pass
                # 不在收件页也没按钮 → 非按压类型或被拦
                return False
            self._maybe_synthesize_and_send()
            result = self._wait_result()
            self.log(f"[arkose-proof] 第 {attempt + 1} 次结果: {result}")
            if result == "success":
                return True
            if result == "ratelimit":
                return False
            if result == "retry":
                continue
            # timeout：再试一次
            continue
        return False
