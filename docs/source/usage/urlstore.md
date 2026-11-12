# URL Store — Domain-Classified URL Storage

The `UrlStore` class allows for storing and retrieving domain-classified URLs, where a URL like `https://example.org/path/page` is stored as the path `/path/page` within the domain `https://example.org`. It tracks visited and unvisited URLs per domain, supports crawl scheduling with per-domain delays, and can persist state to disk.


## Basic UrlStore usage

```{note}
`add_urls` silently drops URLs that fail validation. If URLs seem to
disappear, check them with `check_url` first to see why they are rejected.
```

```python
from courlan import UrlStore

store = UrlStore()
store.add_urls([
    'https://example.com/page1',
    'https://example.com/page2',
    'https://example.org/article',
])

# Retrieve a URL (marks it as visited with a timestamp)
url = store.get_url('https://example.com')
# 'https://example.com/page1'

# Check what's left
store.find_unvisited_urls('https://example.com')
# ['https://example.com/page2']
```

Domains are stored in one normalized form: lowercase, unicode instead of punycode, without credentials or default port, and under `https` once both twins are known.
Every method accepts any form of a URL, or of a domain with its scheme, and returns URLs in the stored form.

```python
store.add_urls(['https://Example.org/a', 'http://xn--mnchen-3ya.de/b'])
store.find_known_urls('https://example.org/')
# ['https://example.org/article', 'https://example.org/a']
store.find_known_urls('http://münchen.de')
# ['http://münchen.de/b']
```


## UrlStore constructor options

