`claude_stream.jsonl` is a small sanitized stream from an installed Claude Code
2.1.323 probe. Event/envelope structure and protocol names are retained. Tool,
MCP, plugin, account, path and credential metadata are omitted. Identifiers and
content are placeholders; usage and timing numbers are zeroed. It contains real
observed shapes, not evidence that this adapter has been run against Claude.

`fake_claude.py` is an original offline test executable. It deliberately creates
only its own process descendants and records them for cleanup assertions.

`codex_stream.jsonl` and `codex_failed.jsonl` are shortened streams from codex-cli
0.167.2 probes (2026-11-13). Event and item shapes are as observed; command output,
paths and query results are shortened or replaced.

`fake_codex.py` is an original offline test executable emitting those shapes; like
codex, it starts its command in a new session, so cleanup must find it.

`fake_interactive.py` is an original offline duplex executable for Claude
stream-json and Codex app-server. It exercises acknowledged and ambiguous inputs,
stale result boundaries, explicit server-request responses and supervised cleanup.
It is synthetic; real protocol evidence comes from the separate installed-CLI probes.
