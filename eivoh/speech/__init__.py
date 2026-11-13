"""Provider-neutral speech to text. Channels depend only on Transcriber."""
from typing import Mapping, Protocol


class TranscriptionError(Exception):
    """A failure whose message is safe to show to the owner."""


class Transcriber(Protocol):
    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str: ...


# Provider name -> environment variables it requires.
PROVIDERS = {"soniox": ("SONIOX_API_KEY",)}


def provider_from_env(env: Mapping[str, str]) -> str | None:
    """EIVOH_STT names the provider; "none" disables voice. Unset picks the
    first provider whose settings are present, so voice stays optional."""
    name = env.get("EIVOH_STT", "").strip().lower()
    if name == "none":
        return None
    if not name:
        return next((p for p, keys in PROVIDERS.items() if all(env.get(k, "").strip() for k in keys)), None)
    if name not in PROVIDERS:
        raise ValueError(f"Unknown EIVOH_STT provider: {name}")
    missing = [k for k in PROVIDERS[name] if not env.get(k, "").strip()]
    if missing:
        raise ValueError(f"EIVOH_STT={name} requires {', '.join(missing)}")
    return name


def make_transcriber(provider: str | None, env: Mapping[str, str]) -> Transcriber | None:
    """Build the configured provider; each provider reads its own settings."""
    if provider is None:
        return None
    if provider == "soniox":
        from .soniox import SonioxTranscriber
        return SonioxTranscriber(env["SONIOX_API_KEY"].strip())
    raise ValueError(f"Unknown speech-to-text provider: {provider}")