| Option | Effect |
|--------|--------|
| `compressed=True` | Compress stored URLs and rules to reduce memory, except for the domain written last |
| `language='en'` | Filter added URLs by target language (ISO 639-1 code) |
| `strict=True` | Apply stricter URL filtering on add |
| `trailing_slash=True` | Preserve trailing slashes (set to `False` to strip them) |
| `verbose=True` | Dump unvisited URLs on SIGINT/SIGTERM (POSIX only, main thread only; replaces existing handlers) |
| `lease_mode=True` | Hand out token-based leases instead of marking URLs visited on draw (see [Lease mode](#lease-mode-reliable-re-crawling-of-failed-urls)) |

```python
filtered_store = UrlStore(language='en', strict=True, compressed=True)
```


## Tracking visited and unvisited URLs

```python
# Check if a URL is already known
store.is_known('https://example.com/page1')
# True

# Check if it has been visited
store.has_been_visited('https://example.com/page1')
# True (we called get_url above)

# Filter a list to only unknown or unvisited URLs
new_urls = ['https://example.com/page1', 'https://example.com/new']
store.filter_unknown_urls(new_urls)
# ['https://example.com/new']
store.filter_unvisited_urls(new_urls)
# ['https://example.com/new']
```


## Adding links from HTML

Extract, filter, and add links from a page in one call:

```python
html = '<a href="/page3">Link</a><a href="https://other.com/x">External</a>'
store.add_from_html(html, 'https://example.com')
# internal links added; external links ignored by default

# include external links
store.add_from_html(html, 'https://example.com', external=True)

# filter by language (note: the parameter is `lang`, not `language`)
store.add_from_html(html, 'https://example.com', lang='en')
```


## UrlStore statistics and inspection

```python
store.total_url_number()         # total URLs across all domains
store.get_known_domains()        # list of all domains
store.get_unvisited_domains()    # domains with unvisited URLs
store.unvisited_websites_number()  # count of such domains
store.get_all_counts()           # download counts per host
store.is_known_domain('https://example.com')      # domain in store, in any form?
store.is_exhausted_domain('https://example.com')  # all URLs visited?

# Per-domain inspection
store.find_known_urls('https://example.com')
store.find_unvisited_urls('https://example.com')

# Store-wide
store.dump_urls()                # all URLs from all domains, as a flat list
```


## Printing and resetting

```python
store.print_unvisited_urls()     # print all unvisited URLs to stdout
store.print_urls()               # print all URLs with visited/unvisited status
store.reset()                    # clear the store and internal caches
```


## Discarding domains

Remove entire domains from the store:

```python
store.discard(['https://spam.example.com'])
```


## Saving and loading UrlStore state

Save and restore state across sessions. `UrlStore.write()` stores state
using Python's pickle format (see security note below). `load_store()`
restores a previously saved store.

```python
from courlan import UrlStore, load_store

store = UrlStore()
store.add_urls(['https://example.com/1', 'https://example.com/2'])
store.get_url('https://example.com')  # visit one

# Save state to disk
store.write('crawler_state.db')

# Later: resume from disk
store = load_store('crawler_state.db')
print(store.get_unvisited_domains())
```

### Persistence tips and compressed mode

- Use `compressed=True` at construction to reduce memory and on-disk
  footprint when saving large crawls: `UrlStore(compressed=True)`.
- For very large crawls, combine periodic saves with incremental files
  (e.g., `crawler_state-0001.db`, `crawler_state-0002.db`) to avoid
  single large writes and to make resuming more robust.
- If you need a human-inspectable export, use `store.dump_urls()` and
  write the flat list to a newline-delimited file.

### Common crawl workflow

A typical crawl loop using UrlStore:

```python
store = UrlStore(language='en', strict=True, compressed=True)
seed_urls = ['https://example.com']
store.add_urls(seed_urls)

while store.unvisited_websites_number() > 0:
    domains = store.get_unvisited_domains()
    if not domains:
        break
    url = store.get_url(domains[0])
    html = fetch(url)  # your downloader
    store.add_from_html(html, url, external=False)
    # process page and enqueue new links
    if should_checkpoint():
        store.write('crawler_state_checkpoint.db')
```

This pattern: keep frequently-accessed state in memory, persist
periodically, and prefer compressed mode for long runs.

```{warning}
`write()`/`load_store()` use Python's pickle format, which can execute
arbitrary code when loading. Only load files you have written yourself
or that you trust. Consider exporting via `dump_urls()` for sharing.
```


## Lease mode: reliable re-crawling of failed URLs

The classic download methods (`get_url()`, `get_download_urls()`,
`establish_download_schedule()`) mark a URL visited the moment it is handed
out. If the fetch then fails, that URL is never tried again. Construct the
store with `lease_mode=True` to switch to a claim/confirm workflow: a claim
returns the URL together with an opaque token and leaves the URL unvisited
until the token is confirmed.

```python
store = UrlStore(lease_mode=True)
store.add_urls(['https://example.org/1', 'https://example.org/2'])

# claim a URL: it is reserved to this token but still counts as unvisited
url, token = store.claim_url('https://example.org')
# ('https://example.org/1', '9f2c…')

try:
    fetch(url)
except FetchError:
    store.release_url(token)   # give it back, it can be claimed again
else:
    store.confirm_url(token)   # only now is the URL marked visited
```

### Claims, tokens and expiration

- `claim_url(domain, lease_duration=300)` returns a `(url, token)` pair, or
  `None` when the domain currently has no claimable URL.
- A URL under a live lease is never handed out twice, so concurrent workers
  cannot draw the same URL: each URL has at most one valid token at a time.
- `confirm_url(token)` marks the URL visited and returns `True`. An unknown,
  already-used or expired token returns `False` and changes nothing.
- `release_url(token)` frees the URL for another attempt while keeping it
  unvisited; it returns `False` for an invalid token.
- Every lease has a lifetime (`lease_duration` seconds, default
  `DEFAULT_LEASE_DURATION = 300`). Once it expires the URL becomes claimable
  again and the old token can no longer be confirmed or released — a
  confirmation that arrives late is simply ignored.
- While URLs are in flight (leased but not confirmed) they still belong to
  the unvisited set, so `done` stays `False` and the domain remains listed by
  `get_unvisited_domains()`.

### Bulk claims with host rotation

`claim_download_urls()` is the lease-mode counterpart of
`get_download_urls()`: it keeps the same rotation across hosts and the same
per-host `time_limit`, measured from the moment of each claim, and returns a
list of `(url, token)` pairs.

```python
claims = store.claim_download_urls(time_limit=10, max_urls=50)
for url, token in claims:
    try:
        fetch(url)
    except FetchError:
        store.release_url(token)
    else:
        store.confirm_url(token)
```

In lease mode the consumption-style methods `get_url()`,
`get_download_urls()` and `establish_download_schedule()` raise
`RuntimeError`; conversely the claim/confirm methods raise `RuntimeError` on
a store built without `lease_mode`. A store without the option behaves
exactly as before.

### Persistence and invalidation

- Saving and reloading keeps the confirmed progress, while every URL that was
  still leased stays unvisited and keeps its place in the queue, so it is
  offered again by the next claims. **All tokens issued before the load become
  invalid**, including those that had not expired; workers must re-claim after
  a restart.
- `reset()` empties the store and invalidates every outstanding token.
- `discard()` voids the selected hosts, HTTP/HTTPS twins included, and
  invalidates their tokens.
- When an HTTP and an HTTPS entry for the same host are merged, outstanding
  leases follow onto the surviving key and their tokens remain valid; if both
  twins held the same path, the duplicate lease is dropped so only one valid
  token remains.
- Pickles written by older courlan versions load unchanged, with lease mode
  off and an empty lease table.
- Compressed and uncompressed stores behave identically in lease mode.


## Thread safety

Each public method runs under one store lock, so single calls are atomic across threads. A sequence of calls is not, for example `is_known()` followed by `add_urls()`.


## Performance tips

- Use `compressed=True` for large crawls to reduce memory usage
- Save periodically with `write()` to allow resume after interruptions
- Set `language` at construction time to filter URLs on add rather than later
- Clear internal caches periodically in long-running processes: `courlan.meta.clear_caches()`


## Next steps

For crawl-specific workflows (robots.txt, crawl delays, download scheduling, frontier management), see the [Web Crawling guide](crawling.md).
