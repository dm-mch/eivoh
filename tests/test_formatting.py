from html.parser import HTMLParser

import pytest

from eivoh.channels.formatting import markdown_to_html, plain_text, render_chunks, split_html, utf16_len


class Balanced(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag


def test_markdown():
    text = '# Title\n**bold** _italics_ ~~gone~~ `x < y` [link](https://example.test/?a=1&b=2)\n```python\na < b\n```\n<script>'
    rendered = markdown_to_html(text)
    assert '<b>Title</b>' in rendered
    assert '<b>bold</b>' in rendered
    assert '<i>italics</i>' in rendered
    assert '<s>gone</s>' in rendered
    assert '<code>x &lt; y</code>' in rendered
    assert '<pre><code>a &lt; b\n</code></pre>' in rendered
    assert 'a=1&amp;b=2' in rendered
    assert '&lt;script&gt;' in rendered
    assert '<script>' not in rendered
    assert '<a ' not in markdown_to_html('[bad](javascript:alert)')


@pytest.mark.parametrize('text', ['x' * 12000, '😀' * 6000, '&<>"' * 3000,
                                  '```python\n' + '😀 <tag> & value\n' * 2000 + '```',
                                  '**' + 'bold ' * 5000 + '**', '```\nunfinished <code>'])
def test_split_balanced_lossless(text):
    chunks = render_chunks(text)
    assert chunks
    for chunk in chunks:
        assert utf16_len(chunk) <= 4096
        parser = Balanced()
        parser.feed(chunk)
        assert not parser.stack
    assert ''.join(plain_text(chunk) for chunk in chunks) == plain_text(markdown_to_html(text))
    if text.startswith('```'):
        assert all('<pre><code>' in chunk and '</code></pre>' in chunk for chunk in chunks)


def test_tiny_limit_and_empty():
    assert render_chunks('') == []
    assert utf16_len('😀a') == 3
    with pytest.raises(ValueError):
        split_html('hello', 5)
