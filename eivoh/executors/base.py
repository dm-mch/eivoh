from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

EventKind = Literal["session_started", "text_delta", "message", "tool_started",
                    "tool_finished", "approval_requested", "usage", "turn_completed", "error"]


@dataclass(frozen=True)
class Event:
    kind: EventKind
    data: dict[str, Any]
    raw: Any
    session_id: str | None = None


@dataclass(frozen=True)
class Record:
    """One physical line, stored once even when it maps to several events or none.

    Synthetic adapter errors have no raw_line. stderr is retained separately.
    Newlines are retained; invalid UTF-8 is decoded with replacement.
    """
    raw_line: str | None
    payload: Any
    events: tuple[Event, ...] = field(default_factory=tuple)
    stream: str = "stdout"


class Executor(Protocol):
    def start(self, run_id: int, prompt: str, model: str | None = None) -> AsyncIterator[Record]: ...
    def resume(self, run_id: int, session_id: str, prompt: str, model: str | None = None) -> AsyncIterator[Record]: ...
    async def cancel(self, run_id: int) -> None: ...
    async def shutdown(self) -> None: ...
