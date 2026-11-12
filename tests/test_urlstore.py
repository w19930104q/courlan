"""
Unit tests for the UrlStore class of the courlan package.
"""

import bz2
import os
import pickle
import signal
import sys
import threading
import uuid
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta
from time import sleep
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import pytest

import courlan
import courlan.urlstore as urlstore_module
from courlan import UrlStore, load_store
from courlan.urlstore import (
    _NEAR,
    DEFAULT_LEASE_DURATION,
    HAS_BZ2,
    HAS_ZLIB,
    Compressor,
    DomainEntry,
    Lease,
    State,
    UrlPathTuple,
)


def test_compressor():
    "Test compression class."
    assert HAS_BZ2 or HAS_ZLIB
    data = 1234

    for setting in (True, False):
        comp = Compressor(compression=setting)
        assert comp.decompress(comp.compress(data)) == data


def test_compressor_bz2(monkeypatch):
    "Test the bz2 compression fallback when zlib is unavailable."
    monkeypatch.setattr("courlan.urlstore.HAS_ZLIB", False)
    comp = Compressor(compression=True)
    assert comp.compress(1234)[:3] == b"BZh"
    assert comp.decompress(comp.compress(1234)) == 1234


@pytest.fixture
def robots_rules():
    "Return a RobotFileParser with an allow-all ruleset for sitemaps.org."
    rules = RobotFileParser()
    rules.set_url("https://sitemaps.org/robots.txt")
    # empty/allow-all robots.txt; parse() also sets last_checked
    rules.parse(["User-agent: *", "Disallow:"])
    return rules


def _example_dataset():
    "Build a large set of example and (random) test URLs for store tests."
    example_domain = "https://www.example.org"
    example_urls = [f"{example_domain}/{a}" for a in range(10000)]
    test_urls = [f"https://test.org/{str(uuid.uuid4())[:20]}" for _ in range(10000)]
    return example_domain, example_urls, test_urls


def test_urlstore_basics():
    "Test basic addition and reset of the URL store."
    my_urls = UrlStore()
    candidates = [
        "123",
        "http://",
        "ftp://example.org",
        "https://example.org/",
        "http://example.org/",
        "https://example.org/1",
        "http://example.org/1",
    ]
    my_urls.add_urls(candidates)
    assert len(my_urls.urldict) == 1 and "http://example.org" not in my_urls.urldict
    tuples = my_urls.urldict["https://example.org"].tuples
    assert isinstance(tuples, deque) and len(tuples) == 2
    firstelem = tuples[0]
    assert firstelem.urlpath == b"/" and firstelem.visited is False
    # host and path come from the normalized parts: query and fragment are kept,
    # and query keys that are HTML entity names are no longer mangled
    parts = UrlStore()
    parts.add_urls(
        [
            "https://example.org/t?other=1&param=2",
            "https://example.org/a?b=1#frag",
            "https://example.org/#frag",
            "https://example.org/",
        ]
    )
    assert parts.dump_urls() == [
        "https://example.org/t?other=1&param=2",
        "https://example.org/a?b=1#frag",
        "https://example.org/#frag",  # root slash preserved when fragment present
        "https://example.org/",
    ]
    # round-trip: is_known must find URLs that were just added
    assert parts.is_known("https://example.org/#frag")
    assert parts.is_known("https://example.org/a?b=1#frag")
    # reset releases the hot domain and its path index
    assert my_urls._hot is not None and my_urls._index is not None
    my_urls.reset()
    assert not my_urls.urldict
    assert my_urls._hot is None and my_urls._index is None

    candidates = [
        "http://example.org/",
        "https://example.org/",
        "http://example.org/1",
        "https://example.org/1",
    ]
    my_urls.add_urls(candidates)
    assert len(my_urls.urldict) == 1 and "http://example.org" not in my_urls.urldict
    assert len(my_urls.urldict["https://example.org"].tuples) == 2

    # slash variants dedup within a batch too
    my_urls.reset()
    my_urls.add_urls(["https://example.org/a", "https://example.org/a/"])
    assert len(my_urls.urldict["https://example.org"].tuples) == 1
    # same for the appendleft path
    my_urls.reset()
    my_urls.add_urls(appendleft=["https://example.org/b", "https://example.org/b/"])
    assert len(my_urls.urldict["https://example.org"].tuples) == 1

    # visited flag defaults
    v = UrlStore()
    v.add_urls(["http://a.com/x"])
    assert v.find_unvisited_urls("http://a.com") == ["http://a.com/x"]
    assert not v.has_been_visited("http://a.com/x")
    v2 = UrlStore()
    v2.add_urls(["http://a.com/y"], visited=True)
    assert v2.find_unvisited_urls("http://a.com") == []
    assert v2.has_been_visited("http://a.com/y")

    # invalid URLs rejected
    inv = UrlStore()
    inv.add_urls(["not-a-url", "http://valid.org/ok"])
    assert inv.dump_urls() == ["http://valid.org/ok"]


def test_urlstore_rules(robots_rules):
    "Test storage and retrieval of crawling rules."
    my_urls = UrlStore()
    my_urls.add_urls(["https://example.org/", "https://example.org/1"])

    my_urls.store_rules("https://example.org", robots_rules)
    assert my_urls.get_rules("http://test.org") is None
    assert my_urls.urldict["https://example.org"].rules is not None
    assert (
        my_urls.get_rules("https://example.org")
        == my_urls.urldict["https://example.org"].rules
        == robots_rules
    )
    assert my_urls.get_crawl_delay("http://test.org", default=2) == 2
    assert my_urls.get_crawl_delay("https://example.org") == 5

    assert my_urls.compressed is False
    my_urls.urldict["https://example.org"].rules = None

    my_urls.compressed = True
    my_urls.store_rules("https://example.org", robots_rules)
    assert my_urls.urldict["https://example.org"].rules is not None
    # no identity check since different location after compression
    assert my_urls.get_rules("https://example.org").mtime() == robots_rules.mtime()
    my_urls.compressed = False

    # http/https twin is canonicalized, no separate entry created
    my_urls.store_rules("http://example.org", robots_rules)
    assert "http://example.org" not in my_urls.urldict
    assert my_urls.get_rules("https://example.org") == robots_rules
    # ... and reads resolve to the same entry
    assert my_urls.get_rules("http://example.org") == robots_rules
    assert my_urls.get_crawl_delay("http://example.org") == 5
    assert my_urls.get_download_urls(time_limit=0)
    # an http-only entry is upgraded to the https key on lookup
    my_urls.add_urls(["http://a.com/x"])
    my_urls.store_rules("http://a.com", robots_rules)
    assert my_urls.get_rules("https://a.com") is robots_rules


def test_urlstore_filters():
    "Test language and strictness filters applied on insertion."
    my_urls = UrlStore(language="en", strict=True)
    candidates = [
        "https://de.wikipedia.org/",
        "https://www.sitemaps.org/en_GB/protocol.html",
        "http://example.com/de/test.html",
    ]
    my_urls.add_urls(candidates)
    assert len(my_urls.urldict) == 1 and "https://www.sitemaps.org" in my_urls.urldict
    my_urls.add_urls(
        ["https://www.sitemaps.org/es/1"], appendleft=["https://www.sitemaps.org/fi/2"]
    )
    assert len(my_urls.urldict["https://www.sitemaps.org"].tuples) == 1
    # pruning
    assert not my_urls.done
    my_urls.urldict["https://www.sitemaps.org"].state = State.ALL_VISITED
    my_urls._set_done()
    assert my_urls.done


def test_urlstore_compression():
    "Test that compression reduces the in-memory footprint."
    example_domain, example_urls, test_urls = _example_dataset()
    urls = example_urls + test_urls

    # test loading
    url_buffer = UrlStore()._buffer_urls(visited=False, data=urls)
    assert sum(len(v) for _, v in url_buffer.items()) == len(urls)

    # compression 1
    my_urls = UrlStore(compressed=True)
    url_buffer = UrlStore()._buffer_urls(visited=False, data=example_urls)
    my_urls.add_urls(example_urls)
    assert my_urls.total_url_number() == len(example_urls)
    assert len(pickle.dumps(my_urls)) < len(pickle.dumps(url_buffer))
    assert my_urls.is_known(f"{example_domain}/100") is True
    # compression 2
    my_urls = UrlStore(compressed=True)
    url_buffer = UrlStore()._buffer_urls(visited=False, data=test_urls)
    my_urls.add_urls(test_urls)
    assert my_urls.total_url_number() == len(test_urls)
    assert len(pickle.dumps(my_urls)) < len(pickle.dumps(url_buffer))


def test_urlstore_discard():
    "Test discarding domains and pruning the store."
    _, example_urls, test_urls = _example_dataset()
    urls = example_urls + test_urls

    my_urls = UrlStore()
    my_urls.add_urls(urls)
    ref_num_domains = my_urls.get_known_domains()

    assert my_urls.total_url_number() != 0
    my_urls.discard(my_urls.get_known_domains())
    assert (
        my_urls.total_url_number() == 0
        and my_urls.get_known_domains() == ref_num_domains
        and not my_urls.get_unvisited_domains()
        and my_urls.done is True
    )
    my_urls.add_urls(
        ["https://www.example.org/1", "https://test.org/1", "https://www.other.org/1"]
    )
    assert (
        my_urls.total_url_number() == 1
        and len(my_urls.get_known_domains()) == 3
        and my_urls.get_unvisited_domains() == ["https://www.other.org"]
        and my_urls.done is False
    )


