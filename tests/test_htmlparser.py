"""
Tests for the opt-in incremental HTML parsing mode (html_parser=True)
and the courlan.htmlparser.HtmlLinkParser class. All tests run offline.
"""

import random
import tracemalloc

from courlan import UrlStore, extract_links, filter_links
from courlan.htmlparser import HtmlLinkParser

DOC = """<!DOCTYPE html>
<html><head>
<title>Test</title>
<script type="text/javascript">
var link = '<a href="https://example.org/fake-script">x</a>';
if (a < b) { console.log("</a>"); }
</script>
<style>
/* <a href="https://example.org/fake-style"> */
a { color: red; }
</style>
<base href="https://example.org/dir/">
</head>
<body>
<!-- <a href="https://example.org/fake-comment">commented</a> -->
<A HREF="https://example.org/upper">upper</A>
<a
  href='https://example.org/newline'
  rel="noopener">newline</a>
<a href=https://example.org/unquoted>unquoted</a>
<a href="https://example.org/ent?a=1&amp;b=2">entities</a>
<a href="https://example.org/nofollow" rel="ugc nofollow">nofollow</a>
<a href="https://example.org/word-token" rel="unfollow">not a token</a>
<a href="relative/page">relative</a>
text with a < b and 3 > 2 comparison
</body></html>
"""


def _chunks(text, size):
    return [text[i : i + size] for i in range(0, len(text), size)]


def _parse(html, chunk_size=None):
    parser = HtmlLinkParser()
    if chunk_size is None:
        parser.feed(html)
    else:
        for chunk in _chunks(html, chunk_size):
            parser.feed(chunk)
    return parser.close()


def test_chunking_invariance():
    "Any chunking of the input yields the same candidates and base."
    expected_candidates, expected_base = _parse(DOC)
    assert expected_base == "https://example.org/dir/"
    for size in (1, 2, 3, 5, 7, 64, 1024):
        candidates, base = _parse(DOC, size)
        assert candidates == expected_candidates
        assert base == expected_base
    # random splits
    rng = random.Random(42)
    for _ in range(10):
        cuts = sorted(rng.sample(range(1, len(DOC)), 50))
        parser = HtmlLinkParser()
        pos = 0
        for cut in cuts + [len(DOC)]:
            parser.feed(DOC[pos:cut])
            pos = cut
        assert parser.close() == (expected_candidates, expected_base)


def test_parser_features():
    "Attribute case, newlines, entities, nofollow tokens, pseudo-links."
    candidates, base = _parse(DOC)
    hrefs = [href for href, _ in candidates]
    assert base == "https://example.org/dir/"
    assert "https://example.org/upper" in hrefs  # uppercase tag/attribute
    assert "https://example.org/newline" in hrefs  # tag spanning newlines
    assert "https://example.org/unquoted" in hrefs  # unquoted attribute
    assert "https://example.org/ent?a=1&b=2" in hrefs  # entities decoded once
    assert "relative/page" in hrefs
    assert "https://example.org/word-token" in hrefs  # "unfollow" is no token
    # rel="nofollow" word token and pseudo-links are excluded
    assert "https://example.org/nofollow" not in hrefs
    assert not any("fake" in href for href in hrefs)


def test_extract_links_chunking():
    "extract_links(html_parser=True) gives identical results for any chunking."
    url = "https://example.org/dir/page.html"
    expected = extract_links(DOC, url, html_parser=True)
    assert expected  # sanity
    for size in (1, 3, 7, 128):
        result = extract_links(iter(_chunks(DOC, size)), url, html_parser=True)
        assert result == expected
    # a one-shot generator works as well
    result = extract_links((c for c in DOC), url, html_parser=True)
    assert result == expected


def test_default_mode_unchanged():
    "The regex mode stays the default and differs where the parser is stricter."
    html = "<script>var s = '<a href=\"https://example.org/pseudo\">';</script>"
    # default mode: regular expressions see the pseudo-link
    assert extract_links(html, "https://example.org") == {"https://example.org/pseudo"}
    # parser mode: script content is raw text, no link inside
    assert extract_links(html, "https://example.org", html_parser=True) == set()


