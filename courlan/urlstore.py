"""
Defines a URL store which holds URLs along with relevant information and entails crawling helpers.
"""

import gc
import logging
import pickle
import signal
import sys

try:
    import bz2

    HAS_BZ2 = True
except ImportError:
    HAS_BZ2 = False

try:
    import zlib

    HAS_ZLIB = True
except ImportError:
    HAS_ZLIB = False


from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from enum import Enum
from itertools import islice
from operator import itemgetter
from threading import RLock
from typing import Any
from urllib.robotparser import RobotFileParser
from uuid import uuid4

from .clean import normalize_and_split, normalize_netloc_parts
from .core import filter_links
from .filters import lang_filter, validate_url
from .meta import clear_caches
from .urlutils import _parse, _slash_variant, _swap_scheme, get_base_url

LOGGER = logging.getLogger(__name__)

# default lifetime of a lease, in seconds
DEFAULT_LEASE_DURATION = 300.0


def _now() -> datetime:
    "Return the current time, patched by the fixed-clock tests."
    return datetime.now()


def _decompress(data: bytes) -> bytes:
    "Decompress by magic bytes, stores written with bz2 included."
    if data[:3] == b"BZh" and HAS_BZ2:
        return bz2.decompress(data)
    if data[:1] == b"\x78" and HAS_ZLIB:
        return zlib.decompress(data)
    return data


class Compressor:
    "Use system information on available compression modules and define corresponding methods."

    __slots__ = ("compressor", "decompressor")

    def __init__(self, compression: bool = True) -> None:
        self.compressor: Callable[[bytes], bytes]
        # pickles start with b"\x80", never with a compression magic byte
        self.decompressor: Callable[[bytes], bytes] = _decompress
        if compression and HAS_ZLIB:
            self.compressor = zlib.compress
        elif compression and HAS_BZ2:
            self.compressor = bz2.compress
        else:
            self.compressor = self._identical

    @staticmethod
    def _identical(data: Any) -> Any:
        "Return unchanged data."
        return data

    def compress(self, data: Any) -> Any:
        "Pickle the data and compress it if a method is available."
        return self.compressor(pickle.dumps(data, protocol=5))

    def decompress(self, data: bytes) -> Any:
        "Decompress the data if a method is available and load the object."
        return pickle.loads(self.decompressor(data))


COMPRESSOR = Compressor()


class State(Enum):
    "Record state information about a domain or host."

    OPEN = 1
    ALL_VISITED = 2
    BUSTED = 3


class DomainEntry:
    "Class to record host-related information and URL paths."

    __slots__ = ("count", "cursor", "rules", "state", "timestamp", "total", "tuples")

    def __init__(self, state: State = State.OPEN) -> None:
        self.count: int = 0
        # all URLs before the cursor are visited
        self.cursor: int = 0
        self.rules: bytes | RobotFileParser | None = None
        self.state: State = state
        self.timestamp: datetime | None = None
        self.total: int = 0
        self.tuples: bytes | deque[UrlPathTuple] = deque()

    def __setstate__(self, state: tuple[None, dict[str, Any]]) -> None:
        "Fill in the slots missing from older pickles."
        self.__init__()  # type: ignore[misc]
        for slot, value in state[1].items():
            setattr(self, slot, value)


class UrlPathTuple:
    "Class storing information for URL paths relative to a domain/host."

    __slots__ = ("urlpath", "visited")

    def __init__(self, urlpath: str, visited: bool) -> None:
        self.urlpath: bytes = urlpath.encode("utf-8")
        self.visited: bool = visited

    def path(self) -> str:
        "Get the URL path as string."
        return self.urlpath.decode("utf-8")


class Lease:
    "A claim on an unvisited URL, identified by an opaque token."

    __slots__ = ("token", "domain", "path", "expires")

    def __init__(self, token: str, domain: str, path: str, expires: datetime) -> None:
        self.token: str = token
        self.domain: str = domain
        self.path: str = path
        self.expires: datetime = expires

    def is_expired(self, now: datetime) -> bool:
        "Tell if the lease has run out at the given time."
        return now >= self.expires


PathIndex = dict[str, UrlPathTuple]

# positions checked by index before _first_unvisited walks the deque
_NEAR = 64


