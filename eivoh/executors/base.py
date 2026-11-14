from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

EventKind = Literal["session_started", "text_delta", "message", "tool_started",
                    "tool_finished", "approval_requested", "usage", "turn_completed", "error", "input_accepted",
                    "answer_part"]


@dataclass(frozen=True)
class SteerResult:
    # Accepted means provider-confirmed receipt before the reader saw the turn
    # end; it does not prove the model obeyed the input. Unknown means delivery
    # may have happened: never claim rejection or attach it to a finished answer.
    status: Literal["accepted", "not_sent", "rejected", "unknown"]
    detail: str = ""


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
    supports_steer: bool
    # cwd: the task's working folder; None means the executor's own workdir.
    def start(self, run_id: int, prompt: str, model: str | None = None, effort: str | None = None,
              cwd: str | None = None) -> AsyncIterator[Record]: ...
    def resume(self, run_id: int, session_id: str, prompt: str, model: str | None = None, effort: str | None = None,
               cwd: str | None = None) -> AsyncIterator[Record]: ...
    async def cancel(self, run_id: int) -> None: ...
    async def steer(self, run_id: int, input_id: str, text: str) -> SteerResult: ...
    async def shutdown(self) -> None: ...