def test_late_base():
    "A base tag after the links still applies to them, earlier links included."
    html = (
        '<a href="early">1</a><a href="/rooted">2</a>'
        '<base href="https://example.org/dir/">'
        '<a href="late">3</a>'
    )
    assert extract_links(
        html, "https://other.net/page", external_bool=True, html_parser=True
    ) == {
        "https://example.org/dir/early",
        "https://example.org/dir/late",
        "https://example.org/rooted",
    }


def test_first_valid_base_wins():
    "Invalid bases are skipped, the first valid HTTP(S) base is used."
    html = (
        '<base href="/relative">'
        '<base href="ftp://ftp.example.org/">'
        '<base href="https://first.example.org/dir/">'
        '<base href="https://second.example.org/">'
        '<a href="page">x</a>'
    )
    assert extract_links(html, "https://example.org", html_parser=True) == {
        "https://first.example.org/dir/page"
    }
    # no valid base: fall back to the page URL
    html = '<base href="/relative"><a href="page">x</a>'
    assert extract_links(html, "https://example.org/dir/", html_parser=True) == {
        "https://example.org/dir/page"
    }


def test_cross_domain_base():
    "Internal/external is judged against url/reference, not the base."
    html = (
        '<base href="https://cdn.other.net/dir/">'
        '<a href="page">1</a><a href="https://example.org/abs">2</a>'
    )
    url = "https://example.org/start"
    # the base resolves the relative link to another domain...
    assert extract_links(html, url, external_bool=True, html_parser=True) == {
        "https://cdn.other.net/dir/page"
    }
    # ...which makes it external with regard to the page URL
    assert extract_links(html, url, external_bool=False, html_parser=True) == {
        "https://example.org/abs"
    }
    # an explicit reference keeps precedence
    assert extract_links(
        html,
        url,
        external_bool=True,
        reference="https://other.net",
        html_parser=True,
    ) == {"https://example.org/abs"}


def test_pseudo_links():
    "Comments, script and style content never yield links."
    html = (
        '<!-- <a href="https://example.org/comment"> -->\n'
        "<script>var u = '<a href=\"https://example.org/script\">';</script>\n"
        "<style>a::after { content: '<a href=\"https://example.org/style\">'; }</style>\n"
        "<SCRIPT>document.write('<a href=https://example.org/upper-script>');</SCRIPT>\n"
        '<a href="https://example.org/real">real</a>'
    )
    assert extract_links(html, "https://example.org", html_parser=True) == {
        "https://example.org/real"
    }
    # an unterminated comment swallows the rest of the page
    html = '<a href="https://example.org/a">1</a><!-- <a href="https://example.org/b">'
    assert extract_links(html, "https://example.org", html_parser=True) == {
        "https://example.org/a"
    }
    # an unterminated script element swallows the rest of the page
    html = (
        '<a href="https://example.org/a">1</a><script><a href="https://example.org/b">'
    )
    assert extract_links(html, "https://example.org", html_parser=True) == {
        "https://example.org/a"
    }
    # </scripture> is not a script end tag
    html = '<script><a href="https://example.org/x"></scripture><a href="https://example.org/y"></script><a href="https://example.org/z">'
    assert extract_links(html, "https://example.org", html_parser=True) == {
        "https://example.org/z"
    }


def test_double_entities():
    "Character entities are decoded exactly once."
    html = '<a href="https://example.org/?a=1&amp;amp;b=2">x</a>'
    assert extract_links(
        html, "https://example.org", no_filter=True, html_parser=True
    ) == {"https://example.org/?a=1&amp;b=2"}
    html = '<a href="https://example.org/?a=1&amp;b=2">x</a>'
    assert extract_links(
        html, "https://example.org", no_filter=True, html_parser=True
    ) == {"https://example.org/?a=1&b=2"}
    # numeric entities too, and only once
    html = '<a href="https://example.org/?a=1&amp;#38;b=2">x</a>'
    assert extract_links(
        html, "https://example.org", no_filter=True, html_parser=True
    ) == {"https://example.org/?a=1&#38;b=2"}


