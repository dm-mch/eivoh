`claude_stream.jsonl` is a small sanitized stream from an installed Claude Code
2.1.323 probe. Event/envelope structure and protocol names are retained. Tool,
MCP, plugin, account, path and credential metadata are omitted. Identifiers and
content are placeholders; usage and timing numbers are zeroed. It contains real
observed shapes, not evidence that this adapter has been run against Claude.

`fake_claude.py` is an original offline test executable. It deliberately creates
only its own process descendants and records them for cleanup assertions.