def test_urlstore():
    "Test crawl-oriented retrieval and scheduling on the URL store."
    example_domain, example_urls, test_urls = _example_dataset()
    urls = example_urls + test_urls

    my_urls = UrlStore()
    my_urls.add_urls(urls)
    assert sum(len(my_urls._load_urls(k)) for k, _ in my_urls.urldict.items()) == len(
        urls
    )
    assert my_urls.total_url_number() == len(urls)
    assert my_urls.get_all_counts() == [0, 0]

    assert sum(len(v.tuples) for _, v in my_urls.urldict.items()) == len(urls)
    my_urls.add_urls(["https://visited.com/visited"], visited=True)
    tuples = my_urls.urldict["https://visited.com"].tuples
    assert isinstance(tuples, deque) and tuples[0].visited is True
    assert my_urls.urldict["https://visited.com"].state is State.ALL_VISITED
    assert not my_urls.find_unvisited_urls("https://visited.com")
    assert my_urls.is_exhausted_domain("https://visited.com") is True
    # new unvisited URLs
    my_urls.add_urls(["https://visited.com/1"], visited=False)
    assert tuples[1].visited is False
    # mypy keeps the State.ALL_VISITED narrowing across the add_urls call
    assert my_urls.urldict["https://visited.com"].state is State.OPEN  # type: ignore[comparison-overlap]
    assert my_urls.is_exhausted_domain("https://visited.com") is False
    assert my_urls.is_exhausted_domain("https://visited2.com") is False
    # revert changes for further tests
    del my_urls.urldict["https://visited.com"].tuples[1]
    my_urls.urldict["https://visited.com"].state = State.ALL_VISITED

    # test extension
    extension_urls = [f"{example_domain}/1/{a}" for a in range(10)]
    my_urls.add_urls(extension_urls)
    assert len(my_urls._load_urls(example_domain)) == len(example_urls) + 10
    # test extension + deduplication
    my_urls.trailing_slash = False
    extension_urls = [f"{example_domain}/1/{a}/" for a in range(11)]
    my_urls.add_urls(appendleft=extension_urls)
    url_tuples = my_urls._load_urls(example_domain)
    assert len(url_tuples) == len(example_urls) + 11
    assert url_tuples[-1].urlpath == b"/1/9" and url_tuples[0].urlpath == b"/1/10"

    # duplicates
    my_urls.add_urls(extension_urls)
    my_urls.add_urls(appendleft=extension_urls)
    assert len(my_urls._load_urls(example_domain)) == len(example_urls) + len(
        extension_urls
    )
    assert url_tuples[-1].urlpath == b"/1/9" and url_tuples[0].urlpath == b"/1/10"

    # get_url
    assert my_urls.urldict[example_domain].timestamp is None
    assert my_urls.urldict[example_domain].count == 0

    url1 = my_urls.get_url(example_domain)
    timestamp = my_urls.urldict[example_domain].timestamp
    sleep(0.1)
    url2 = my_urls.get_url(example_domain)
    assert url1 != url2 and url1 == "https://www.example.org/1/10"
    assert my_urls.urldict[example_domain].count == 2
    assert timestamp != my_urls.urldict[example_domain].timestamp
    assert url2 not in set(my_urls.find_unvisited_urls(example_domain))
    assert my_urls.get_all_counts() == [2, 0, 0]

    # as_visited=False
    timestamp = my_urls.urldict[example_domain].timestamp
    url3 = my_urls.get_url(example_domain, as_visited=False)
    assert url3 not in (url1, url2)
    assert my_urls.urldict[example_domain].count == 2
    assert timestamp == my_urls.urldict[example_domain].timestamp
    assert url3 in set(my_urls.find_unvisited_urls(example_domain))

    url_tuples = my_urls._load_urls(example_domain)
    # positions
    assert url1.endswith(url_tuples[0].urlpath.decode("utf-8")) and url2.endswith(
        url_tuples[1].urlpath.decode("utf-8")
    )
    # timestamp
    assert my_urls.urldict[example_domain].timestamp is not None
    # nothing left
    assert my_urls.urldict[example_domain].state is State.OPEN
    my_urls.add_urls(["http://tovisit.com/page"])
    assert my_urls.get_url("http://tovisit.com") == "http://tovisit.com/page"
    assert my_urls.urldict["http://tovisit.com"].state is State.ALL_VISITED
    assert my_urls.get_url("http://tovisit.com") is None

    # known domains
    assert my_urls.get_known_domains() == [
        "https://www.example.org",
        "https://test.org",
        "https://visited.com",
        "http://tovisit.com",
    ]

    # known or not
    assert my_urls.is_known("http://tovisit.com/page") is True
    assert my_urls.is_known("https://www.other.org/1") is False
    assert my_urls.is_known("https://www.example.org/1") is True
    candidates = [
        "https://test.org/category/this",
        "https://www.example.org/1",
        "https://otherdomain.org/",
    ]
    assert my_urls.filter_unknown_urls(candidates) == [
        "https://test.org/category/this",
        "https://otherdomain.org/",
    ]
    # visited or not
    assert (
        url_tuples[0].visited is True
        and url_tuples[1].visited is True
        and url_tuples[2].visited is False
    )
    assert my_urls.has_been_visited("http://tovisit.com/page") is True
    assert my_urls.urldict["http://tovisit.com"].state is State.ALL_VISITED
    assert not my_urls.filter_unvisited_urls(["http://tovisit.com/page"])
    assert my_urls.filter_unvisited_urls(["http://tovisit.com/otherpage"]) == [
        "http://tovisit.com/otherpage"
    ]
    assert my_urls.has_been_visited("https://www.other.org/1") is False
    assert my_urls.has_been_visited(url1) is True
    assert my_urls.has_been_visited(f"{example_domain}/this") is False
    assert my_urls.has_been_visited(f"{example_domain}/999") is False
    candidates = [url1, f"{example_domain}/this", f"{example_domain}/999"]
    assert my_urls.filter_unvisited_urls(candidates) == [
        example_domain + "/this",
        example_domain + "/999",
    ]
    assert (
        len(my_urls.find_known_urls(example_domain))
        == len(my_urls._load_urls(example_domain))
        == my_urls.urldict[example_domain].total
        == 10011
    )
    assert len(my_urls.find_unvisited_urls(example_domain)) == 10009
    assert (
        my_urls.unvisited_websites_number() == len(my_urls.get_unvisited_domains()) == 2
    )
    assert my_urls.total_url_number() == 20014

    # get download URLs
    downloadable_urls = my_urls.get_download_urls(time_limit=0, max_urls=1)
    assert (
        len(downloadable_urls) == 1
        and downloadable_urls[0] == "https://www.example.org/1"
    )
    assert (
        datetime.now() - my_urls.urldict["https://www.example.org"].timestamp
    ).total_seconds() < 0.25
    assert my_urls.urldict["https://www.example.org"].count == 3

    test_urls = UrlStore()
    test_urls.add_urls(
        ["https://www.example.org/1", "https://test.org/1", "https://test.org/2"]
    )

    downloadable_urls = test_urls.get_download_urls(time_limit=0)
    assert (
        len(downloadable_urls) == 2
        and downloadable_urls[0].startswith("https://www.example.org")
        and downloadable_urls[1].startswith("https://test.org")
        and test_urls.urldict["https://test.org"].count == 1
    )
    downloadable_urls = test_urls.get_download_urls()
    assert not downloadable_urls

    other_store = UrlStore()
    downloadable_urls = other_store.get_download_urls()
    assert not downloadable_urls and other_store.done is True

    # schedule
    schedule = other_store.establish_download_schedule()
    assert not schedule
    # store exhaustion
    other_store.add_urls(["https://www.test.org/1"])
    assert other_store.done is False
    other_store = UrlStore()
    other_store.add_urls(
        ["http://domain.fi/page1", "http://domain.fi/page2", "http://domain.no/0"]
    )
    schedule = other_store.establish_download_schedule()
    assert len(schedule) == 3
    # reaching buffer limit, starting after the domain get_download_urls served last
    schedule = my_urls.establish_download_schedule(max_urls=1, time_limit=1)
    assert (
        len(schedule) == 1
        and round(schedule[0][0]) == 0
        and schedule[0][1].startswith("https://test.org")
    )
    schedule = my_urls.establish_download_schedule(max_urls=6, time_limit=1)
    assert len(schedule) == 6 and round(max(s[0] for s in schedule)) == 3
    assert my_urls.urldict["https://www.example.org"].count == 6
    assert (
        my_urls.urldict["https://test.org"].count
        == 4
        == sum(u.visited is True for u in my_urls.urldict["https://test.org"].tuples)
    )
    assert my_urls.download_threshold_reached(7) is False
    assert my_urls.download_threshold_reached(6) is True


def test_dbdump(capsys):
    "Test cases where the URLs are dumped or printed."

    # database dump
    first_one = UrlStore()
    first_one.add_urls(["http://example.org/print", "http://print.org/print"])
    assert first_one.dump_urls() == [
        "http://example.org/print",
        "http://print.org/print",
    ]

    # database print out
    other_one = UrlStore()
    other_one.add_urls(["http://test.org/this"])
    other_one.print_urls()
    captured = capsys.readouterr()
    assert captured.out.strip() == "http://test.org/this\tFalse"

    # print unvisited (direct call, works on all platforms)
    unvisited_store = UrlStore()
    unvisited_store.add_urls(["https://www.test.org/a", "https://www.test.org/b"])
    unvisited_store.print_unvisited_urls()
    assert capsys.readouterr().out == "https://www.test.org/a\nhttps://www.test.org/b\n"

    # no blank line for exhausted or discarded hosts
    mixed_store = UrlStore()
    mixed_store.add_urls(["https://www.test.org/a", "https://other.org/x"])
    mixed_store.get_url("https://other.org")
    mixed_store.print_unvisited_urls()
    assert capsys.readouterr().out == "https://www.test.org/a\n"
    mixed_store.discard(["https://www.test.org"])
    mixed_store.print_urls()
    assert capsys.readouterr().out == "https://other.org/x\tTrue\n"

    # dump unvisited, don't test it on Windows
    if os.name != "nt":
        # standard: no signal handler registered
        handler = signal.getsignal(signal.SIGINT)
        UrlStore().add_urls(["https://www.test.org/1", "https://www.test.org/2"])
        assert signal.getsignal(signal.SIGINT) is handler

        # verbose: opt-in registration, a later store takes over
        UrlStore(verbose=True)
        interrupted_one = UrlStore(verbose=True)
        interrupted_one.add_urls(["https://www.test.org/1", "https://www.test.org/2"])
        # SIGINT + SIGTERM caught
        pid = os.getpid()
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                with pytest.raises(SystemExit) as exc_info:
                    os.kill(pid, signum)
                assert exc_info.value.code == 1
                captured = capsys.readouterr()
                assert captured.out.strip().endswith("https://www.test.org/2")
        finally:
            # clean up for the rest of the test session
            signal.signal(signal.SIGINT, signal.default_int_handler)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)


