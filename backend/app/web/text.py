"""Readable job descriptions: postings arrive as web-page code, as escaped web-page code, or as plain text with
its bullets and headings flattened. Shown as paragraphs, headings and bullet lists.

Safety: only plain formatting survives — paragraphs, line breaks, lists, bold / italic and headings, with no
attributes. Everything else from the posting (links, scripts, styles, images, frames) is dropped; all text is
escaped. Built on the standard library's HTML reader, so no new library.
"""

import html
import re
from html.parser import HTMLParser

from markupsafe import Markup, escape

KEEP = {"p": "p", "br": "br", "ul": "ul", "ol": "ol", "li": "li", "strong": "strong", "b": "strong",
        "em": "em", "i": "em", "h1": "h4", "h2": "h4", "h3": "h4", "h4": "h4", "h5": "h4", "h6": "h4"}
BLOCKS = {"div", "section", "article", "tr", "table", "header", "footer", "blockquote", "dl", "dd", "dt"}
# content dropped too (always closed in practice; a never-closed one would hide the rest, so only these)
DROP = {"script", "style", "noscript", "head", "title", "template", "select", "textarea"}
LOOKS_LIKE_HTML = re.compile(r"<\s*(p|div|br|li|ul|ol|strong|b|em|h[1-6]|span|section|table)\b[^>]*>", re.I)
BULLET = re.compile(r"^\s*(?:[-*•●▪◦·–]|\d{1,2}[.)])\s+")


class _Clean(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.open: list[str] = []
        self.dropping = 0

    def handle_starttag(self, tag, attrs):
        if tag in DROP:
            self.dropping += 1
            return
        if self.dropping:
            return
        if tag in BLOCKS:
            self._close_inline()
            self.out.append("<br>" if self.open else "\n\n")      # inside a paragraph / item: a line break
            return
        new = KEEP.get(tag)
        if new == "br":
            self.out.append("<br>")
        elif new:
            if new in ("p", "h4", "li") and new in self.open[self._last_list() + 1:]:   # an unclosed <p>/<li>
                self._close(new)                                                      # before the next (same list)
            self.out.append(f"<{new}>")
            self.open.append(new)

    def handle_startendtag(self, tag, attrs):
        if not self.dropping and KEEP.get(tag) == "br":
            self.out.append("<br>")

    def handle_endtag(self, tag):
        if tag in DROP:
            self.dropping = max(0, self.dropping - 1)
            return
        if self.dropping:
            return
        if tag in BLOCKS:
            if not self.open:                                   # (inside a paragraph / item: its start did it)
                self.out.append("\n\n")
            return
        new = KEEP.get(tag)
        if new and new != "br" and new in self.open:
            self._close(new)

    def handle_data(self, data):
        if not self.dropping:
            self.out.append(str(escape(re.sub(r"\s+", " ", data))))   # spacing as a browser would show it

    def _close(self, tag: str) -> None:
        while self.open:
            t = self.open.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                return

    def _last_list(self) -> int:
        """Where the innermost open list is (-1: none), so a nested list's items stay nested."""
        return max((i for i, t in enumerate(self.open) if t in ("ul", "ol")), default=-1)

    def _close_inline(self) -> None:
        while self.open and self.open[-1] in ("strong", "em"):
            self.out.append(f"</{self.open.pop()}>")

    def result(self) -> str:
        while self.open:
            self.out.append(f"</{self.open.pop()}>")
        text = "".join(self.out)
        text = re.sub(r"(?:<br>\s*){3,}", "<br><br>", text)
        text = re.sub(r"(<(p|li|h4)>)\s*(?:<br>\s*)+|(?:<br>\s*)+(</(p|li|h4)>)", r"\1\3", text)   # edge breaks
        text = re.sub(r"<(p|li|h4|strong|em)>\s*</\1>", "", text)          # empty ones
        # loose text between blocks becomes paragraphs
        parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        return "\n".join(p if p.startswith(("<p>", "<ul>", "<ol>", "<h4>")) else f"<p>{p}</p>" for p in parts)


def _from_html(raw: str) -> str:
    parser = _Clean()
    parser.feed(raw)
    parser.close()
    return parser.result()


def _is_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 70 or s.endswith((".", ",", ";")):
        return False
    words = s.rstrip(":").split()
    return s.endswith(":") or (len(words) <= 6 and (s.isupper() or all(w[:1].isupper() for w in words if w[0].isalpha())))


def _from_text(raw: str) -> str:
    out: list[str] = []
    for block in re.split(r"\n\s*\n", raw.replace("\r\n", "\n").strip()):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        items: list[str] = []
        para: list[str] = []

        def flush_para():
            if para:
                out.append("<p>" + "<br>".join(str(escape(x)) for x in para) + "</p>")
                para.clear()

        def flush_items():
            if items:
                out.append("<ul>" + "".join(f"<li>{escape(x)}</li>" for x in items) + "</ul>")
                items.clear()

        for ln in lines:
            if BULLET.match(ln):
                flush_para()
                items.append(BULLET.sub("", ln))
            elif _is_heading(ln) and len(lines) > 1:
                flush_para()
                flush_items()
                out.append(f"<h4>{escape(ln.rstrip(':'))}</h4>")
            else:
                flush_items()
                para.append(ln)
        flush_para()
        flush_items()
    return "\n".join(out)


def rich_text(raw: str | None) -> Markup:
    """A posting's description, ready to show (see the module note for what is kept)."""
    if not raw or not raw.strip():
        return Markup("")
    text = raw
    if not LOOKS_LIKE_HTML.search(text) and "&lt;" in text and LOOKS_LIKE_HTML.search(html.unescape(text)):
        text = html.unescape(text)       # escaped web-page code (a real page's "&lt;div&gt;" example stays text)
    if LOOKS_LIKE_HTML.search(text):
        return Markup(_from_html(text))
    return Markup(_from_text(html.unescape(text)))
