from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Inbound:
    channel: str
    chat_id: str
    user_id: str
    message_id: str
    text: str


class Channel(Protocol):
    # Text is Markdown; each channel owns rendering, limits and network policy.
    async def send(self, chat_id: str, text: str) -> str | None: ...
    async def edit(self, chat_id: str, message_id: str, text: str) -> None: ...
    async def typing(self, chat_id: str) -> None: ...
