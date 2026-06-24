"""协作式任务取消令牌。

注册流程在长轮询循环（邮箱 OTP/验证链接、浏览器等待、Google OAuth driver）
里每轮检查令牌状态，命中即抛 TaskCancelledError 中断，使“停止任务”在
一个轮询间隔（约数秒）内真正生效，而不是等当前注册跑完或超时。

令牌默认为 None：所有检查点短路，行为与未引入取消机制前完全一致。
"""
from __future__ import annotations

import threading
from contextvars import ContextVar


class TaskCancelledError(Exception):
    """协作式取消：注册内部检测到取消信号时抛出。

    与普通业务异常区分开，便于 _do_one 特判为“用户取消”而非“注册失败”。
    """


# 线程内隐式传递的取消令牌：worker 线程在执行注册前 set，注册内部各层
# （OAuthBrowser、drive_google_oauth 等）无需逐平台显式传参即可读取。
# ThreadPoolExecutor 的 worker 线程不继承主线程 context，因此必须在 _do_one
# 内部 set，该线程上所有同步调用栈都能读到同一个值。
_CANCEL_TOKEN: "ContextVar[CancelToken | None]" = ContextVar("cancel_token", default=None)


def get_active_cancel_token() -> "CancelToken | None":
    """读取当前调用栈隐式绑定的取消令牌（可能为 None）。"""
    return _CANCEL_TOKEN.get()


class CancelToken:
    """轻量线程内共享的取消令牌。

    worker 线程内创建一个实例，后台轮询线程在检测到 DB cancel 状态后
    调用 request() 置位；注册内部各循环调用 is_set()/raise_if_set()。
    基于 threading.Event，线程安全且零分配。
    """

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    def request(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def raise_if_set(self) -> None:
        if self._event.is_set():
            raise TaskCancelledError("任务已取消")


def check_cancel(token: "CancelToken | None") -> None:
    """在轮询循环每轮调用；token 为 None 时直接返回，无任何开销。"""
    if token is not None and token.is_set():
        raise TaskCancelledError("任务已取消")
