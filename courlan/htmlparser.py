"""
Incremental HTML parsing for link extraction, an alternative to the
regular-expression based extraction in core.py.

The parser accepts text in arbitrarily small chunks (feed()) and only
finalizes on close(). Memory use stays bounded by the number of link
candidates: consumed text is dropped as soon as it is processed and only
not-yet-closed tags, comment or raw-text delimiters are kept in the buffer.
"""

import re
from html import unescape
from urllib.parse import urlsplit

__all__ = ["HtmlLinkParser"]

_TAGNAME_REGEX = re.compile(r"[a-zA-Z][a-zA-Z0-9]*")
# name, then optionally ="..." / ='...' / =value; quoted values may span newlines
_ATTR_REGEX = re.compile(r"([^\s/>=]+)(?:\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+)))?")
_RAWTEXT_REGEX = {
    "script": re.compile(r"</script", re.I),
    "style": re.compile(r"</style", re.I),
}
# characters allowed after a raw-text end-tag name
_TAG_DELIMITERS = " \t\n\r\f/>"

_TEXT, _COMMENT, _RAWTEXT = 0, 1, 2


def _parse_attrs(tag: str, start: int) -> dict[str, str]:
    "Parse the attributes of a tag body, first occurrence of a name wins."
    attrs: dict[str, str] = {}
    for match in _ATTR_REGEX.finditer(tag, start):
        name = match[1].lower()
        if name in attrs:
            continue
        value = match[2] if match[2] is not None else match[3]
        if value is None:
            value = match[4]
        attrs[name] = value if value is not None else ""
    return attrs


def _tag_end(buf: str, start: int) -> int:
    "Return the position of the '>' closing a tag, quotes included, or -1."
    quote = None
    for pos in range(start, len(buf)):
        char = buf[pos]
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == ">":
            return pos
    return -1


def _partial_tail(buf: str, pos: int, delimiter: str) -> int:
    "Length of the longest suffix of buf[pos:] which starts delimiter."
    maxk = min(len(delimiter) - 1, len(buf) - pos)
    lower = buf.lower()
    for k in range(maxk, 0, -1):
        if lower.endswith(delimiter[:k]):
            return k
    return 0


class HtmlLinkParser:
    """Incremental extractor of link targets and of the document base URL.

    candidates: (href, hreflang) pairs in document order, nofollow links
    excluded, character entities decoded exactly once. base: the first
    valid absolute HTTP(S) <base href>, whatever its position in the page.
    """

    __slots__ = ("_buf", "_rawtext", "_state", "base", "candidates")

    def __init__(self) -> None:
        self._buf = ""
        self._rawtext = ""
        self._state = _TEXT
        self.base: str | None = None
        self.candidates: list[tuple[str, str | None]] = []

    def feed(self, chunk: str) -> None:
        "Process a text chunk of any size."
        if not isinstance(chunk, str):
            raise TypeError("str expected, got " + type(chunk).__name__)
        self._buf += chunk
        self._consume()

    def close(self) -> tuple[list[tuple[str, str | None]], str | None]:
        "End of input: incomplete constructs are discarded, return the results."
        # unclosed tags, comments and raw-text elements yield nothing
        self._buf = ""
        return self.candidates, self.base

    def _consume(self) -> None:
        "Process as much of the buffer as possible, keep only the open construct."
        buf = self._buf
        pos, end = 0, len(buf)
        while pos < end:
            if self._state == _COMMENT:
                idx = buf.find("-->", pos)
                if idx < 0:
                    pos = end - _partial_tail(buf, pos, "-->")
                    break
                self._state = _TEXT
                pos = idx + 3
            elif self._state == _RAWTEXT:
                closer = _RAWTEXT_REGEX[self._rawtext]
                while True:
                    match = closer.search(buf, pos)
                    if match is None:
                        pos = end - _partial_tail(buf, pos, "</" + self._rawtext)
                        break
                    if match.end() < end and buf[match.end()] not in _TAG_DELIMITERS:
                        pos = match.start() + 1  # e.g. </scripture, not a closer
                        continue
                    idx = buf.find(">", match.end())
                    if idx < 0:
                        pos = match.start()  # keep the partial end tag
                        break
                    self._state = _TEXT
                    pos = idx + 1
                    break
                if self._state == _RAWTEXT:
                    break
            else:  # _TEXT
                idx = buf.find("<", pos)
                if idx < 0:
                    pos = end
                    break
                if idx + 1 >= end:
                    break  # a lone '<' may start a construct
                char = buf[idx + 1]
                if char == "!":
                    if buf.startswith("<!--", idx):
                        self._state = _COMMENT
                        pos = idx + 4
                        continue
                    if len(buf) - idx < 4 and "<!--".startswith(buf[idx : idx + 4]):
                        break  # may still become a comment
                    idx2 = buf.find(">", idx + 2)  # doctype or declaration
                    if idx2 < 0:
                        break
                    pos = idx2 + 1
                elif char in "?/":
                    idx2 = buf.find(">", idx + 2)
                    if idx2 < 0:
                        break
                    pos = idx2 + 1
                elif "a" <= char <= "z" or "A" <= char <= "Z":
                    idx2 = _tag_end(buf, idx + 1)
                    if idx2 < 0:
                        break  # unclosed tag, kept whole
                    self._handle_tag(buf[idx + 1 : idx2])
                    pos = idx2 + 1
                else:
                    pos = idx + 1  # literal '<'
        self._buf = buf[pos:]

    def _handle_tag(self, tag: str) -> None:
        "Handle a complete tag body (between '<' and '>')."
        match = _TAGNAME_REGEX.match(tag)
        if match is None:
            return
        name = match[0].lower()
        if name == "a":
            attrs = _parse_attrs(tag, match.end())
            href = attrs.get("href")
            if href is None:
                return
            rel = attrs.get("rel")
            if rel is not None and "nofollow" in rel.lower().split():
                return
            hreflang = attrs.get("hreflang")
            self.candidates.append(
                (unescape(href), unescape(hreflang) if hreflang else None)
            )
        elif name == "base" and self.base is None:
            href = _parse_attrs(tag, match.end()).get("href")
            if not href:
                return
            href = unescape(href).strip()
            try:
                parts = urlsplit(href)
            except ValueError:
                return
            # the first valid absolute HTTP(S) base wins, later ones are ignored
            if parts.scheme in ("http", "https") and parts.netloc:
                self.base = href
        elif name in _RAWTEXT_REGEX:
            # script/style content is raw text until the matching end tag
            self._state = _RAWTEXT
            self._rawtext = name