def _host_key(domain: str) -> str:
    "Return the store key of a domain or URL: normalized scheme and host, no userinfo or path."
    parsed = _parse(domain)
    if not parsed.netloc:
        return domain
    try:
        scheme, _, hostport = normalize_netloc_parts(parsed)
    except ValueError:  # e.g. a port over int()'s digit limit, matches no key
        return domain
    return f"{scheme}://{hostport}"


def _lookup(path: str, index: PathIndex) -> UrlPathTuple | None:
    "Find the stored tuple for a path or its trailing slash variant."
    return index.get(path) or index.get(_slash_variant(path))


def _first_unvisited(urls: deque[UrlPathTuple], start: int) -> int:
    "Return the index of the first unvisited URL from start on, len(urls) if none."
    # index access for short gaps, then one islice pass, since deque[i] walks from an end
    near = min(start + _NEAR, len(urls))
    for i in range(start, near):
        if not urls[i].visited:
            return i
    return next(
        (i for i, u in enumerate(islice(urls, near, None), near) if not u.visited),
        len(urls),
    )


def _first_claimable(
    urls: deque[UrlPathTuple],
    leased: set[tuple[str, str]],
    domain: str,
    start: int = 0,
) -> int:
    """Return the index of the first unvisited URL without a live lease,
    len(urls) if none."""
    return next(
        (
            i
            for i, u in enumerate(islice(urls, start, None), start)
            if not u.visited and (domain, u.path()) not in leased
        ),
        len(urls),
    )


def _dedup_extend(
    urls: deque[UrlPathTuple],
    index: PathIndex,
    tuples: Iterable[UrlPathTuple],
    left: bool = False,
) -> int:
    """Add tuples whose paths are not yet known, in input order, and return their number.
    A visited input marks the stored URL as visited, never the reverse."""
    new = []
    for utuple in tuples:
        path = utuple.path()
        known = _lookup(path, index)
        if known is None:
            new.append(utuple)
            index[path] = utuple
        elif utuple.visited:
            known.visited = True
    if left:
        urls.extendleft(reversed(new))
    else:
        urls.extend(new)
    return len(new)


