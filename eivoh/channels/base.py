from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Inbound:
    channel: str
    chat_id: str
    user_id: str
    message_id: str
    text: str
    reply_to: str | None = None
    button: bool = False  # A press of a button the gateway sent; its text is a command.


class Channel(Protocol):
    # Text is Markdown; each channel owns rendering, limits and network policy.
    async def send(self, chat_id: str, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> str | None: ...
    async def edit(self, chat_id: str, message_id: str, text: str) -> None: ...
    async def typing(self, chat_id: str) -> None: ...
