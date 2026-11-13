"""Selectable CLI models; Codex's local catalog is refreshed on each call."""
import json
import os
from pathlib import Path


CLAUDE_MODELS = [
    ("fable", "Fable alias"),
    ("opus", "Opus alias"),
    ("sonnet", "Sonnet alias"),
    ("haiku", "Haiku alias"),
    ("claude-fable-5-2", "Fable 5.2"),
    ("claude-opus-5-5", "Opus 5.5"),
    ("claude-sonnet-5-5", "Sonnet 5.5"),
    ("claude-haiku-5-5", "Haiku 5.5"),
]


def catalog() -> dict[str, list[tuple[str, str]]]:
    claude = list(CLAUDE_MODELS)
    if "EIVOH_CLAUDE_MODELS" in os.environ:
        claude = [(name, "Configured model") for name in dict.fromkeys(
            value.strip() for value in os.environ["EIVOH_CLAUDE_MODELS"].split(",") if value.strip())]
    path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser() / "models_cache.json"
    codex = []
    try:
        data = json.loads(path.read_text())
        entries = data.get("models", []) if isinstance(data, dict) else []
        if isinstance(entries, list):
            seen = set()
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("visibility") != "list":
                    continue
                slug = entry.get("slug")
                if isinstance(slug, str) and slug.strip() and slug not in seen:
                    description = entry.get("description")
                    codex.append((slug, description if isinstance(description, str) else ""))
                    seen.add(slug)
    except (OSError, ValueError):
        pass
    return {"claude": claude, "codex": codex}


def resolve(name: str, model_catalog=None) -> tuple[str, str]:
    """Resolve a name, or a one-based number in the displayed catalog order."""
    choices = [(executor, model) for executor, models in
               (catalog() if model_catalog is None else model_catalog).items() for model, _ in models]
    query = name.strip().casefold()
    if query.isdecimal():
        number = int(query)
        matches = choices[number - 1:number] if 1 <= number <= len(choices) else []
    else:
        matches = [choice for choice in choices if query == choice[1].casefold()]
        if not matches and query:
            matches = [choice for choice in choices if query in choice[1].casefold()]
    if len(matches) == 1:
        return matches[0]
    reason = "Ambiguous" if matches else "Unknown"
    candidates = ", ".join(f"{executor} / {model}" for executor, model in matches or choices) or "none"
    raise ValueError(f"{reason} model `{name}`. Candidates: {candidates}.")