def test_verbose_outside_main_thread(caplog):
    "Signal registration fails outside the main thread but is not fatal."
    errors = []

    def run():
        try:
            UrlStore(verbose=True)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=run)
    with caplog.at_level("WARNING", logger="courlan.urlstore"):
        thread.start()
        thread.join()

    assert not errors
    # no registration attempt on Windows, hence no warning
    if os.name != "nt":
        assert "outside the main thread" in caplog.text


def test_from_html(robots_rules):
    "Test link extraction procedures."
    url_store = UrlStore()
    url_store.trailing_slash = False
    base_url = "https://example.org"
    htmlstring = '<html><body><a href="https://example.com/page1"/><a href="https://example.org/page1/"/><a href="https://test.org/page1"/></body></html>'
    # 1 internal link in total
    url_store.add_from_html(htmlstring, base_url)
    url_store.store_rules("https://example.org", robots_rules)
    known = ["https://example.org/page1"]
    assert url_store.find_known_urls(base_url) == known
    assert url_store.find_unvisited_urls(base_url) == known
    # same with content already seen
    url_store.add_from_html(htmlstring, base_url)
    assert url_store.find_unvisited_urls(base_url) == known
    # test navigation links
    htmlstring = '<html><body><a href="https://example.org/tag/number1"/><a href="https://example.org/page2"/><a href="https://example.org/login"/><a href="https://example.org/imprint.html"/></body></html>'
    url_store.add_from_html(htmlstring, base_url)
    known = ["https://example.org/tag/number1", *known, "https://example.org/page2"]
    assert url_store.find_unvisited_urls(base_url) == known
    # test cleaning and language
    htmlstring = '<html><body><a href="https://example.org/en/page1/?"/></body></html>'
    url_store.add_from_html(htmlstring, base_url, lang="en")
    known.append("https://example.org/en/page1")
    assert url_store.find_unvisited_urls(base_url) == known
    # wrong language
    htmlstring = '<html><body><a href="https://example.org/en/page2"/></body></html>'
    url_store.add_from_html(htmlstring, base_url, lang="de")
    assert url_store.find_known_urls(base_url) == known

    # strict + language as URL store parameters
    url_store = UrlStore(strict=True, language="de")
    base_url = "https://example.org"
    htmlstring = '<html><body><a href="https://example.org/en/page2"/><a href="https://example.org/imprint.html"/></body></html>'
    url_store.add_from_html(htmlstring, base_url)
    assert not url_store.find_known_urls(base_url)

    # stored robots rules filter the links
    url_store = UrlStore()
    rules = RobotFileParser()
    rules.parse(["User-agent: *", "Disallow: /private/"])
    url_store.add_urls(["https://example.org/"])
    url_store.store_rules(base_url, rules)
    htmlstring = '<html><body><a href="/private/a"/><a href="/public/a"/></body></html>'
    url_store.add_from_html(htmlstring, base_url)
    assert url_store.find_known_urls(base_url) == [
        "https://example.org/",
        "https://example.org/public/a",
    ]


def test_persistance(tmp_path):
    "Test writing and loading to/from disk."
    url_store = UrlStore(
        compressed=True, language="de", strict=True, trailing_slash=True, verbose=True
    )
    example_urls = [f"https://www.example.org/{a}" for a in range(100)]
    test_urls = [f"https://test.org/{str(uuid.uuid4())[:20]}" for _ in range(100)]
    url_store.add_urls(example_urls + test_urls)

    tmp = str(tmp_path / "urlstore.pickle")
    url_store.write(tmp)
    # the original store must stay usable after write()
    # (regression: write() used to delete self._lock, breaking later calls)
    url_store.add_urls(["https://www.example.org/after-write"])
    assert url_store.is_known("https://www.example.org/after-write")
    new_store = load_store(tmp)

    assert new_store.compressed is True
    assert new_store.language == "de"
    assert new_store.strict is True
    assert new_store.trailing_slash is True
    urls = set(new_store.dump_urls())
    assert new_store.total_url_number() == len(urls) == 200
    assert "https://www.example.org/99" in urls
    assert "load_store" in courlan.__all__


def test_urlstore_open_but_all_visited():
    "An OPEN domain whose URLs are all visited yields nothing and self-corrects its state."
    store = UrlStore()
    store.add_urls(["https://example.org/1"])
    entry = store.urldict["https://example.org"]
    assert isinstance(entry.tuples, deque)
    entry.tuples[0].visited = True  # mark the only URL as visited
    entry.state = State.OPEN  # but leave the domain marked OPEN
    # get_download_urls -> get_url finds nothing, returns None, and fixes the state
    assert store.get_download_urls() == []
    assert entry.state == State.ALL_VISITED


def test_urlstore_compressed_defaults():
    "Compressed store handles unset rules/tuples and discard gracefully."
    s = UrlStore(compressed=True)
    s.add_urls(["http://x.com/a"])

    assert s.get_rules("http://x.com") is None
    assert s.get_crawl_delay("http://x.com") == 5

    s.discard(["http://x.com"])
    assert s.dump_urls() == []

    zero_rules = RobotFileParser()
    zero_rules.parse(["User-agent: *", "Disallow:", "Crawl-delay: 0"])
    delay_store = UrlStore()
    delay_store.add_urls(["http://x.com/a"])
    delay_store.store_rules("http://x.com", zero_rules)
    assert delay_store.get_crawl_delay("http://x.com") == 0


def test_schedule_timing():
    "Schedule entries start at 0, are time-ordered and set the domain timestamp."
    s = UrlStore()
    s.add_urls(["http://x.com/z", "http://x.com/a"])  # not in alphabetical order
    before = datetime.now()
    schedule = s.establish_download_schedule(max_urls=10, time_limit=10)
    assert schedule == [(0.0, "http://x.com/z"), (10.0, "http://x.com/a")]
    ts = s.urldict["http://x.com"].timestamp
    assert ts is not None and ts >= before


@pytest.mark.parametrize(
    ("domains", "max_urls", "expected"),
    [
        # floor division: 7 // 3 = 2 per domain
        (("a.com", "b.com", "c.com"), 7, {"a.com": 2, "b.com": 2, "c.com": 2}),
        # falls back to 1 per domain when max_urls < number of domains
        (("a.com", "b.com", "c.com"), 2, {"a.com": 1, "b.com": 1}),
        (("a.com",), 3, {"a.com": 3}),
    ],
)
def test_schedule_per_domain(domains, max_urls, expected):
    "establish_download_schedule spreads max_urls evenly across domains."
    s = UrlStore()
    for d in domains:
        s.add_urls([f"http://{d}/{i}" for i in range(5)])
    schedule = s.establish_download_schedule(max_urls=max_urls, time_limit=10)
    assert Counter(urlsplit(url).netloc for _, url in schedule) == expected


def test_urlstore_compressed_crawl():
    "get_url and establish_download_schedule persist visited flags in compressed mode."
    s = UrlStore(compressed=True)
    s.add_urls(["http://x.com/a", "http://x.com/b"])
    urls = [s.get_url("http://x.com") for _ in range(3)]
    assert urls == ["http://x.com/a", "http://x.com/b", None]
    assert s.is_exhausted_domain("http://x.com")
    assert s.urldict["http://x.com"].count == 2

    s2 = UrlStore(compressed=True)
    s2.add_urls(["http://y.com/a", "http://y.com/b"])
    run1 = s2.establish_download_schedule(max_urls=10, time_limit=10)
    assert run1 == [(0.0, "http://y.com/a"), (10.0, "http://y.com/b")]
    assert s2.establish_download_schedule(max_urls=10, time_limit=10) == []


def test_urlstore_store_non_http_domain():
    "_store_urls tolerates a domain without an http(s) scheme (defensive fall-through)."
    store = UrlStore()
    store._store_urls("ftp://example.org", deque())
    assert list(store.urldict) == ["ftp://example.org"]


def test_urlstore_unparsable_port():
    "Readers treat a port over int()'s digit limit as an unknown domain."
    store = UrlStore()
    store.add_urls(["https://a.org/x"])
    url = "http://a.org:" + "1" * 5000
    assert store.is_exhausted_domain(url) is False
    assert store.get_url(url) is None
    assert store.find_known_urls(url) == []
    assert store.get_rules(url) is None
    assert not store.done


def test_store_rules_merges_twin():
    "store_rules canonicalizes under the https key, merging twins."
    store = UrlStore()
    # force two independent entries (simulates legacy unpickled data)
    store.urldict["http://host.com"] = DomainEntry()
    store.urldict["https://host.com"] = DomainEntry()
    rules = RobotFileParser()
    store.store_rules("http://host.com", rules)
    # twins merged under https key
    assert list(store.urldict) == ["https://host.com"]
    assert store.get_rules("https://host.com") is rules