def test_attributes_case_newlines_entities():
    "Attribute names are case-insensitive, values may contain newlines."
    html = (
        '<A HREF="https://example.org/caps">1</A>'
        '<a HrEf="https://example.org/mixed">2</a>'
        '<a href="https://example.org/broken\nline">3</a>'
        "<a href='https://example.org/single'>4</a>"
        '<a href="https://example.org/x" REL="NoFollow">5</a>'
    )
    assert extract_links(html, "https://example.org", html_parser=True) == {
        "https://example.org/caps",
        "https://example.org/mixed",
        "https://example.org/brokenline",
        "https://example.org/single",
    }


def test_hreflang_parity():
    "The language filter on hreflang behaves like in the default mode."
    html = (
        '<a href="https://example.org/de" hreflang="de-DE">1</a>'
        '<a href="https://example.org/en" hreflang="en">2</a>'
        '<a href="https://example.org/any" hreflang="x-default">3</a>'
        '<a href="https://example.org/none">4</a>'
    )
    result = extract_links(html, "https://example.org", language="de", html_parser=True)
    assert result == {
        "https://example.org/de",
        "https://example.org/any",
        "https://example.org/none",
    }


def test_unclosed_constructs_at_eof():
    "Incomplete constructs at end of input are discarded gracefully."
    parser = HtmlLinkParser()
    parser.feed('<a href="https://example.org/a">1</a><a href="https://example.org/b"')
    candidates, base = parser.close()
    assert candidates == [("https://example.org/a", None)]
    assert base is None
    # a link whose start tag is complete needs no end tag
    parser = HtmlLinkParser()
    parser.feed('<a href="https://example.org/a">')
    assert parser.close()[0] == [("https://example.org/a", None)]
    # empty input
    assert HtmlLinkParser().close() == ([], None)


def test_three_entry_points():
    "extract_links, filter_links and UrlStore.add_from_html agree."
    html = (
        '<base href="https://example.org/dir/">'
        '<a href="a">1</a><a href="b">2</a>'
        '<a href="https://example.org/c">3</a>'
        '<a href="https://other.net/ext">4</a>'
        '<a href="/page/2">5</a>'
        '<!-- <a href="https://example.org/fake"> --></a>'
    )
    url = "https://example.org/start"
    expected = extract_links(
        html, url, external_bool=False, strict=False, with_nav=True, html_parser=True
    )
    assert expected == {
        "https://example.org/dir/a",
        "https://example.org/dir/b",
        "https://example.org/c",
        "https://example.org/page/2",
    }
    # chunked input through filter_links, navigation pages prioritized
    links, links_priority = filter_links(
        iter(_chunks(html, 5)),
        url,
        external=False,
        strict=False,
        with_nav=True,
        html_parser=True,
    )
    assert set(links) | set(links_priority) == expected
    assert links_priority == ["https://example.org/page/2"]
    # UrlStore, fed by a one-shot generator
    store = UrlStore(strict=False)
    store.add_from_html(
        (c for c in _chunks(html, 3)), url, with_nav=True, html_parser=True
    )
    assert set(store.dump_urls()) == expected
    # the navigation page was appended left
    assert store.find_unvisited_urls("https://example.org")[0].endswith("/page/2")


def test_memory_bounded_linkless_body():
    "Memory stays bounded on large linkless bodies, comments and scripts."
    parser = HtmlLinkParser()
    peak = 0
    chunk = "lorem ipsum dolor sit amet, consectetur adipiscing elit " * 16
    for _ in range(2000):  # ~1.8 MB of plain text
        parser.feed(chunk)
        peak = max(peak, len(parser._buf))
    # linkless script and comment bodies are not retained either
    parser.feed("<script>")
    for _ in range(1000):
        parser.feed(chunk)
        peak = max(peak, len(parser._buf))
    parser.feed("</script><!--")
    for _ in range(1000):
        parser.feed(chunk)
        peak = max(peak, len(parser._buf))
    parser.feed("-->")
    candidates, _ = parser.close()
    assert candidates == []
    assert peak < 2 * len(chunk)


def test_memory_bounded_extract_links():
    "extract_links on a chunked linkless body does not retain the body."
    chunk = "plain text without any markup " * 2048  # 60 KB
    repeats = 128  # ~7.7 MB overall

    def body():
        for _ in range(repeats):
            yield chunk

    tracemalloc.start()
    try:
        result = extract_links(body(), "https://example.org", html_parser=True)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result == set()
    assert peak < len(chunk) * repeats // 4