class UrlStore:
    """Defines a class to store domain-classified URLs and perform checks against it.

    Domains are keyed in normalized form: lowercase, unicode host, no userinfo
    or default port, under https if both twins are known. Readers accept any form.

    Thread-safety: each public method runs under one store lock, so calls are
    atomic, but a sequence of calls is not."""

    __slots__ = (
        "compressed",
        "done",
        "language",
        "strict",
        "trailing_slash",
        "urldict",
        "_hot",
        "_index",
        "_lease_mode",
        "_leased",
        "_leases",
        "_lock",
        "_offset",
    )

    def __init__(
        self,
        compressed: bool = False,
        language: str | None = None,
        strict: bool = False,
        trailing_slash: bool = True,
        verbose: bool = False,
        lease_mode: bool = False,
    ) -> None:
        self.compressed: bool = compressed
        self.done: bool = False
        self.language: str | None = language
        self.strict: bool = strict
        self.trailing_slash: bool = trailing_slash
        self.urldict: dict[str, DomainEntry] = {}
        # last written domain, kept uncompressed, and its path index
        self._hot: DomainEntry | None = None
        self._index: PathIndex | None = None
        # opt-in lease mode: claims hand out tokens instead of marking URLs
        self._lease_mode: bool = lease_mode
        # token -> Lease, and the (domain key, path) pairs under a live lease
        self._leases: dict[str, Lease] = {}
        self._leased: set[tuple[str, str]] = set()
        self._lock: RLock = RLock()
        # rotating start of the download methods
        self._offset: int = 0

        def dump_unvisited_urls(num: Any, frame: Any) -> None:
            LOGGER.debug(
                "Processing interrupted, dumping unvisited URLs from %s hosts",
                len(self.urldict),
            )
            self.print_unvisited_urls()
            sys.exit(1)

        # opt-in only (mostly for CLI use), and not on Windows:
        # existing handlers are replaced since the caller asked for it
        if verbose and not sys.platform.startswith("win"):
            try:
                signal.signal(signal.SIGINT, dump_unvisited_urls)
                signal.signal(signal.SIGTERM, dump_unvisited_urls)
            except ValueError:
                # signal handlers can only be registered in the main thread
                LOGGER.warning("Cannot set signal handlers outside the main thread")

    def __getstate__(self) -> dict[str, Any]:
        "Return the picklable state, excluding the lock, the hot domain and live leases."
        with self._lock:
            self._freeze()
            return {
                slot: getattr(self, slot)
                for slot in self.__slots__
                if slot not in ("_lock", "_leases", "_leased")
            }

    def __setstate__(self, state: dict[str, Any]) -> None:
        "Restore state after unpickling, older stores included."
        self._hot = self._index = None
        self._lock = RLock()
        self._offset = 0
        # leases never survive a reload: every pre-load token is invalid
        self._leases = {}
        self._leased = set()
        for slot, value in state.items():
            setattr(self, slot, value)
        # older stores predate lease mode
        if not hasattr(self, "_lease_mode"):
            self._lease_mode = False
        # older stores use a defaultdict, whose reads create entries
        self.urldict = dict(self.urldict)

    def _split(self, url: str) -> tuple[str, str, str] | None:
        "Return the normalized URL, the store key and the path, None if the URL is invalid."
        _, parsed_url = validate_url(url)
        if parsed_url is None:
            return None
        try:
            return normalize_and_split(
                parsed_url, self.strict, self.language, self.trailing_slash
            )
        except ValueError:
            return None

    def _buffer_urls(
        self, data: list[str], visited: bool
    ) -> defaultdict[str, deque[UrlPathTuple]]:
        inputdict: defaultdict[str, deque[UrlPathTuple]] = defaultdict(deque)
        for url in dict.fromkeys(data):
            split = self._split(url)
            # filter on the final form, as in check_url
            if split is None or (
                self.language is not None
                and lang_filter(
                    split[0], self.language, self.strict, self.trailing_slash
                )
                is False
            ):
                LOGGER.warning("Discarding URL: %s", url)
                continue
            inputdict[split[1]].append(UrlPathTuple(split[2], visited))
        return inputdict

    def _find(self, key: str) -> str | None:
        "Return the stored key for a normalized key or its http/https twin."
        if key in self.urldict:
            return key
        if (
            key.startswith(("http://", "https://"))
            and (twin := _swap_scheme(key)) in self.urldict
        ):
            return twin
        return None

    def _load_urls(self, domain: str) -> deque[UrlPathTuple]:
        raw = self.urldict[domain].tuples
        if isinstance(raw, bytes):  # compressed
            result: deque[UrlPathTuple] = COMPRESSOR.decompress(raw)
            return result
        return raw

    def _paths(self, key: str) -> PathIndex:
        "Return the path index of a stored domain, kept for the hot one."
        if self.urldict[key] is self._hot:
            return self._hot_index()
        return {u.path(): u for u in self._load_urls(key)}

    def _hot_index(self) -> PathIndex:
        "Return the path index of the hot domain, built on first use."
        if self._index is None:
            assert self._hot is not None and isinstance(self._hot.tuples, deque)
            self._index = {u.path(): u for u in self._hot.tuples}
        return self._index

    def _freeze(self) -> None:
        "Compress the hot domain again and drop its index."
        if self._hot is not None:
            if self.compressed:
                self._hot.tuples = COMPRESSOR.compress(self._hot.tuples)
            self._hot = self._index = None

    def _live(self, key: str) -> deque[UrlPathTuple]:
        """Return the deque of a stored domain for writing.
        In compressed mode it stays uncompressed until another domain is written."""
        entry = self.urldict[key]
        if entry is not self._hot:
            self._freeze()
            entry.tuples = self._load_urls(key)
            self._hot = entry
        return self._load_urls(key)

    def _bust(self, entry: DomainEntry) -> None:
        "Declare an entry void in place, a final state."
        if entry is self._hot:
            self._hot = self._index = None
        entry.__init__(State.BUSTED)  # type: ignore[misc]

    def _purge_leases(self, domain: str) -> None:
        "Invalidate the live leases of a domain, e.g. before it is voided."
        for token in [t for t, lease in self._leases.items() if lease.domain == domain]:
            lease = self._leases.pop(token)
            self._leased.discard((domain, lease.path))

    def _rekey_leases(
        self, source: str, target: str, index: PathIndex | None = None
    ) -> None:
        """Move the live leases of a merged twin onto the surviving key.
        Paths follow slash-variant dedup and colliding leases are dropped."""
        if not any(lease.domain == source for lease in self._leases.values()):
            return
        paths = index or self._paths(target)
        for token in [t for t, lease in self._leases.items() if lease.domain == source]:
            lease = self._leases.pop(token)
            self._leased.discard((source, lease.path))
            known = _lookup(lease.path, paths)
            path = known.path() if known is not None else lease.path
            key = (target, path)
            if key in self._leased:
                # the surviving twin already holds this URL: one valid lease only
                continue
            lease.domain = target
            lease.path = path
            self._leases[token] = lease
            self._leased.add(key)

    def _update(self, entry: DomainEntry, urls: deque[UrlPathTuple]) -> None:
        "Refresh the counters and the state of an entry after a write."
        if entry.state is State.BUSTED:
            return
        entry.total = len(urls)
        entry.cursor = _first_unvisited(urls, entry.cursor)
        if entry.cursor < len(urls):
            entry.state = State.OPEN
            self.done = False
        else:
            entry.state = State.ALL_VISITED

    def _set_done(self) -> None:
        if not self.done and all(v.state != State.OPEN for v in self.urldict.values()):
            self.done = True

    def _merge_entries(self, target: str, source: str) -> None:
        "Merge the source entry into the target one; a discarded twin voids the domain."
        tgt, src = self.urldict[target], self.urldict[source]
        if State.BUSTED in (tgt.state, src.state):
            self._purge_leases(target)
            self._purge_leases(source)
            self._bust(tgt)
            self._bust(src)
            return
        source_urls = self._load_urls(source)
        if src is self._hot:
            # the source is deleted after the merge, no need to compress it
            self._hot = self._index = None
        urls = self._live(target)
        _dedup_extend(urls, self._hot_index(), source_urls)
        self._rekey_leases(source, target, self._hot_index())
        tgt.count += src.count
        if tgt.rules is None:
            tgt.rules = src.rules
        if src.timestamp and (not tgt.timestamp or src.timestamp > tgt.timestamp):
            tgt.timestamp = src.timestamp
        self._update(tgt, urls)

    def _merge_twin(self, domain: str) -> str:
        """Canonicalize the domain under the https key if a twin exists,
        merging coexisting entries into a single one."""
        if not domain.startswith(("http://", "https://")):
            return domain
        https_key = domain if domain.startswith("https://") else _swap_scheme(domain)
        http_key = _swap_scheme(https_key)
        if https_key in self.urldict:
            if http_key in self.urldict:
                self._merge_entries(https_key, http_key)
                del self.urldict[http_key]
            return https_key
        if domain == https_key and http_key in self.urldict:
            # upgrade the existing http entry to the https key
            entry = self.urldict.pop(http_key)
            self.urldict[https_key] = entry
            self._rekey_leases(http_key, https_key)
            return https_key
        return domain

    def _store_urls(
        self, domain: str, tuples: deque[UrlPathTuple], left: bool = False
    ) -> None:
        domain = self._merge_twin(domain)
        if domain not in self.urldict:
            self.urldict[domain] = DomainEntry()
        entry = self.urldict[domain]
        if entry.state is State.BUSTED:
            return
        urls = self._live(domain)
        index = self._hot_index()
        added = _dedup_extend(urls, index, tuples, left)
        if left and added:
            entry.cursor = 0
        self._update(entry, urls)

    def _search_urls(
        self, splits: dict[str, tuple[str, str, str] | None], visited: bool
    ) -> list[str]:
        "Return the URLs not stored, or not visited, in input order."
        remaining = []
        indexes: dict[str, PathIndex] = {}
        for url, split in splits.items():
            key = self._find(split[1]) if split else None
            if split and key:
                if key not in indexes:
                    indexes[key] = self._paths(key)
                known = _lookup(split[2], indexes[key])
                if known and (not visited or known.visited):
                    continue
            remaining.append(url)
        return remaining

    # ADDITIONS AND DELETIONS

    def add_urls(
        self,
        urls: list[str] | None = None,
        appendleft: list[str] | None = None,
        visited: bool = False,
    ) -> None:
        """Add a list of URLs to the (possibly) existing one.
        Optional: append certain URLs to the left,
        specify if the URLs have already been visited."""
        # normalize outside the lock
        right = self._buffer_urls(urls, visited) if urls else {}
        left = self._buffer_urls(appendleft, visited) if appendleft else {}
        with self._lock:
            for host, urltuples in right.items():
                self._store_urls(host, urltuples)
            for host, urltuples in left.items():
                self._store_urls(host, urltuples, left=True)

    def add_from_html(
        self,
        htmlstring: str,
        url: str,
        external: bool = False,
        lang: str | None = None,
        with_nav: bool = True,
    ) -> None:
        "Find links in a HTML document, filter them and add them to the data store."
        base_url = get_base_url(url)
        rules = self.get_rules(base_url)
        links, links_priority = filter_links(
            htmlstring=htmlstring,
            url=url,
            external=external,
            lang=lang or self.language,
            rules=rules,
            strict=self.strict,
            with_nav=with_nav,
        )
        self.add_urls(urls=links, appendleft=links_priority)

    def discard(self, domains: list[str]) -> None:
        "Declare domains void and prune the store, http/https twins included."
        with self._lock:
            for domain in domains:
                key = _host_key(domain)
                # purge both twins before the merge deletes one of the keys
                if key.startswith(("http://", "https://")):
                    self._purge_leases(key)
                    self._purge_leases(_swap_scheme(key))
                domain = self._merge_twin(key)
                self._bust(self.urldict.setdefault(domain, DomainEntry()))
            self._set_done()
        num = gc.collect()
        LOGGER.debug("%s objects in GC after UrlStore.discard", num)

    def reset(self) -> None:
        "Re-initialize the URL store."
        with self._lock:
            self.urldict = {}
            self._hot = self._index = None
            # every outstanding token loses its target
            self._leases = {}
            self._leased = set()
        clear_caches()
        num = gc.collect()
        LOGGER.debug("UrlStore reset, %s objects in GC", num)

    # DOMAINS / HOSTNAMES

    def get_known_domains(self) -> list[str]:
        "Return all known domains as a list."
        with self._lock:
            return list(self.urldict.keys())

    def get_unvisited_domains(self) -> list[str]:
        """Find all domains for which there are unvisited URLs
        and potentially adjust done meta-information."""
        with self._lock:
            return [d for d, v in self.urldict.items() if v.state == State.OPEN]

    def is_known_domain(self, domain: str) -> bool:
        "Tell if the website is in store, in any form."
        with self._lock:
            return self._find(_host_key(domain)) is not None

    def is_exhausted_domain(self, domain: str) -> bool:
        "Tell if all known URLs for the website have been visited."
        with self._lock:
            key = self._find(_host_key(domain))
            return key is not None and self.urldict[key].state != State.OPEN

    def unvisited_websites_number(self) -> int:
        "Return the number of websites for which there are still URLs to visit."
        return len(self.get_unvisited_domains())

    # URL-BASED QUERIES

    def find_known_urls(self, domain: str) -> list[str]:
        """Get all already known URLs for the given domain (ex. "https://example.org")."""
        with self._lock:
            key = self._find(_host_key(domain))
            return [key + u.path() for u in self._load_urls(key)] if key else []

    def find_unvisited_urls(self, domain: str) -> list[str]:
        "Get all unvisited URLs for the given domain."
        with self._lock:
            key = self._find(_host_key(domain))
            return self._unvisited(key) if key else []

    def _unvisited(self, key: str) -> list[str]:
        if self.urldict[key].state is not State.OPEN:
            return []
        return [key + u.path() for u in self._load_urls(key) if not u.visited]

    def filter_unknown_urls(self, urls: list[str]) -> list[str]:
        "Take a list of URLs and return the currently unknown ones."
        splits = {url: self._split(url) for url in urls}
        with self._lock:
            return self._search_urls(splits, visited=False)

    def filter_unvisited_urls(self, urls: list[str]) -> list[str]:
        "Take a list of URLs and return the currently unvisited ones."
        splits = {url: self._split(url) for url in urls}
        with self._lock:
            return self._search_urls(splits, visited=True)

    def has_been_visited(self, url: str) -> bool:
        "Check if the given URL has already been visited."
        return not self.filter_unvisited_urls([url])

    def is_known(self, url: str) -> bool:
        "Check if the given URL has already been stored."
        return not self.filter_unknown_urls([url])

    # DOWNLOADS

    def get_url(self, domain: str, as_visited: bool = True) -> str | None:
        "Retrieve a single URL and consider it to be visited (with corresponding timestamp)."
        if self._lease_mode:
            raise RuntimeError(
                "consumption-style get_url() is disabled in lease mode, "
                "use claim_url() and confirm_url()"
            )
        with self._lock:
            return self._get_url(_host_key(domain), as_visited)

    def _get_url(self, key: str, as_visited: bool = True) -> str | None:
        "Draw a URL from a key already in stored form."
        stored = self._find(key)
        if stored is None:
            return None
        # merge any twin first so the writes below stay on one key
        domain = self._merge_twin(stored)
        entry = self.urldict[domain]
        if entry.state is State.OPEN:
            urls = self._live(domain)
            i = _first_unvisited(urls, entry.cursor)
            if i < len(urls):
                url = urls[i]
                if as_visited:
                    url.visited = True
                    entry.cursor = i + 1
                    entry.count += 1
                    entry.timestamp = datetime.now()
                    self._update(entry, urls)
                return domain + url.path()
            # nothing to draw from
            entry.state = State.ALL_VISITED
        self._set_done()
        return None

    def _rotated(self) -> list[tuple[int, str]]:
        "Snapshot of (position, key) pairs, starting after the last domain served."
        websites = list(self.urldict)
        start = self._offset % len(websites) if websites else 0
        return list(enumerate(websites[start:] + websites[:start], start))

    def get_download_urls(
        self,
        time_limit: float = 10.0,
        max_urls: int = 10000,
    ) -> list[str]:
        """Get a list of immediately downloadable URLs according to the given
        time limit per domain. Each call starts after the last domain served."""
        if self._lease_mode:
            raise RuntimeError(
                "consumption-style get_download_urls() is disabled in lease mode, "
                "use claim_download_urls()"
            )
        with self._lock:
            urls: list[str] = []
            # snapshot the keys: _get_url may merge twin entries and mutate the dict
            for position, website in self._rotated():
                entry = self.urldict.get(website)
                if entry is None or entry.state != State.OPEN:
                    continue
                if (
                    not entry.timestamp
                    or (datetime.now() - entry.timestamp).total_seconds() > time_limit
                ):
                    url = self._get_url(website)
                    if url is not None:
                        urls.append(url)
                        self._offset = position + 1
                        if len(urls) >= max_urls:
                            break
            self._set_done()
            return urls

    def establish_download_schedule(
        self, max_urls: int = 100, time_limit: int = 10
    ) -> list[tuple[float, str]]:
        """Get up to the specified number of URLs along with a suitable
        backoff schedule (in seconds). Each call starts after the last domain served."""
        if self._lease_mode:
            raise RuntimeError(
                "consumption-style establish_download_schedule() is disabled "
                "in lease mode, use claim_download_urls()"
            )
        with self._lock:
            # see which domains are free
            potential = [
                (position, domain)
                for position, domain in self._rotated()
                if self.urldict[domain].state == State.OPEN
            ]
            if not potential:
                return []
            # variables init
            per_domain = max_urls // len(potential) or 1
            targets: list[tuple[float, str]] = []
            # iterate potential domains
            for position, domain in potential:
                if len(targets) >= max_urls:
                    break
                # merge any twin first so the writes below stay on one key
                domain = self._merge_twin(domain)
                entry = self.urldict[domain]
                urls = self._live(domain)
                urlpaths: list[str] = []
                # first unvisited URLs, the cursor follows
                limit = min(per_domain, max_urls - len(targets))
                # one islice pass, per-URL index access is slower
                for i, utuple in enumerate(
                    islice(urls, entry.cursor, None), entry.cursor
                ):
                    if len(urlpaths) == limit:
                        break
                    if not utuple.visited:
                        utuple.visited = True
                        urlpaths.append(utuple.path())
                        entry.cursor = i + 1
                if urlpaths:
                    self._offset = position + 1
                # determine timestamps
                now = datetime.now()
                original_timestamp = entry.timestamp
                if (
                    not original_timestamp
                    or (now - original_timestamp).total_seconds() > time_limit
                ):
                    schedule_secs = 0.0
                else:
                    schedule_secs = time_limit - round(
                        (now - original_timestamp).total_seconds(), 2
                    )
                for urlpath in urlpaths:
                    targets.append((schedule_secs, domain + urlpath))
                    schedule_secs += time_limit
                # store new info, offset of the last addition
                entry.count += len(urlpaths)
                entry.timestamp = now + timedelta(0, schedule_secs - time_limit)
                self._update(entry, urls)
            # sort by first tuple element (time in secs)
            self._set_done()
            return sorted(targets, key=itemgetter(0))

    # LEASES

    def _sweep_leases(self, now: datetime, domain: str | None = None) -> None:
        "Drop expired leases, making their URLs claimable again."
        for token in [
            t
            for t, lease in self._leases.items()
            if lease.is_expired(now) and (domain is None or lease.domain == domain)
        ]:
            lease = self._leases.pop(token)
            self._leased.discard((lease.domain, lease.path))

    def _claim_url(
        self, key: str, duration: float, now: datetime
    ) -> tuple[str, str] | None:
        "Draw a claimable URL from a key already in stored form, None if none is free."
        stored = self._find(key)
        if stored is None:
            return None
        # merge any twin first so the writes below stay on one key
        domain = self._merge_twin(stored)
        entry = self.urldict[domain]
        if entry.state is State.OPEN:
            self._sweep_leases(now, domain)
            urls = self._live(domain)
            i = _first_claimable(urls, self._leased, domain)
            if i < len(urls):
                url = urls[i]
                token = uuid4().hex
                path = url.path()
                self._leases[token] = Lease(
                    token,
                    domain,
                    path,
                    now + timedelta(seconds=duration),
                )
                self._leased.add((domain, path))
                # the interval is timed from the claim, the URL stays unvisited
                entry.count += 1
                entry.timestamp = now
                return domain + path, token
        self._set_done()
        return None

    def claim_url(
        self, domain: str, lease_duration: float = DEFAULT_LEASE_DURATION
    ) -> tuple[str, str] | None:
        """Claim a single URL of the domain without marking it visited.
        Return the URL and an opaque token, or None if no URL is currently
        claimable. Confirm the token once the fetch succeeds, or release it."""
        if not self._lease_mode:
            raise RuntimeError("claim_url() requires lease_mode=True")
        with self._lock:
            return self._claim_url(_host_key(domain), lease_duration, _now())

    def claim_download_urls(
        self,
        time_limit: float = 10.0,
        max_urls: int = 10000,
        lease_duration: float = DEFAULT_LEASE_DURATION,
    ) -> list[tuple[str, str]]:
        """Claim immediately downloadable URLs with the same host rotation and
        per-host interval as get_download_urls(). Each call starts after the
        last host served and returns (URL, token) pairs."""
        if not self._lease_mode:
            raise RuntimeError("claim_download_urls() requires lease_mode=True")
        with self._lock:
            now = _now()
            claims: list[tuple[str, str]] = []
            # snapshot the keys: _claim_url may merge twin entries and mutate the dict
            for position, website in self._rotated():
                entry = self.urldict.get(website)
                if entry is None or entry.state != State.OPEN:
                    continue
                if (
                    not entry.timestamp
                    or (now - entry.timestamp).total_seconds() > time_limit
                ):
                    claim = self._claim_url(website, lease_duration, now)
                    if claim is not None:
                        claims.append(claim)
                        self._offset = position + 1
                        if len(claims) >= max_urls:
                            break
            self._set_done()
            return claims

    def _invalidate(self, token: str, now: datetime) -> Lease | None:
        "Remove a valid lease by token; expired or unknown tokens change nothing."
        lease = self._leases.get(token)
        if lease is None:
            return None
        if lease.is_expired(now):
            self._leases.pop(token)
            self._leased.discard((lease.domain, lease.path))
            return None
        self._leases.pop(token)
        self._leased.discard((lease.domain, lease.path))
        return lease

    def confirm_url(self, token: str) -> bool:
        """Mark the URL held by the token as visited.
        Return False, without changing any state, on an unknown, duplicate or
        expired token."""
        if not self._lease_mode:
            raise RuntimeError("confirm_url() requires lease_mode=True")
        with self._lock:
            now = _now()
            lease = self._invalidate(token, now)
            if lease is None:
                return False
            entry = self.urldict.get(lease.domain)
            if entry is None or entry.state is State.BUSTED:
                return False
            urls = self._live(lease.domain)
            known = _lookup(lease.path, self._hot_index())
            if known is None:
                return False
            known.visited = True
            self._update(entry, urls)
            self._set_done()
            return True

    def release_url(self, token: str) -> bool:
        """Give up a claim so its URL can be claimed again, the URL stays
        unvisited. Return False, without changing any state, on an unknown,
        duplicate or expired token."""
        if not self._lease_mode:
            raise RuntimeError("release_url() requires lease_mode=True")
        with self._lock:
            now = _now()
            lease = self._invalidate(token, now)
            if lease is None:
                return False
            # the in-flight URL belongs to the unvisited set again
            entry = self.urldict.get(lease.domain)
            if entry is not None and entry.state is State.ALL_VISITED:
                entry.state = State.OPEN
                self.done = False
            return True

    # CRAWLING

    def store_rules(self, website: str, rules: RobotFileParser | None) -> None:
        "Store crawling rules for a given website."
        with self._lock:
            key = _host_key(website)
            stored = self._find(key)
            website = key if stored is None else self._merge_twin(stored)
            if self.compressed:
                rules = COMPRESSOR.compress(rules)
            if website not in self.urldict:
                # rules alone leave nothing to visit
                self.urldict[website] = DomainEntry(state=State.ALL_VISITED)
            self.urldict[website].rules = rules

    def get_rules(self, website: str) -> RobotFileParser | None:
        "Return the stored crawling rules for the given website."
        with self._lock:
            key = self._find(_host_key(website))
            if key is None:
                return None
            raw = self.urldict[key].rules
            if isinstance(raw, bytes):  # compressed
                result: RobotFileParser = COMPRESSOR.decompress(raw)
                return result
            return raw

    def get_crawl_delay(self, website: str, default: float = 5) -> float:
        "Return the delay as extracted from robots.txt, or a given default."
        rules = self.get_rules(website)
        if rules is None:
            return default
        delay = rules.crawl_delay("*")
        return float(delay) if delay is not None else default

    # GENERAL INFO

    def get_all_counts(self) -> list[int]:
        "Return all download counts for the hosts in store."
        with self._lock:
            return [v.count for v in self.urldict.values()]

    def total_url_number(self) -> int:
        "Find number of all URLs in store."
        with self._lock:
            return sum(v.total for v in self.urldict.values())

    def download_threshold_reached(self, threshold: float) -> bool:
        "Find out if the download threshold (number of retrieved URLs) has been reached for one of the websites in store."
        with self._lock:
            return any(v.count >= threshold for v in self.urldict.values())

    def dump_urls(self) -> list[str]:
        "Return a list of all known URLs."
        with self._lock:
            return [d + u.path() for d in self.urldict for u in self._load_urls(d)]

    def print_unvisited_urls(self) -> None:
        "Print all unvisited URLs in store."
        # print after releasing the lock
        with self._lock:
            urls = [url for domain in self.urldict for url in self._unvisited(domain)]
        if urls:
            print("\n".join(urls), flush=True)

    def print_urls(self) -> None:
        "Print all URLs in store (URL + TAB + visited or not)."
        with self._lock:
            urls = [
                f"{domain}{u.path()}\t{u.visited!s}"
                for domain in self.urldict
                for u in self._load_urls(domain)
            ]
        if urls:
            print("\n".join(urls), flush=True)

    # PERSISTENCE

    def write(self, filename: str) -> None:
        "Write the URL store to disk as a pickle file, see load_store()."
        with self._lock:
            with open(filename, "wb") as output:
                pickle.dump(self, output)


def load_store(filename: str) -> UrlStore:
    """Load a URL store from disk.

    Warning: uses pickle, which can execute arbitrary code.
    Only load files you have written yourself with UrlStore.write()."""
    with open(filename, "rb") as output:
        url_store: UrlStore = pickle.load(output)
    return url_store