@pytest.mark.parametrize("compressed", [False, True])
def test_lookup_keeps_http_key(compressed):
    "Asking for the https form neither re-keys an http-only domain nor invents URLs."
    store = UrlStore(compressed=compressed)
    store.add_urls(["http://x.org/a"])
    assert store.get_url("https://x.org", as_visited=False) == "http://x.org/a"
    store.store_rules("https://x.org", RobotFileParser())
    assert store.get_known_domains() == ["http://x.org"]
    assert store.get_rules("http://x.org") is not None
    assert store.get_url("https://x.org") == "http://x.org/a"
    # an unknown domain gets rules under the requested key
    store.store_rules("https://new.org", None)
    assert "https://new.org" in store.urldict


def test_merge_hot_source_twin():
    "A hot http twin merges into its https twin in compressed mode."
    store = UrlStore(compressed=True)
    store.add_urls(["https://h.org/b"])
    entry = store.urldict.pop("https://h.org")
    store.add_urls(["http://h.org/a"])  # http entry is now hot
    store.urldict["https://h.org"] = entry
    store.add_urls(["https://h.org/c"])
    assert list(store.urldict) == ["https://h.org"]
    assert sorted(store.find_known_urls("https://h.org")) == [
        "https://h.org/a",
        "https://h.org/b",
        "https://h.org/c",
    ]


def test_twin_merge_on_download():
    "Coexisting legacy twins are merged on first write, no crash while iterating."
    store = UrlStore()
    store.add_urls(["http://dup.com/http-only", "http://dup.com/shared"])
    # simulate a legacy pickle with coexisting twin entries
    entry = store.urldict.pop("http://dup.com")
    store.add_urls(["https://dup.com/https-only", "https://dup.com/shared"])
    store.urldict["http://dup.com"] = entry

    downloaded = []
    while url := store.get_download_urls(time_limit=0):
        downloaded.extend(url)
    # twin deleted during iteration without RuntimeError, URLs unioned and deduped
    assert "http://dup.com" not in store.urldict
    assert sorted(downloaded) == [
        "https://dup.com/http-only",
        "https://dup.com/https-only",
        "https://dup.com/shared",
    ]
    assert store.urldict["https://dup.com"].total == 3


def test_twin_merge_metadata():
    "Merging twins in compressed mode sums counts, keeps rules and newest timestamp."
    store = UrlStore(compressed=True)
    store.add_urls(["http://m.com/a"])
    store.get_url("http://m.com")
    entry = store.urldict.pop("http://m.com")
    store.add_urls(["https://m.com/b"])
    store.urldict["https://m.com"].count = 1  # no timestamp on this side
    store.store_rules("https://m.com", RobotFileParser())
    store.urldict["http://m.com"] = entry
    store.add_urls(["https://m.com/c"])  # triggers the merge
    merged = store.urldict["https://m.com"]
    assert "http://m.com" not in store.urldict
    assert merged.count == 2 and merged.total == 3
    assert merged.timestamp == entry.timestamp
    assert store.get_rules("https://m.com") is not None


def test_merge_copies_src_rules():
    "Twin merge copies rules from src when tgt has none."
    s = UrlStore()
    rules = RobotFileParser()
    s.add_urls(["http://r.com/a"])
    s.store_rules("http://r.com", rules)
    entry = s.urldict.pop("http://r.com")
    s.add_urls(["https://r.com/b"])
    s.urldict["http://r.com"] = entry
    s.add_urls(["https://r.com/c"])
    assert "http://r.com" not in s.urldict
    assert s.get_rules("https://r.com") is rules


def test_twin_discard():
    "A discarded domain voids its http/https twin and rejects later additions."
    store = UrlStore()
    store.add_urls(["http://y.com/a"])
    store.discard(["https://y.com"])
    assert store.dump_urls() == []
    store.add_urls(["http://y.com/b"])
    assert store.dump_urls() == []
    assert "http://y.com" not in store.urldict
    # a resurrected twin next to the BUSTED entry is voided on merge
    store.urldict["http://y.com"] = DomainEntry()
    store.add_urls(["http://y.com/c"])
    assert "http://y.com" not in store.urldict
    assert store.dump_urls() == []


def test_busted_twin_stays_busted_in_schedule():
    "A BUSTED twin voids the domain for good, the schedule does not reopen it."
    store = UrlStore()
    store.add_urls(["https://a.org/1"])
    store.urldict["http://a.org"] = DomainEntry(state=State.BUSTED)
    assert store.establish_download_schedule() == []
    assert store.urldict["https://a.org"].state is State.BUSTED
    store.add_urls(["https://a.org/2"])
    assert store.dump_urls() == []


@pytest.mark.parametrize("compressed", [False, True])
def test_discard_releases_hot_domain(compressed):
    "Discarding the last written domain voids it in place and drops its cache."
    store = UrlStore(compressed=compressed)
    store.add_urls(["https://a.org/1"])
    entry = store.urldict["https://a.org"]
    store.discard(["https://a.org"])
    assert store._hot is None
    assert store.urldict["https://a.org"] is entry
    assert entry.state is State.BUSTED and entry.total == 0
    # a merge into a discarded legacy twin releases the hot source entry
    store = UrlStore(compressed=compressed)
    store.add_urls(["http://a.org/x"])
    store.urldict["https://a.org"] = DomainEntry(State.BUSTED)
    assert store.get_url("http://a.org") is None
    assert store._hot is None


def test_draws_move_the_cursor():
    "Draws leave the cursor past the last drawn URL, visited ones are skipped."
    store = UrlStore()
    store.add_urls([f"https://a.org/{i}" for i in range(4)])
    store.add_urls(["https://a.org/2"], visited=True)
    assert store.get_url("https://a.org") == "https://a.org/0"
    entry = store.urldict["https://a.org"]
    assert entry.cursor == 1
    schedule = store.establish_download_schedule(time_limit=0)
    assert [url for _, url in schedule] == ["https://a.org/1", "https://a.org/3"]
    assert entry.cursor == 4 and entry.state is State.ALL_VISITED


def test_compressed_cold_read_keeps_hot_domain():
    "Reading another domain in compressed mode leaves the hot domain in place."
    store = UrlStore(compressed=True)
    store.add_urls(["https://b.org/1"])
    store.add_urls(["https://a.org/1"])
    assert store.is_known("https://b.org/1")
    assert store._hot is store.urldict["https://a.org"]
    assert isinstance(store.urldict["https://b.org"].tuples, bytes)


def test_reentrant_lock():
    "Locked methods calling other locked methods do not deadlock."
    store = UrlStore()
    store.add_urls(["https://a.org/1", "https://b.org/1"])
    results: list[object] = []

    def run():
        results.append(sorted(store.get_download_urls(time_limit=0)))
        results.append(store.establish_download_schedule(time_limit=0))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert results == [["https://a.org/1", "https://b.org/1"], []]


