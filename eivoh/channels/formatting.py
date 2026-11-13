"""Small Markdown subset and balanced Telegram HTML chunks.

Studied reference: nanobot (MIT), nanobot/channels/telegram/runtime.py.
This implementation is original; unsupported Markdown remains readable text.
"""
import html
from html.parser import HTMLParser
import re


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _inline(text: str, depth=0) -> str:
    if depth > 8:
        return html.escape(text)
    pattern = re.compile(r"`([^`\n]+)`|\[([^]\n]+)\]\((https?://[^\s)]+)\)|\*\*(.+?)\*\*|__(.+?)__|~~(.+?)~~|(?<!\w)\*([^*\n]+)\*|(?<!\w)_([^_\n]+)_")
    result, pos = [], 0
    for match in pattern.finditer(text):
        result.append(html.escape(text[pos:match.start()]))
        code, label, url, bold, bold2, strike, italic, italic2 = match.groups()
        if code is not None:
            result.append("<code>" + html.escape(code) + "</code>")
        elif label is not None:
            escaped_url = html.escape(url, quote=True)
            body = _inline(label, depth + 1)
            result.append(f'<a href="{escaped_url}">{body}</a>' if len(escaped_url) < 512 else body)
        else:
            tag = "b" if bold is not None or bold2 is not None else "s" if strike is not None else "i"
            content = next(x for x in (bold, bold2, strike, italic, italic2) if x is not None)
            result.append(f"<{tag}>{_inline(content, depth + 1)}</{tag}>")
        pos = match.end()
    result.append(html.escape(text[pos:]))
    return "".join(result)


def markdown_to_html(text: str) -> str:
    out, code = [], None
    for line in text.splitlines(keepends=True):
        fence = re.match(r"^\s*```([^\n`]*)\s*\n?$", line)
        if fence:
            if code is None:
                code = []
            else:
                out.append("<pre><code>" + html.escape("".join(code)) + "</code></pre>")
                code = None
            continue
        if code is not None:
            code.append(line)
        elif re.match(r'^#t\d+ ', line):
            out.append(html.escape(line))  # Task labels/titles are literal display text.
        else:
            heading = re.match(r"^#{1,6} (.*?)(\n?)$", line)
            out.append(f"<b>{_inline(heading[1])}</b>{heading[2]}" if heading else _inline(line))
    if code is not None:  # Partial streamed code block: close it for Telegram.
        out.append("<pre><code>" + html.escape("".join(code)) + "</code></pre>")
    return "".join(out)


class _Tokens(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.tokens = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = "".join(f' {k}="{html.escape(v, quote=True)}"' for k, v in attrs)
        self.tokens.append(("open", tag, f"<{tag}{attrs}>"))

    def handle_endtag(self, tag):
        self.tokens.append(("close", tag, f"</{tag}>"))

    def handle_data(self, data):
        self.tokens.append(("text", "", data))


def plain_text(rendered: str) -> str:
    return "".join(value for kind, _, value in _Tokens(rendered).tokens if kind == "text")


def split_html(rendered: str, limit=4096) -> list[str]:
    """Conservatively bound serialized HTML UTF-16 length, including entities.

    Tags and entities are never cut. A long fenced block is closed and reopened
    in successive messages. This also bounds Telegram's parsed text length.
    """
    if limit < 64:
        raise ValueError("Chunk limit must be at least 64")
    chunks, stack, parts = [], [], []
    size, has_text = 0, False

    def endings():
        return "".join(f"</{tag}>" for tag, _ in reversed(stack))

    def flush():
        nonlocal parts, size, has_text
        if has_text:
            chunks.append("".join(parts) + endings())
        parts = [opening for _, opening in stack]
        size = sum(utf16_len(p) for p in parts)
        has_text = False

    for kind, tag, value in _Tokens(rendered).tokens:
        if kind == "open":
            if size + utf16_len(value + f"</{tag}>" + endings()) > limit:
                flush()
            if size + utf16_len(value + f"</{tag}>" + endings()) > limit - 12:
                # Defensive limit for pathological nesting/URLs in small chunks.
                raise ValueError("Formatting overhead exceeds chunk limit")
            stack.append((tag, value))
            parts.append(value)
            size += utf16_len(value)
        elif kind == "close":
            parts.append(value)
            size += utf16_len(value)
            stack.pop()
        else:
            for char in value:
                escaped = html.escape(char)
                if size + utf16_len(escaped + endings()) > limit:
                    flush()
                parts.append(escaped)
                size += utf16_len(escaped)
                has_text = True
    flush()
    return chunks


def render_chunks(text: str) -> list[str]:
    rendered = markdown_to_html(text)
    try:
        return split_html(rendered)
    except ValueError:
        return split_html(html.escape(plain_text(rendered)))
