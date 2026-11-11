"""
byparr-proxy: tiny HTTP server that fronts one or more upstream sites via
Byparr.

Prowlarr/Jackett talk to us in plain HTTP; we ask Byparr to fetch from the
real upstream and return its solved body directly. Prowlarr never sees a
Cloudflare challenge, so it never triggers its cookie-replay path -- which
is the path that modern Cloudflare detects and blocks.

Configuration is via environment variables:
  UPSTREAMS   multi mode. Comma-separated prefix=base-url pairs, e.g.
              "1337x=https://1337x.to,eztv=https://eztvx.to". Requests are
              routed by their first path segment (/1337x/..., /eztv/...).
  UPSTREAM    single (legacy) mode. Base URL of one indexer, e.g.
              https://1337x.to (no trailing slash). Used when UPSTREAMS is
              not set: the site is served at / with no prefix routing.
  BYPARR      Byparr /v1 endpoint, e.g. http://byparr:8191/v1
  TIMEOUT_MS  per-request timeout passed to Byparr (default 120000)
  PORT        local listen port (default 8888)
  LOG_LEVEL   DEBUG, INFO, WARNING, ERROR (default INFO)
  CACHE_TTL_S TTL for successful response cache, in seconds (default 3600)
  STUB_CAT_PATHS[_<prefix>]  if truthy (default), /cat/... paths (indexer
                  test endpoints) skip Byparr and return a synthetic
                  results page with one fake row per major category
                  family. Cloudflare's protection on the category browse
                  pages is significantly more aggressive than on keyword
                  search, so probing them through Byparr flaps the
                  indexer health even when real searches work fine. In
                  multi mode the suffixed form controls one prefix; the
                  bare form is the single-mode variable. Set "false"/"0"
                  to route /cat/... through Byparr like everything else.
  MAGNET_PATHS[_<prefix>]  regex of (prefix-stripped) paths whose fetched
                  page is reduced to its bare magnet URI, which the *arr
                  apps accept directly. Absent/empty means the default
                  ?d= marker (EZTV-style episode links).
"""
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.request
import uuid
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BYPARR = os.environ.get("BYPARR", "http://byparr:8191/v1")
TIMEOUT_MS = int(os.environ.get("TIMEOUT_MS", "120000"))
PORT = int(os.environ.get("PORT", "8888"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
CACHE_TTL_S = int(os.environ.get("CACHE_TTL_S", "3600"))

UPSTREAMS = {}
for _pair in os.environ.get("UPSTREAMS", "").split(","):
    if "=" in _pair:
        _prefix, _url = _pair.split("=", 1)
        _prefix = _prefix.strip().strip("/")
        _url = _url.strip().rstrip("/")
        if _prefix and _url:
            UPSTREAMS[_prefix] = _url

# Legacy single mode: one upstream, served at / with no prefix routing.
if not UPSTREAMS:
    _single = os.environ.get("UPSTREAM", "").strip().rstrip("/")
    if _single:
        UPSTREAMS[""] = _single
    else:
        sys.exit(
            "set UPSTREAMS (e.g. '1337x=https://1337x.to,eztv=https://eztvx.to') "
            "or UPSTREAM (single upstream, e.g. https://1337x.to)"
        )

LEGACY = "" in UPSTREAMS

# Skip static assets -- Prowlarr never needs them, and forwarding them to Byparr
# wastes a full Cloudflare-solve cycle per request.
SKIP_EXT = re.compile(
    r"\.(css|js|mjs|map|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|eot|mp4|webm)(\?|$)",
    re.IGNORECASE,
)

# Indexer "test" and "browse" endpoints in the cardigann definition all live
# under /cat/<Category>/<page>/. Real keyword searches go to /search/ or
# /sort-search/, so this regex isolates the test traffic.
CAT_PATH = re.compile(r"^/cat/", re.IGNORECASE)

# Some indexers keep the magnet off the listing rows: EZTV behind a "show
# links" form, the KickAss mirror on the torrent details page. Byparr's
# /v1 API takes no request headers, so a layout cookie that would reveal
# the magnets can't be sent. Instead the cardigann download field points
# at the page that carries the magnet and we return it bare, which the
# *arr apps accept directly.
MAGNET_RE = re.compile(r"magnet:\?[^\"'<\s]+")
MAGNET_PATHS_DEFAULT = re.compile(r"[?&]d=(?:&|$)")

# Fake rows -- one per major category family -- so every *arr app finds a
# matching row when filtering by its configured category set. 0 seeders
# ensures nothing ever grabs these synthetic entries, and the titles are
# obviously fake if they leak into a Prowlarr browse view.
_STUB_CATEGORIES = [
    (42, "Movies/HD"),
    (41, "TV/HD"),
    (22, "Audio/MP3"),
    (18, "Apps/PC"),
    (10, "Games/PC"),
    (36, "Books/EBook"),
    (40, "Other/Misc"),
]


def _build_stub_body():
    rows = []
    for cat_id, label in _STUB_CATEGORIES:
        rows.append(
            f'<tr>'
            f'<td class="coll-1 name">'
            f'<a href="/sub/{cat_id}/0/">{label}</a>'
            f'<a href="/torrent/{cat_id}/byparr-proxy-stub-{cat_id}/">'
            f'byparr-proxy stub - {label} healthy</a>'
            f'</td>'
            f'<td class="coll-2 seeds">0</td>'
            f'<td class="coll-3 leeches">0</td>'
            f'<td class="coll-date">now</td>'
            f'<td class="coll-4 size">1 KB</td>'
            f'<td class="coll-5 user">byparr-proxy</td>'
            f'</tr>'
        )
    html = (
        '<!DOCTYPE html><html><head><title>byparr-proxy stub</title></head>'
        '<body><table class="table-list"><tbody>'
        + ''.join(rows) +
        '</tbody></table></body></html>'
    )
    return html.encode("utf-8")


STUB_BODY = _build_stub_body()

# Root page for multi mode -- shows which prefixes are live, for browser
# smoke tests. Single mode has no index page: everything routes upstream.
INDEX_BODY = None
if not LEGACY:
    INDEX_BODY = (
        "<!DOCTYPE html><html><head><title>byparr-proxy</title></head><body>"
        "<h1>byparr-proxy</h1><ul>"
        + "".join(f'<li><a href="/{p}/">{p}</a> &rarr; {u}</li>'
                  for p, u in UPSTREAMS.items())
        + "</ul></body></html>"
    ).encode("utf-8")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s.%(msecs)03d %(levelname)-5s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("byparr-proxy")


def _site_env(name, prefix):
    # Per-prefix variable in multi mode (NAME_prefix), bare NAME in single
    # mode. dockerMan and friends pass unset variables as "", which the
    # callers treat as "use the default".
    return os.environ.get(name + ("" if prefix == "" else "_" + prefix), "")


def _magnet_paths(prefix):
    val = _site_env("MAGNET_PATHS", prefix)
    return re.compile(val) if val else MAGNET_PATHS_DEFAULT


def _stub_cat(prefix):
    return _site_env("STUB_CAT_PATHS", prefix).lower() not in ("0", "false", "no", "off")


def _extract_magnet(body):
    m = MAGNET_RE.search(body.decode("utf-8", errors="replace"))
    if not m:
        return None
    return unescape(m.group(0))


class _Pending:
    __slots__ = ("event", "status", "body", "error")

    def __init__(self):
        self.event = threading.Event()
        self.status = None
        self.body = None
        self.error = None


# Cache successful upstream responses for CACHE_TTL_S, keyed by (prefix,
# path). Sonarr/Radarr/Prowlarr poll the same endpoints on independent
# schedules; without this, each poll triggers a full Cloudflare solve and
# the bursts cause Byparr to 408 on queued requests.
_cache = {}          # (prefix, path) -> (expires_at_monotonic, status, body)
_inflight = {}       # (prefix, path) -> _Pending
_state_lock = threading.Lock()
# Byparr drives a real browser, so it effectively serializes. Holding this
# while calling Byparr keeps a burst from sharing a single maxTimeout window
# across N concurrent requests (which is what caused the 408s).
_byparr_lock = threading.Lock()


def _fetch_from_byparr(rid, target):
    """Call Byparr and return (status, body). Raises on failure."""
    payload = json.dumps({
        "cmd": "request.get",
        "url": target,
        "maxTimeout": TIMEOUT_MS,
    }).encode()
    req = urllib.request.Request(
        BYPARR,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_MS / 1000 + 15) as resp:
        data = json.loads(resp.read())
    if data.get("status") != "ok":
        raise RuntimeError(f"byparr returned: {data.get('message', 'unknown')}")
    sol = data["solution"]
    return sol.get("status", 200), sol["response"].encode("utf-8", errors="replace")


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body, content_type="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _proxy(self):
        rid = uuid.uuid4().hex[:6]
        client = self.address_string()
        method = self.command
        path = self.path
        start = time.monotonic()

        if LEGACY:
            # single mode: no prefix routing, the whole path goes upstream
            prefix, stripped = "", path
        else:
            parts = path.split("/", 2)
            prefix = parts[1] if len(parts) > 1 else ""
            if prefix == "":
                log.info("[%s] %s %s from %s -> 200 index page",
                         rid, method, path, client)
                self._send(200, INDEX_BODY)
                return
            if prefix not in UPSTREAMS:
                log.info("[%s] %s %s from %s -> 404 unknown prefix",
                         rid, method, path, client)
                self._send(404, b"unknown prefix")
                return
            stripped = "/" + (parts[2] if len(parts) > 2 else "")

        if SKIP_EXT.search(stripped):
            log.info("[%s] %s %s from %s -> 404 skipped (static asset)",
                     rid, method, path, client)
            self._send(404, b"")
            return

        if _stub_cat(prefix) and CAT_PATH.search(stripped):
            log.info("[%s] %s %s from %s -> 200 stubbed (cat path, bypassing byparr)",
                     rid, method, path, client)
            self._send(200, STUB_BODY)
            return

        status, body, source, byparr_elapsed = self._get(rid, prefix, stripped, method, client)
        if status is None:
            # error already sent
            return

        self._send(status, body)

        elapsed = time.monotonic() - start
        if source == "cache":
            log.info("[%s] %s %s <- %d in %.2fs (cache hit, %d bytes)",
                     rid, method, path, status, elapsed, len(body))
        elif source == "coalesced":
            log.info("[%s] %s %s <- %d in %.2fs (coalesced with in-flight, %d bytes)",
                     rid, method, path, status, elapsed, len(body))
        else:
            log.info("[%s] %s %s <- %d in %.2fs (byparr %.2fs, %d bytes)",
                     rid, method, path, status, elapsed,
                     byparr_elapsed, len(body))

    def _get(self, rid, prefix, path, method, client):
        """Return (status, body, source, byparr_elapsed) or (None, ...) on error."""
        key = (prefix, path)
        now = time.monotonic()

        with _state_lock:
            entry = _cache.get(key)
            if entry and entry[0] > now:
                age = CACHE_TTL_S - (entry[0] - now)
                log.info("[%s] %s /%s%s from %s -> cache hit (age %.0fs, ttl %ds)",
                         rid, method, prefix, path, client, age, CACHE_TTL_S)
                return entry[1], entry[2], "cache", 0.0

            pending = _inflight.get(key)
            if pending is not None:
                log.info("[%s] %s /%s%s from %s -> coalescing with in-flight request",
                         rid, method, prefix, path, client)
                owner = False
            else:
                pending = _Pending()
                _inflight[key] = pending
                owner = True

        if not owner:
            pending.event.wait(timeout=TIMEOUT_MS / 1000 + 30)
            if pending.error is not None:
                log.error("[%s] %s /%s%s <- coalesced request failed: %s",
                          rid, method, prefix, path, pending.error)
                self.send_error(502, f"byparr request failed: {pending.error}")
                return None, None, None, 0.0
            return pending.status, pending.body, "coalesced", 0.0

        target = UPSTREAMS[prefix] + path
        log.info("[%s] %s /%s%s from %s -> forwarding to byparr (%s)",
                 rid, method, prefix, path, client, target)
        byparr_start = time.monotonic()
        try:
            with _byparr_lock:
                status, body = _fetch_from_byparr(rid, target)
        except Exception as e:
            byparr_elapsed = time.monotonic() - byparr_start
            with _state_lock:
                _inflight.pop(key, None)
            pending.error = e
            pending.event.set()
            log.error("[%s] %s /%s%s <- byparr request failed after %.2fs: %s",
                      rid, method, prefix, path, byparr_elapsed, e)
            self.send_error(502, f"byparr request failed: {e}")
            return None, None, None, byparr_elapsed

        byparr_elapsed = time.monotonic() - byparr_start
        if _magnet_paths(prefix).search(path):
            magnet = _extract_magnet(body)
            if magnet:
                body = magnet.encode("utf-8")
                status = 200

        # Treat a body with no torrent rows as "empty" and skip caching it.
        # Empty pages happen when the search legitimately has no matches,
        # when Cloudflare served a challenge page instead of the real
        # content, or on transient upstream errors. None of these should
        # be pinned for an hour. A bare magnet body is stable for the
        # lifetime of the entry, and KAT-style mirrors mark their rows
        # with cellMainLink instead of /torrent/ links.
        has_results = (b"/torrent/" in body
                       or body.startswith(b"magnet:")
                       or b"cellMainLink" in body)
        with _state_lock:
            _inflight.pop(key, None)
            if 200 <= status < 300 and has_results:
                _cache[key] = (time.monotonic() + CACHE_TTL_S, status, body)
                log.info("[%s] %s /%s%s -> cached for %ds (status %d, %d bytes)",
                         rid, method, prefix, path, CACHE_TTL_S, status, len(body))
            elif 200 <= status < 300:
                log.info("[%s] %s /%s%s -> not cached (empty results, %d bytes)",
                         rid, method, prefix, path, len(body))
        pending.status = status
        pending.body = body
        pending.event.set()
        return status, body, "fresh", byparr_elapsed

    def do_GET(self):
        self._proxy()

    def do_HEAD(self):
        self._proxy()

    # Silence BaseHTTPRequestHandler's default per-request logging --
    # we emit our own structured lines from _proxy().
    def log_message(self, fmt, *args):
        pass

    def log_error(self, fmt, *args):
        pass


def main():
    mode = "single" if LEGACY else "multi"
    log.info("byparr-proxy starting (%s mode)", mode)
    log.info("  upstreams:  %s",
             ", ".join(f"{'/' if p == '' else p + '/'} -> {u}"
                       for p, u in UPSTREAMS.items()))
    for prefix in UPSTREAMS:
        label = "/" if prefix == "" else f"/{prefix}/"
        log.info("  %-12s stub /cat/: %s | magnet paths: %s",
                 label, "on" if _stub_cat(prefix) else "off",
                 _magnet_paths(prefix).pattern)
    log.info("  byparr:     %s", BYPARR)
    log.info("  timeout:    %d ms", TIMEOUT_MS)
    log.info("  port:       %d", PORT)
    log.info("  log level:  %s", LOG_LEVEL)
    log.info("  cache ttl:  %d s", CACHE_TTL_S)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("ready, listening on 0.0.0.0:%d", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutdown signal received, stopping")


if __name__ == "__main__":
    main()
