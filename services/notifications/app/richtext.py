"""Rich text for message bodies.

Messages store formatting as a tiny tag language, the only markup any client renders:

    <b>bold</b>  <i>italic</i>  <u>underline</u>  <a href="https://…">link</a>

Text uses the entities &amp; &lt; &gt; &quot;. Line breaks are plain newlines. Anything else in the
input (other tags, attributes, scripts) is kept as literal text, never as markup. sanitize()
rewrites input into this canonical form: tags balanced, no links inside links, only allowed URLs.
"""
import html
import re
from typing import Callable

TOKEN = re.compile(r'<(/?)(b|i|u|a)(?:\s+href="([^"]*)")?\s*>|[^<]+|<', re.IGNORECASE)


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def sanitize(raw: str, link_ok: Callable[[str], bool]) -> tuple[str, str]:
    """Returns (canonical rich text, plain text). Raises ValueError for a link that isn't allowed."""
    out: list[str] = []
    plain: list[str] = []
    stack: list[str] = []

    for m in TOKEN.finditer(raw.replace("\r\n", "\n").replace("\r", "\n")):
        closing, tag, href = m.group(1), (m.group(2) or "").lower(), m.group(3)
        if not tag:                                   # text, or a stray "<"
            text = html.unescape(m.group(0))
            out.append(escape(text))
            plain.append(text)
        elif closing:
            if tag in stack:                          # close it and anything opened inside it
                while stack:
                    t = stack.pop()
                    out.append(f"</{t}>")
                    if t == tag:
                        break
        elif tag == "a":
            url = html.unescape(href or "").strip()
            if not url or "a" in stack:               # no empty links, no links inside links
                continue
            if not link_ok(url):
                raise ValueError(f"Links in the message must be https on an allowed domain: {url}")
            out.append(f'<a href="{escape(url)}">')
            stack.append("a")
        elif tag not in stack:                        # <b> inside <b> adds nothing
            out.append(f"<{tag}>")
            stack.append(tag)
    while stack:
        out.append(f"</{stack.pop()}>")

    rich = re.sub(r"<(b|i|u|a)(?: [^>]*)?></\1>", "", "".join(out))   # drop empty formatting
    return rich.strip(), "".join(plain).strip()