@pytest.mark.parametrize("compressed", [False, True])
def test_concurrent_calls(compressed):
    "Concurrent readers and writers neither crash nor store duplicates."
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        store = UrlStore(compressed=compressed)
        store.add_urls([f"https://a.org/{i}" for i in range(5000)])
        store.add_urls(["https://b.org/x"])
        errors = []

        def write():
            for i in range(1000):
                store.add_urls([f"https://a.org/n{i}"])

        def read_domain():
            for _ in range(50):
                store.find_known_urls("https://a.org")

        def read_other():
            for _ in range(1000):
                store.is_known("https://b.org/x")

        def guarded(func):
            def target():
                try:
                    func()
                except Exception as err:
                    errors.append(err)

            return threading.Thread(target=target)

        threads = [guarded(f) for f in (write, write, read_domain, read_other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(interval)
    assert errors == []
    urls = store.find_known_urls("https://a.org")
    assert len(urls) == len(set(urls)) == 6000


def test_done_lifecycle():
    "done starts False, flips True when exhausted, reverts on new URLs."
    s = UrlStore()
    assert s.done is False
    s.add_urls(["http://a.com/1"])
    assert s.done is False
    assert s.get_url("http://a.com") is not None
    assert s.get_url("http://a.com") is None
    assert s.done is True
    s.add_urls(["http://a.com/2"])
    assert s.done is False
    # done stays False while any domain is still OPEN
    s2 = UrlStore()
    s2.add_urls(["http://a.com/1", "http://b.com/1"])
    s2.get_url("http://a.com")
    s2.get_url("http://a.com")
    assert s2.done is False
    # get_download_urls skips the exhausted domain
    assert s2.get_download_urls(time_limit=0) == ["http://b.com/1"]
    s2.get_url("http://b.com")
    assert s2.done is True


def test_filter_unknown_same_domain():
    "filter_unknown_urls works correctly for many same-domain URLs."
    s = UrlStore()
    s.add_urls([f"http://z.com/{i:04d}" for i in range(50)])
    assert s.filter_unknown_urls([f"http://z.com/{i:04d}" for i in range(50)]) == []


def test_store_key_forms():
    "Readers and writers accept any form of a domain and use one normalized key."
    store = UrlStore()
    store.add_urls(["https://Example.org/a", "https://u:p@example.org:443/b"])
    assert list(store.urldict) == ["https://example.org"]
    for domain in ("https://Example.org", "https://example.org/", "http://example.org"):
        assert store.find_known_urls(domain) == [
            "https://example.org/a",
            "https://example.org/b",
        ]
        assert not store.is_exhausted_domain(domain)
    assert store.find_known_urls("example.org") == []
    # punycode, unicode and IDNA2008 deviation forms
    store.add_urls(["https://xn--mnchen-3ya.de/a", "https://MÜNCHEN.de/b"])
    store.add_urls(["https://faß.de/a", "https://xn--fa-hia.de/b"])
    assert list(store.urldict)[1:] == ["https://münchen.de", "https://faß.de"]
    assert len(store.find_unvisited_urls("https://xn--fa-hia.de")) == 2
    # IPv6 and port forms
    store.add_urls(["https://[2001:db8:0:0::1]/a", "https://[2001:db8::1]:0443/b"])
    store.add_urls(
        ["https://ex.org:08080/a", "https://ex.org:8080/b", "https://ex.org:/c"]
    )
    assert list(store.urldict)[3:] == [
        "https://[2001:db8::1]",
        "https://ex.org:8080",
        "https://ex.org",
    ]
    # twins: lookups find the merged entry under either scheme
    twins = UrlStore()
    twins.add_urls(["http://example.org/a"])
    twins.add_urls(["https://example.org/b"])
    assert twins.is_known("http://example.org/a")
    assert twins.get_url("http://example.org") == "https://example.org/a"
    assert twins.has_been_visited("http://example.org/a")
    assert twins.find_unvisited_urls("http://example.org") == ["https://example.org/b"]


def test_store_readers_normalize():
    "Readers apply the normalization of add_urls, malformed URLs are unknown."
    inputs = [
        "https://a.org/x?utm_source=1",
        "https://a.org//c",
        "https://a.org/e#utm_source=x",
        "https://a.org/f/",
    ]
    store = UrlStore()
    store.add_urls(inputs)
    assert [store.is_known(u) for u in inputs] == [True] * 4
    assert store.is_known("https://a.org/f")
    assert not store.has_been_visited("https://a.org/x?utm_source=2")
    malformed = ["https://a.org/x", "http://[bad", "https://a.org/new"]
    assert store.filter_unknown_urls(malformed) == malformed[1:]
    assert store.filter_unvisited_urls(malformed) == malformed
    assert not store.is_known("http://[bad")
    assert store.filter_unknown_urls(["https://b.org/x"] * 2) == ["https://b.org/x"]
    # raw domain forms, as passed by trafilatura
    store.store_rules("https://a.org", RobotFileParser())
    assert store.get_rules("https://A.org/") is not None
    assert store.get_url("https://A.org/", as_visited=False) == "https://a.org/x"
    # URLs whose query does not pass the language filter are not stored
    lang_store = UrlStore(language="de", strict=True)
    lang_store.add_urls(["https://a.org/x?lang=en"])
    assert not lang_store.urldict


def test_store_no_phantom_entries():
    "Reads and empty draws create no entry and keep a discarded domain discarded."
    store = UrlStore()
    assert store.get_url("https://unknown.org") is None
    assert not store.get_rules("https://unknown.org")
    assert not store.urldict
    # rules alone leave nothing to visit
    store.store_rules("https://Rules.org/", RobotFileParser())
    assert list(store.urldict) == ["https://rules.org"]
    assert store.unvisited_websites_number() == 0
    store.add_urls(["https://rules.org/a"])
    assert store.get_rules("https://rules.org") is not None
    assert store.get_url("https://rules.org") == "https://rules.org/a"
    # a discarded domain is not reopened
    store.add_urls(["https://a.org/x"])
    store.discard(["https://a.org/"])
    assert store.get_url("https://a.org") is None
    assert store.urldict["https://a.org"].state is State.BUSTED
    store.add_urls(["https://a.org/y"])
    assert store.dump_urls() == ["https://rules.org/a"]


def test_store_visited_upgrade():
    "A visited input marks a stored URL as visited, never the reverse."
    for compressed in (False, True):
        store = UrlStore(compressed=compressed)
        store.add_urls(["https://a.org/x", "https://a.org/y"])
        store.add_urls(["https://a.org/x/"], visited=True)
        assert store.has_been_visited("https://a.org/x")
        assert store.get_url("https://a.org") == "https://a.org/y"
        assert store.is_exhausted_domain("https://A.org/")
        store.add_urls(["https://a.org/x"])
        assert store.has_been_visited("https://a.org/x")


def test_store_appendleft_order():
    "Priority URLs keep their input order at the front of the queue."
    store = UrlStore()
    store.add_urls(["https://a.org/p1"])
    store.add_urls(
        appendleft=["https://a.org/n1", "https://a.org/n2", "https://a.org/p1"]
    )
    assert store.find_unvisited_urls("https://a.org") == [
        "https://a.org/n1",
        "https://a.org/n2",
        "https://a.org/p1",
    ]
    assert [store.get_url("https://a.org") for _ in range(3)] == [
        "https://a.org/n1",
        "https://a.org/n2",
        "https://a.org/p1",
    ]
    # priority URLs added after draws are served first
    for compressed in (False, True):
        store = UrlStore(compressed=compressed)
        store.add_urls(["https://a.org/x", "https://a.org/y"])
        assert store.get_url("https://a.org") == "https://a.org/x"
        store.add_urls(appendleft=["https://a.org/p"])
        assert [store.get_url("https://a.org") for _ in range(3)] == [
            "https://a.org/p",
            "https://a.org/y",
            None,
        ]


def test_store_merge_total():
    "Merging legacy twins updates the URL total."
    store = UrlStore()
    store.add_urls(["http://example.org/a"])
    store.urldict["https://example.org"] = DomainEntry()
    store.store_rules("http://example.org", None)
    assert list(store.urldict) == ["https://example.org"]
    assert store.total_url_number() == 1


class _CountingCompressor(Compressor):
    "Count the compression calls."

    __slots__ = ("calls",)

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def compress(self, data):
        self.calls += 1
        return super().compress(data)


def test_compressed_store_writes(monkeypatch):
    "A compressed domain is compressed again only when another domain is written."
    counter = _CountingCompressor()
    monkeypatch.setattr("courlan.urlstore.COMPRESSOR", counter)
    store = UrlStore(compressed=True)
    for i in range(50):
        store.add_urls([f"https://a.org/{i}/{j}" for j in range(20)])
    assert [store.get_url("https://a.org") for _ in range(2)] == [
        "https://a.org/0/0",
        "https://a.org/0/1",
    ]
    assert counter.calls == 0
    store.add_urls(["https://b.org/x"])
    assert counter.calls == 1
    assert isinstance(store.urldict["https://a.org"].tuples, bytes)
    assert len(store.find_unvisited_urls("https://a.org")) == 998
    # the pickle holds compressed domains only
    restored = pickle.loads(pickle.dumps(store))
    assert all(isinstance(v.tuples, bytes) for v in restored.urldict.values())
    assert restored.get_url("https://a.org") == "https://a.org/0/2"


def test_store_legacy_data():
    "Stores and entries pickled by earlier versions load and work."
    store = UrlStore(compressed=True)
    entry = DomainEntry()
    entry.tuples = bz2.compress(
        pickle.dumps(deque([UrlPathTuple("/a", False)]), protocol=5)
    )
    entry.total = 1
    del entry.cursor
    store.urldict["https://old.org"] = pickle.loads(pickle.dumps(entry))
    assert store.get_url("https://old.org") == "https://old.org/a"
    assert store.get_url("https://old.org") is None
    state = store.__getstate__()
    del state["_offset"]
    state["urldict"] = defaultdict(DomainEntry, state["urldict"])
    legacy = UrlStore.__new__(UrlStore)
    legacy.__setstate__(state)
    assert type(legacy.urldict) is dict
    assert legacy.get_url("https://new.org") is None
    assert list(legacy.urldict) == ["https://old.org"]
    assert legacy.get_download_urls() == []


def test_download_urls_rotation():
    "get_download_urls starts after the last domain served."
    store = UrlStore()
    store.add_urls([f"https://d{i}.org/{j}" for i in range(4) for j in range(2)])
    first = store.get_download_urls(time_limit=-1, max_urls=2)
    second = store.get_download_urls(time_limit=-1, max_urls=2)
    assert first == ["https://d0.org/0", "https://d1.org/0"]
    assert second == ["https://d2.org/0", "https://d3.org/0"]


def test_schedule_stops_at_quota():
    "establish_download_schedule leaves the domains past the quota untouched."
    store = UrlStore()
    store.add_urls([f"https://d{i}.org/x" for i in range(5)])
    assert len(store.establish_download_schedule(max_urls=2)) == 2
    assert [v.timestamp is None for v in store.urldict.values()] == [
        False,
        False,
        True,
        True,
        True,
    ]


def test_download_urls_rotation_partial():
    "A call ending below max_urls still moves the start past the last domain served."
    store = UrlStore()
    store.add_urls([f"https://d{i}.org/{j}" for i in range(3) for j in range(2)])
    assert store.get_url("https://d2.org") == "https://d2.org/0"
    # d2 was just served, the time limit skips it
    assert store.get_download_urls(time_limit=10) == [
        "https://d0.org/0",
        "https://d1.org/0",
    ]
    assert store.get_download_urls(time_limit=-1, max_urls=1) == ["https://d2.org/1"]


def test_schedule_rotation():
    "establish_download_schedule starts after the last domain served."
    store = UrlStore()
    store.add_urls([f"https://d{i}.org/{j}" for i in range(4) for j in range(3)])
    served = [
        sorted(urlsplit(url).netloc for _, url in store.establish_download_schedule(2))
        for _ in range(3)
    ]
    assert served == [["d0.org", "d1.org"], ["d2.org", "d3.org"], ["d0.org", "d1.org"]]


def test_draw_interleaved_visited():
    "Draws skip URLs added as visited, in order, with the cursor on the next one."
    store = UrlStore()
    for i in range(6):
        store.add_urls([f"https://a.org/{i}"], visited=i % 2 == 0)
    assert [store.get_url("https://a.org") for _ in range(4)] == [
        "https://a.org/1",
        "https://a.org/3",
        "https://a.org/5",
        None,
    ]
    store.add_urls([f"https://b.org/{i}" for i in range(4)], visited=False)
    store.add_urls(["https://b.org/0", "https://b.org/1"], visited=True)
    schedule = store.establish_download_schedule(max_urls=10, time_limit=10)
    assert [url for _, url in schedule] == ["https://b.org/2", "https://b.org/3"]
    assert store.urldict["https://b.org"].cursor == 4


def test_is_known_domain():
    "is_known_domain normalizes its input like the store keys."
    store = UrlStore()
    store.add_urls(["https://user@Example.org:443/a"])
    assert store.urldict.keys() == {"https://example.org"}
    for form in (
        "https://user@example.org",
        "https://EXAMPLE.org/",
        "http://example.org",
        "https://example.org:443/other",
    ):
        assert store.is_known_domain(form), form
    assert not store.is_known_domain("https://example.com")
    assert not store.is_known_domain("https://example.org:8080")


def _lock_taken_elsewhere(store: UrlStore) -> bool:
    "Tell if another thread fails to take the store lock."
    taken: list[bool] = []

    def probe() -> None:
        if store._lock.acquire(timeout=0.5):
            store._lock.release()
            taken.append(True)

    thread = threading.Thread(target=probe)
    thread.start()
    thread.join()
    return not taken


def test_normalization_outside_lock(monkeypatch):
    "add_urls and the filters normalize the URLs before taking the store lock."
    store = UrlStore()
    held: list[bool] = []
    split = UrlStore._split

    def probed(self: UrlStore, url: str) -> tuple[str, str, str] | None:
        held.append(_lock_taken_elsewhere(self))
        return split(self, url)

    monkeypatch.setattr(UrlStore, "_split", probed)
    store.add_urls(["https://a.org/1"], appendleft=["https://a.org/0"])
    store.filter_unknown_urls(["https://a.org/2"])
    store.filter_unvisited_urls(["https://a.org/3"])
    assert held == [False] * 4
    assert store.find_known_urls("https://a.org") == [
        "https://a.org/0",
        "https://a.org/1",
    ]


def test_print_outside_lock(monkeypatch, capsys):
    "Printing happens after the store lock is released."
    store = UrlStore()
    store.add_urls(["https://a.org/1", "https://b.org/1"])
    held: list[bool] = []
    real_print = print

    def probed(*args: object, **kwargs: object) -> None:
        held.append(_lock_taken_elsewhere(store))
        real_print(*args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr("builtins.print", probed)
    store.print_unvisited_urls()
    store.print_urls()
    UrlStore().print_unvisited_urls()
    UrlStore().print_urls()
    assert held == [False, False]
    assert capsys.readouterr().out == (
        "https://a.org/1\nhttps://b.org/1\n"
        "https://a.org/1\tFalse\nhttps://b.org/1\tFalse\n"
    )


def test_draw_past_long_visited_run():
    "Draws find the first unvisited URL after a run longer than the index scan."
    store = UrlStore()
    store.add_urls([f"https://a.org/{i}" for i in range(200)])
    # first unvisited URL right after the index scan
    store.add_urls([f"https://a.org/{i}" for i in range(_NEAR)], visited=True)
    assert store.urldict["https://a.org"].cursor == _NEAR
    store.urldict["https://a.org"].cursor = 0
    store.add_urls([f"https://a.org/{i}" for i in range(150)], visited=True)
    assert store.urldict["https://a.org"].cursor == 150
    store.urldict["https://a.org"].cursor = 0
    assert store.get_url("https://a.org") == "https://a.org/150"
    store.add_urls([f"https://a.org/{i}" for i in range(200)], visited=True)
    assert store.get_url("https://a.org") is None


# --- lease mode ---------------------------------------------------------


@pytest.fixture
def frozen_clock(monkeypatch):
    "Pin the store clock to a fixed instant and return its mutable holder."
    holder = {"now": datetime(2026, 1, 1, 12, 0, 0)}
    monkeypatch.setattr(urlstore_module, "_now", lambda: holder["now"])
    return holder


def test_lease_mode_defaults_off():
    "The store stays on the classic consumption API unless lease_mode is set."
    store = UrlStore()
    assert store._lease_mode is False and store._leases == {} and store._leased == set()
    for method in ("claim_url", "claim_download_urls", "confirm_url", "release_url"):
        with pytest.raises(RuntimeError):
            getattr(store, method)("token")
    # classic API still works
    store.add_urls(["https://a.org/1"])
    assert store.get_url("https://a.org") == "https://a.org/1"
    assert store.get_download_urls() == []
    assert store.establish_download_schedule() == []


def test_lease_mode_rejects_consumption_api():
    "Old consumption-style entries refuse to run instead of silently visiting."
    store = UrlStore(lease_mode=True)
    store.add_urls(["https://a.org/1"])
    with pytest.raises(RuntimeError):
        store.get_url("https://a.org")
    with pytest.raises(RuntimeError):
        store.get_download_urls()
    with pytest.raises(RuntimeError):
        store.establish_download_schedule()
    # nothing was consumed
    assert store.find_unvisited_urls("https://a.org") == ["https://a.org/1"]


def test_claim_confirm_basic(frozen_clock):
    "A claim returns URL and token, only the confirmation marks it visited."
    store = UrlStore(lease_mode=True)
    store.add_urls(["https://a.org/1", "https://a.org/2"])
    claim = store.claim_url("https://a.org")
    assert claim is not None
    url, token = claim
    assert url == "https://a.org/1" and isinstance(token, str) and token
    entry = store.urldict["https://a.org"]
    assert entry.count == 1 and entry.timestamp == frozen_clock["now"]
    # in-flight URLs still belong to the unvisited set
    assert store.find_unvisited_urls("https://a.org") == [
        "https://a.org/1",
        "https://a.org/2",
    ]
    assert store.has_been_visited(url) is False
    assert ("https://a.org", "/1") in store._leased
    assert store.confirm_url(token) is True
    assert store.has_been_visited(url)
    assert ("https://a.org", "/1") not in store._leased
    assert store.find_unvisited_urls("https://a.org") == ["https://a.org/2"]


def test_invalid_and_duplicate_tokens_rejected(frozen_clock):
    "Unknown, duplicate and expired tokens are rejected without state change."
    store = UrlStore(lease_mode=True)
    store.add_urls(["https://a.org/1", "https://a.org/2"])
    url, token = store.claim_url("https://a.org")
    # unknown token
    assert store.confirm_url("deadbeef") is False
    assert store.release_url("deadbeef") is False
    assert ("https://a.org", "/1") in store._leased
    # a second confirmation with the same token is refused
    assert store.confirm_url(token) is True
    assert store.confirm_url(token) is False
    assert store.release_url(token) is False
    assert store.has_been_visited(url)
    # released tokens cannot be reused either
    _, token2 = store.claim_url("https://a.org")
    assert store.release_url(token2) is True
    assert store.confirm_url(token2) is False
    assert store.release_url(token2) is False


def test_release_allows_reclaim(frozen_clock):
    "Releasing a claim puts the URL back, unvisited, into the claimable set."
    store = UrlStore(lease_mode=True)
    store.add_urls(["https://a.org/only"])
    _, token = store.claim_url("https://a.org")
    assert store.claim_url("https://a.org") is None  # leased out, no other URL
    assert store.release_url(token) is True
    assert store.find_unvisited_urls("https://a.org") == ["https://a.org/only"]
    claim = store.claim_url("https://a.org")
    assert claim is not None and claim[0] == "https://a.org/only"
    assert claim[1] != token  # a fresh lease, the old token stays invalid


def test_failed_fetch_reclaim_cycle(frozen_clock):
    "End-to-end failure handling: claim, failed fetch, release, claim, confirm."
    store = UrlStore(lease_mode=True)
    store.add_urls(["https://a.org/flaky"])
    first = store.claim_url("https://a.org")
    assert first[0] == "https://a.org/flaky"
    # the fetch fails: the worker gives the claim back
    assert store.release_url(first[1]) is True
    assert not store.has_been_visited("https://a.org/flaky")
    # another worker can pick the very same URL
    second = store.claim_url("https://a.org")
    assert second[0] == "https://a.org/flaky" and second[1] != first[1]
    assert store.confirm_url(second[1]) is True
    assert store.has_been_visited("https://a.org/flaky")
    assert store.claim_url("https://a.org") is None


def test_lease_expiration(frozen_clock):
    "Expired leases free their URLs and their tokens are refused."
    store = UrlStore(lease_mode=True)
    store.add_urls(["https://a.org/1", "https://a.org/2"])
    first = store.claim_url("https://a.org", lease_duration=30)
    second = store.claim_url("https://a.org", lease_duration=30)
    assert [c[0] for c in (first, second)] == [
        "https://a.org/1",
        "https://a.org/2",
    ]
    assert store.claim_url("https://a.org") is None
    # exactly at the expiration instant the lease is over
    frozen_clock["now"] += timedelta(seconds=30)
    reclaimed = store.claim_url("https://a.org", lease_duration=30)
    assert reclaimed is not None and reclaimed[0] == "https://a.org/1"
    # the stale token is rejected and changes nothing
    assert store.confirm_url(first[1]) is False
    assert store.release_url(first[1]) is False
    assert ("https://a.org", "/1") in store._leased  # held by the new lease
    # a late confirmation after expiry is impossible even if no one reclaimed
    frozen_clock["now"] += timedelta(seconds=60)
    assert store.confirm_url(second[1]) is False
    assert store.has_been_visited("https://a.org/2") is False


def test_late_confirmation_does_not_resurrect(frozen_clock):
    "A confirmation arriving after another lease took over cannot double-write."
    store = UrlStore(lease_mode=True)
    store.add_urls(["https://a.org/x"])
    _, stale = store.claim_url("https://a.org", lease_duration=10)
    frozen_clock["now"] += timedelta(seconds=11)
    current = store.claim_url("https://a.org")
    assert current[0] == "https://a.org/x"
    assert store.confirm_url(stale) is False
    assert store.confirm_url(current[1]) is True
    assert len(store._leases) == 0 and store._leased == set()


def test_inflight_keeps_store_open(frozen_clock):
    "done cannot flip True while unconfirmed URLs are still in flight."
    store = UrlStore(lease_mode=True)
    store.add_urls(["https://a.org/1"])
    assert store.done is False
    claim = store.claim_url("https://a.org")
    assert claim is not None and store.done is False
    assert store.get_unvisited_domains() == ["https://a.org"]
    assert store.confirm_url(claim[1]) is True
    assert store.done is True
    assert store.get_unvisited_domains() == []
    # a released URL reopens the finished store
    store.add_urls(["https://b.org/1"])
    pending = store.claim_url("https://b.org")
    assert store.done is False
    assert store.release_url(pending[1]) is True
    assert store.get_unvisited_domains() == ["https://b.org"]


def test_claim_interval_and_rotation(frozen_clock):
    "Bulk claims keep the host rotation and measure the interval from the claim."
    store = UrlStore(lease_mode=True)
    store.add_urls([f"https://d{i}.org/{j}" for i in range(3) for j in range(2)])
    first = store.claim_download_urls(time_limit=10, max_urls=2)
    assert [url for url, _ in first] == ["https://d0.org/0", "https://d1.org/0"]
    # d0/d1 were just served, d2 is free; rotation continues after d1
    frozen_clock["now"] += timedelta(seconds=1)
    second = store.claim_download_urls(time_limit=10, max_urls=2)
    assert [url for url, _ in second] == ["https://d2.org/0"]
    # still within the interval: nothing new
    assert store.claim_download_urls(time_limit=10) == []
    # strictly past the interval the hosts rotate back to d0 and d1
    frozen_clock["now"] += timedelta(seconds=10)
    third = store.claim_download_urls(time_limit=10, max_urls=10)
    assert [url for url, _ in third] == ["https://d0.org/1", "https://d1.org/1"]
    # d2 was served one second later, so it waits one more second
    assert store.claim_download_urls(time_limit=10) == []
    frozen_clock["now"] += timedelta(seconds=1)
    fourth = store.claim_download_urls(time_limit=10, max_urls=10)
    assert [url for url, _ in fourth] == ["https://d2.org/1"]
    # every URL carries a distinct valid token and stays unvisited
    tokens = [token for _, token in first + second + third + fourth]
    assert len(tokens) == len(set(tokens)) == 6
    assert len(store._leased) == 6
    assert store.total_url_number() == 6
    assert all(
        not u.visited
        for key in store.urldict
        for u in store._load_urls(key)
    )


def test_leased_urls_skipped_by_bulk_claims(frozen_clock):
    "Hosts whose URLs are all leased out yield nothing until a lease ends."
    store = UrlStore(lease_mode=True)
    store.add_urls(["https://a.org/1", "https://b.org/1"])
    store.claim_url("https://a.org", lease_duration=5)
    frozen_clock["now"] += timedelta(seconds=1)
    claims = store.claim_download_urls(time_limit=0)
    assert [url for url, _ in claims] == ["https://b.org/1"]
    # once the a.org lease expires it is served again
    frozen_clock["now"] += timedelta(seconds=5)
    claims = store.claim_download_urls(time_limit=0)
    assert [url for url, _ in claims] == ["https://a.org/1"]


def test_concurrent_claims_single_lease(frozen_clock):
    "Threads racing for one host produce one valid lease per URL, no duplicates."
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        store = UrlStore(lease_mode=True)
        store.add_urls([f"https://a.org/{i}" for i in range(200)])
        claims: list[tuple[str, str]] = []
        errors = []

        def worker() -> None:
            try:
                for _ in range(200):
                    claim = store.claim_url("https://a.org", lease_duration=1000)
                    if claim is not None:
                        claims.append(claim)
            except Exception as err:  # pragma: no cover
                errors.append(err)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(interval)
    assert errors == []
    # exactly one lease per URL, every token unique
    assert len(claims) == 200
    assert len({url for url, _ in claims}) == 200
    assert len({token for _, token in claims}) == 200
    assert set(store._leased) == {
        ("https://a.org", f"/{i}") for i in range(200)
    }
    # concurrent confirmations of the same token: exactly one succeeds
    token = claims[0][1]
    outcomes = []

    def confirm() -> None:
        outcomes.append(store.confirm_url(token))

    runners = [threading.Thread(target=confirm) for _ in range(8)]
    for thread in runners:
        thread.start()
    for thread in runners:
        thread.join()
    assert outcomes.count(True) == 1 and outcomes.count(False) == 7


def test_lease_parity_compressed(frozen_clock):
    "Compressed and plain stores behave identically across a full lease cycle."
    sequences = []
    for compressed in (False, True):
        store = UrlStore(compressed=compressed, lease_mode=True)
        store.add_urls(["https://a.org/1", "https://a.org/2", "https://b.org/x"])
        seen = []
        first = store.claim_download_urls(time_limit=0, lease_duration=30)
        seen.append(sorted(url for url, _ in first))
        frozen_clock["now"] += timedelta(seconds=1)
        second = store.claim_download_urls(time_limit=0, lease_duration=30)
        seen.append(sorted(url for url, _ in second))
        # confirm one, release one, let one expire
        assert store.confirm_url(first[0][1]) is True
        assert store.release_url(second[0][1]) is True
        frozen_clock["now"] += timedelta(seconds=40)
        third = store.claim_download_urls(time_limit=0)
        seen.append(sorted(url for url, _ in third))
        for _url, token in third:
            assert store.confirm_url(token) is True
        seen.append(store.done)
        seen.append(sorted(store.dump_urls()))
        sequences.append(seen)
    assert sequences[0] == sequences[1]


def test_twin_merge_keeps_lease_valid(frozen_clock):
    "HTTP/HTTPS canonicalization preserves outstanding leases and their tokens."
    store = UrlStore(lease_mode=True)
    store.add_urls(["http://h.org/a", "http://h.org/b"])
    claim = store.claim_url("http://h.org", lease_duration=100)
    assert claim[0] == "http://h.org/a"
    # introducing the https twin merges the entries under https
    store.add_urls(["https://h.org/c"])
    assert "http://h.org" not in store.urldict
    assert ("https://h.org", "/a") in store._leased
    lease = store._leases[claim[1]]
    assert lease.domain == "https://h.org" and lease.path == "/a"
    # the token issued before the merge still confirms the URL
    assert store.confirm_url(claim[1]) is True
    assert store.has_been_visited("http://h.org/a")
    nxt = store.claim_url("https://h.org")
    assert nxt[0] == "https://h.org/b"
    assert store.confirm_url(nxt[1]) is True
    # a claim on the merged-away twin key resolves to the surviving entry
    assert store.claim_url("http://h.org")[0] == "https://h.org/c"


def test_twin_merge_dedupes_shared_path(frozen_clock):
    "Coexisting twins holding the same path keep a single valid lease."
    store = UrlStore(lease_mode=True)
    store.add_urls(["http://dup.com/shared", "http://dup.com/http-only"])
    http_claim = store.claim_url("http://dup.com", lease_duration=100)
    assert http_claim[0] == "http://dup.com/shared"
    # build a legacy-style coexisting https twin
    entry = store.urldict.pop("http://dup.com")
    store.add_urls(["https://dup.com/shared", "https://dup.com/https-only"])
    store.urldict["http://dup.com"] = entry
    https_claim = store.claim_url("https://dup.com", lease_duration=100)
    assert https_claim[0] == "https://dup.com/https-only"
    # the next addition triggers the twin merge
    store.add_urls(["https://dup.com/more"])
    assert "http://dup.com" not in store.urldict
    # the pre-merge token still confirms the shared URL exactly once
    assert store.confirm_url(http_claim[1]) is True
    assert store.confirm_url(http_claim[1]) is False
    assert store.confirm_url(https_claim[1]) is True


def test_discard_invalidates_leases(frozen_clock):
    "Discarding a host (or its twin) voids every related token."
    store = UrlStore(lease_mode=True)
    store.add_urls(["http://y.org/a", "https://y.org/b", "https://z.org/1"])
    y_claim = store.claim_url("http://y.org", lease_duration=100)
    z_claim = store.claim_url("https://z.org", lease_duration=100)
    store.discard(["https://y.org"])
    assert store.confirm_url(y_claim[1]) is False
    assert store.release_url(y_claim[1]) is False
    assert not any(lease.domain == "https://y.org" for lease in store._leases.values())
    # the other host is untouched
    assert store.confirm_url(z_claim[1]) is True
    # discarded hosts stay discarded and produce no claims
    assert store.claim_url("http://y.org") is None


def test_reset_invalidates_leases(frozen_clock):
    "A reset drops every lease and all previously issued tokens."
    store = UrlStore(lease_mode=True)
    store.add_urls(["https://a.org/1", "https://b.org/1"])
    claims = store.claim_download_urls(time_limit=0)
    tokens = [token for _, token in claims]
    store.reset()
    assert store._leases == {} and store._leased == set()
    for token in tokens:
        assert store.confirm_url(token) is False
        assert store.release_url(token) is False


def test_lease_persistence_roundtrip(tmp_path, frozen_clock):
    "Saving and loading keeps confirmed progress, requeues pending URLs, kills tokens."
    for compressed in (False, True):
        store = UrlStore(compressed=compressed, lease_mode=True)
        store.add_urls([f"https://x.org/{i}" for i in range(4)])
        done = store.claim_url("https://x.org", lease_duration=100)
        assert store.confirm_url(done[1]) is True
        pending = store.claim_url("https://x.org", lease_duration=100)
        assert pending[0] == "https://x.org/1"
        path = str(tmp_path / f"store-{compressed}.pickle")
        store.write(path)
        loaded = load_store(path)
        assert loaded._lease_mode is True
        assert loaded._leases == {} and loaded._leased == set()
        # confirmed progress survives, in-flight URLs are unvisited again
        assert loaded.has_been_visited("https://x.org/0")
        assert sorted(loaded.find_unvisited_urls("https://x.org")) == [
            f"https://x.org/{i}" for i in range(1, 4)
        ]
        # every pre-load token is invalid
        assert loaded.confirm_url(pending[1]) is False
        assert loaded.release_url(pending[1]) is False
        # the requeued URL leads the next claims and can complete
        claim = loaded.claim_url("https://x.org")
        assert claim[0] == "https://x.org/1"
        assert loaded.confirm_url(claim[1]) is True


def test_legacy_pickle_loads_in_lease_mode_compatible(tmp_path):
    "Pickles written before lease mode load cleanly and keep the classic API."
    store = UrlStore(compressed=True)
    store.add_urls(["https://old.org/a"])
    state = store.__getstate__()
    assert "_leases" not in state and "_leased" not in state
    legacy = UrlStore.__new__(UrlStore)
    legacy.__setstate__(state)
    assert legacy._lease_mode is False
    assert legacy._leases == {} and legacy._leased == set()
    assert legacy.get_url("https://old.org") == "https://old.org/a"
    # enabling lease mode on a loaded store works from a clean slate
    legacy._lease_mode = True
    legacy.add_urls(["https://old.org/b"])
    claim = legacy.claim_url("https://old.org")
    assert claim[0] == "https://old.org/b"
    assert legacy.confirm_url(claim[1]) is True


def test_lease_objects_and_default_duration():
    "Lease value objects track their lifetime and the default duration is exposed."
    assert DEFAULT_LEASE_DURATION == 300.0
    start = datetime(2026, 1, 1)
    lease = Lease("tok", "https://a.org", "/x", start + timedelta(seconds=10))
    assert lease.is_expired(start) is False
    assert lease.is_expired(start + timedelta(seconds=10)) is True
    assert lease.is_expired(start + timedelta(seconds=11)) is True


def test_claim_collection_and_state_accounting(frozen_clock):
    "Claim sets and counters stay coherent across claims, confirms and releases."
    store = UrlStore(lease_mode=True)
    store.add_urls([f"https://a.org/{i}" for i in range(5)])
    claims = [store.claim_url("https://a.org", lease_duration=100) for _ in range(3)]
    assert all(claims)
    entry = store.urldict["https://a.org"]
    assert entry.count == 3
    assert len(store._leases) == len(store._leased) == 3
    assert store.get_all_counts() == [3]
    # releasing does not rewrite the download counter
    store.release_url(claims[0][1])
    assert entry.count == 3 and len(store._leases) == 2
    # the released URL is claimable again
    again = store.claim_url("https://a.org", lease_duration=100)
    assert again[0] == claims[0][0] and again[1] != claims[0][1]
    # drain everything: confirm the four live claims, reclaim the last URL
    for claim in claims[1:]:
        assert store.confirm_url(claim[1]) is True
    assert store.confirm_url(again[1]) is True
    last = store.claim_url("https://a.org")
    assert last[0] == "https://a.org/3"
    assert store.confirm_url(last[1]) is True
    tail = store.claim_url("https://a.org")
    assert tail[0] == "https://a.org/4"
    assert store.confirm_url(tail[1]) is True
    assert store.done is True
    assert store._leases == {} and store._leased == set()
    assert store.total_url_number() == 5


def test_claim_unknown_domain(frozen_clock):
    "Claiming an unknown or non-OPEN domain yields None without creating entries."
    store = UrlStore(lease_mode=True)
    assert store.claim_url("https://unknown.org") is None
    assert not store.urldict
    store.add_urls(["https://a.org/1"])
    claim = store.claim_url("https://a.org")
    assert claim is not None
    store.confirm_url(claim[1])
    # exhausted domain yields nothing
    assert store.claim_url("https://a.org") is None
    # and a bulk sweep over the same exhausted host stays empty
    assert store.claim_download_urls(time_limit=0) == []


def test_expired_token_cleanup_path(frozen_clock):
    "An expired token used in confirm/release is swept and reported invalid."
    store = UrlStore(lease_mode=True)
    store.add_urls(["https://a.org/1", "https://a.org/2"])
    stale = store.claim_url("https://a.org", lease_duration=5)
    # another live lease keeps the host busy; expiry only matters on token use
    frozen_clock["now"] += timedelta(seconds=6)
    assert store.confirm_url(stale[1]) is False
    assert stale[1] not in store._leases
    # same path through release_url
    other = store.claim_url("https://a.org", lease_duration=5)
    frozen_clock["now"] += timedelta(seconds=6)
    assert store.release_url(other[1]) is False
    assert other[1] not in store._leases


def test_confirm_after_discard_invalid(frozen_clock):
    "A confirmation against a voided domain is refused after removing its lease."
    store = UrlStore(lease_mode=True)
    store.add_urls(["https://a.org/1", "https://b.org/1"])
    a_claim = store.claim_url("https://a.org", lease_duration=100)
    b_claim = store.claim_url("https://b.org", lease_duration=100)
    store.discard(["https://a.org"])
    assert store.confirm_url(a_claim[1]) is False
    # the unaffected host still completes
    assert store.confirm_url(b_claim[1]) is True


def test_confirm_missing_path_after_dedup(frozen_clock):
    "A token whose path vanished from the store cannot confirm anything."
    store = UrlStore(lease_mode=True)
    store.add_urls(["https://a.org/1"])
    _, token = store.claim_url("https://a.org", lease_duration=100)
    lease = store._leases[token]
    # simulate the path no longer being present under the stored domain,
    # dropping the hot path index which otherwise still references it
    entry = store.urldict[lease.domain]
    store._freeze()
    entry.tuples = deque()
    entry.total = 0
    assert store.confirm_url(token) is False
    assert token not in store._leases  # the lease itself was consumed


def test_release_reopens_finished_domain(frozen_clock):
    "Releasing the last in-flight URL reopens an ALL_VISITED domain and clears done."
    store = UrlStore(lease_mode=True)
    store.add_urls(["https://a.org/1"])
    _, token = store.claim_url("https://a.org", lease_duration=100)
    entry = store.urldict["https://a.org"]
    entry.state = State.ALL_VISITED
    store.done = True
    assert store.release_url(token) is True
    assert entry.state is State.OPEN
    assert store.done is False
    assert store.get_unvisited_domains() == ["https://a.org"]


def test_legacy_pickle_missing_lease_slot():
    "A pickled state without lease slots restores lease mode off."
    store = UrlStore()
    store.add_urls(["https://old.org/a"])
    state = store.__getstate__()
    del state["_lease_mode"]
    restored = UrlStore.__new__(UrlStore)
    restored.__setstate__(state)
    assert restored._lease_mode is False
    assert restored._leases == {} and restored._leased == set()


def test_bulk_claim_skips_busted_and_none_entries(frozen_clock):
    "Bulk claims tolerate entries that disappear or are voided during the sweep."
    store = UrlStore(lease_mode=True)
    store.add_urls(["https://a.org/1", "https://b.org/1"])
    store.discard(["https://b.org"])
    # no exception, only the OPEN host can be served
    claims = store.claim_download_urls(time_limit=0)
    assert [url for url, _ in claims] == ["https://a.org/1"]


def test_rekey_lease_collision_drops_duplicate(frozen_clock):
    "Merging twins that both lease the same path keeps a single valid token."
    store = UrlStore(lease_mode=True)
    store.add_urls(["http://dup.com/shared", "http://dup.com/http-only"])
    # detach the http entry and build a coexisting https twin (legacy state)
    http_entry = store.urldict.pop("http://dup.com")
    store.add_urls(["https://dup.com/shared", "https://dup.com/https-only"])
    store.urldict["http://dup.com"] = http_entry
    # place a live lease on each twin for the same normalized path,
    # bypassing claim_url() which would merge the twins on entry
    now = frozen_clock["now"]
    http_token, https_token = "http-token", "https-token"
    store._leases[http_token] = Lease(
        http_token, "http://dup.com", "/shared", now + timedelta(seconds=100)
    )
    store._leases[https_token] = Lease(
        https_token, "https://dup.com", "/shared", now + timedelta(seconds=100)
    )
    store._leased.update(
        {("http://dup.com", "/shared"), ("https://dup.com", "/shared")}
    )
    # the next addition canonicalizes and merges the twins
    store.add_urls(["https://dup.com/more"])
    assert "http://dup.com" not in store.urldict
    # only one lease remains for the shared path and it is the https one
    assert store._leased == {("https://dup.com", "/shared")}
    assert https_token in store._leases
    assert http_token not in store._leases
    assert store.confirm_url(http_token) is False
    assert store.confirm_url(https_token) is True


def test_confirm_against_busted_entry_refused(frozen_clock):
    "A lease whose entry is voided out of band cannot confirm, and is dropped."
    store = UrlStore(lease_mode=True)
    store.add_urls(["https://a.org/1"])
    _, token = store.claim_url("https://a.org", lease_duration=100)
    # void the entry while keeping the lease on record (defensive state)
    store._bust(store.urldict["https://a.org"])
    assert store.confirm_url(token) is False
    assert token not in store._leases and store._leased == set()


def test_get_url_on_busted_domain_sets_done():
    "Drawing from a voided domain returns None and lets the store settle done."
    store = UrlStore()
    store.add_urls(["https://a.org/1"])
    store._bust(store.urldict["https://a.org"])
    assert store.get_url("https://a.org") is None
    assert store.done is True


def test_discard_non_http_key_in_lease_mode(frozen_clock):
    "Discarding a non-http(s) key still voids it and settles the store."
    store = UrlStore(lease_mode=True)
    store._store_urls("ftp://example.org", deque())
    store.discard(["ftp://example.org"])
    assert store.urldict["ftp://example.org"].state is State.BUSTED
    assert store.done is True
