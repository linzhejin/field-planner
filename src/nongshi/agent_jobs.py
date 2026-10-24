"""UI-independent background execution and cooperative cancellation.

An AgentJob starts immediately. Keep it in UI session state, poll snapshot(), and
cancel it before changing the active field/page. The factory must create its own
AgentServiceV3 and service/store. No worker touches Streamlit session state.
Cancellation does not abort a socket: the bounded request drains in the daemon
worker, but its result and subsequent writes are rejected. Previously committed
artifacts remain available; cancellation is not a rollback.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from threading import Event, RLock, Thread
from typing import TYPE_CHECKING, Literal, TypeVar

if TYPE_CHECKING:
    from nongshi.agent import AgentServiceV3
    from nongshi.schema_v3 import AgentResponseV3

T = TypeVar("T")


class AgentCancelled(RuntimeError):
    """Execution was invalidated by the caller; no retry or failure conversion."""


class AgentCancellation:
    """Serialize cancellation with the three allowed assistant persistence calls."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._cancelled = Event()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self) -> None:
        # The lock only covers bounded local writes, never a model/weather call.
        with self._lock:
            self._cancelled.set()

    def check(self) -> None:
        if self.cancelled:
            raise AgentCancelled("本轮助手操作已取消")

    def persist(self, operation: Callable[[], T]) -> T:
        with self._lock:
            self.check()
            return operation()


@dataclass(frozen=True)
class AgentProgress:
    stage: Literal["model", "tool", "finished"]
    status: str
    request_count: int
    tool_name: str = ""


@dataclass(frozen=True)
class AgentJobSnapshot:
    events: tuple[AgentProgress, ...]
    done: bool
    cancelled: bool
    response: AgentResponseV3 | None
    error: str


class AgentJob:
    def __init__(
        self,
        agent_factory: Callable[[], AgentServiceV3],
        field_id: str,
        user_message: str,
        *,
        conversation: list[dict[str, str]] | None = None,
        received_at: datetime | None = None,
    ) -> None:
        self.field_id = field_id
        self.user_message = user_message
        self._cancellation = AgentCancellation()
        self._lock = RLock()
        self._done = Event()
        self._events: list[AgentProgress] = []
        self._response: AgentResponseV3 | None = None
        self._error = ""
        history = [dict(item) for item in (conversation or [])]

        def execute() -> None:
            agent = None
            try:
                self._cancellation.check()
                agent = agent_factory()
                response = agent.run(
                    field_id,
                    user_message,
                    conversation=history,
                    received_at=received_at,
                    progress=self._report,
                    cancellation=self._cancellation,
                )
                with self._lock:
                    if not self._cancellation.cancelled:
                        self._response = response
            except AgentCancelled:
                pass
            except Exception:
                with self._lock:
                    self._error = "助手执行失败；可以重试或使用结构化表单继续。"
            finally:
                try:
                    if agent is not None:
                        agent.close()
                finally:
                    self._done.set()

        self._thread = Thread(target=execute, name="nongshi-agent", daemon=True)
        self._thread.start()

    def _report(self, event: AgentProgress) -> None:
        with self._lock:
            if not self._cancellation.cancelled:
                self._events.append(event)

    def snapshot(self) -> AgentJobSnapshot:
        with self._lock:
            cancelled = self._cancellation.cancelled
            return AgentJobSnapshot(
                events=tuple(self._events),
                done=self._done.is_set() or cancelled,
                cancelled=cancelled,
                response=None if cancelled else self._response,
                error="" if cancelled else self._error,
            )

    def cancel(self) -> None:
        self._cancellation.cancel()

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for worker cleanup; a cancelled snapshot is available immediately."""
        return self._done.wait(timeout)
