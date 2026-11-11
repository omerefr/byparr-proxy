# byparr-proxy

A single-file, zero-dependency HTTP passthrough that gets **1337x, EZTV, KickAss and other Cloudflare-protected indexers working again with Prowlarr/Jackett in 2026**, when FlareSolverr and Byparr alone are no longer enough. Run one instance per site, or one instance fronting many sites behind URL prefixes.

If you've been seeing this in Prowlarr's logs even with FlareSolverr or Byparr configured:

```
NzbDrone.Core.Http.CloudFlare.CloudFlareProtectionException:
  Unable to access 1337x.to, blocked by CloudFlare Protection.
```

...this is for you.

---

## The problem

By early 2026, Cloudflare's protection of sites like 1337x has tightened to the point where the entire **FlareSolverr/Byparr cookie-replay model is fundamentally broken**. The mechanism that breaks:

1. Prowlarr makes a direct HTTPS request to `https://1337x.to/cat/Movies/1/`.
2. Cloudflare returns a "Just a moment..." 403 challenge page.
3. Prowlarr detects the challenge and sends the URL to FlareSolverr/Byparr via its indexer-proxy mechanism.
4. Byparr fires up a real Firefox, solves the JavaScript challenge, and returns a `cf_clearance` cookie plus the actual page HTML.
5. **Prowlarr throws away the HTML** and replays the request to `https://1337x.to/cat/Movies/1/` with the cookie attached, using its own .NET `HttpClient`.
6. Cloudflare inspects the replay — different TLS/JA4 fingerprint than the one that solved the challenge, slightly different HTTP/2 settings, extra headers like `Accept-Encoding: gzip` — and reissues a fresh challenge.
7. Prowlarr sees the new challenge → throws `CloudFlareProtectionException`. Fail.

The cookie was valid. Byparr worked. The solver is not the problem. The problem is the **replay step (5)** — Prowlarr discarding the already-fetched body and re-fetching from a client Cloudflare can fingerprint.

Workarounds we tried that **did not** fix it:
- Updating Prowlarr to the latest version
- Updating Byparr to the latest version
- Disabling IPv6 on the host network adapter
- Patching the Cardigann YAML to add `Accept: */*` and matching `User-Agent` headers ([FlareSolverr issue #1672](https://github.com/FlareSolverr/FlareSolverr/issues/1672)'s suggested fix)
- Switching from FlareSolverr to Byparr
- Switching from Prowlarr to Jackett (Jackett does the same cookie replay and fails the same way)

The body returned by Byparr always contained the real torrent listings. Prowlarr just never used it.

## The fix

Stop the replay from ever happening. Put a tiny HTTP server between Prowlarr and the indexer that **uses Byparr's response body directly**:

```
┌──────────┐   plain HTTP    ┌──────────────┐   POST /v1   ┌────────┐   real browser   ┌─────────┐
│ Prowlarr │ ──────────────► │ byparr-proxy │ ───────────► │ Byparr │ ───────────────► │ 1337x   │
│          │ ◄────── 200 OK  │              │ ◄── 200 OK   │        │ ◄── solved HTML  │ (via CF)│
└──────────┘   real HTML     └──────────────┘   solution   └────────┘                  └─────────┘
```

Prowlarr's 1337x indexer is reconfigured to use `http://byparr-proxy:8888/` as its **Base URL**. Prowlarr never talks to Cloudflare directly. The Cloudflare detection in Prowlarr's `CloudFlareDetectionService` never fires, because Prowlarr only ever sees clean `200 OK` responses with real HTML in the body. There is no cookie replay because there is no second request to replay.

That's it. One Python file, no dependencies, reuses the Byparr you already have running.

## Quick start

Prerequisites:
- Docker + Docker Compose
- Prowlarr (or Jackett) running in the same Docker network
- Byparr running (`ghcr.io/thephaseless/byparr:latest`)

### 1. Add it to your existing stack

From the directory that already contains your stack's compose file, clone this repo:

```bash
git clone https://github.com/martin-van-coden/byparr-proxy.git
```

Append this service to the `services:` block in your existing compose file. No other changes needed if `byparr` is already in the same compose project/network — the proxy will reach it by service name.

```yaml
  byparr-proxy:
    build: ./byparr-proxy
    container_name: byparr-proxy
    restart: unless-stopped
    ports:
      - "8888:8888"
    environment:
      # Multi mode: one container, one URL prefix per indexer
      UPSTREAMS: "1337x=https://1337x.to,eztv=https://eztvx.to"
      BYPARR: http://byparr:8191/v1
      TIMEOUT_MS: "120000"
      PORT: "8888"
    depends_on:
      - byparr
```

Then start just the new service:

```bash
docker compose up -d byparr-proxy
```

Smoke-test from your browser on the Docker host: visit `http://localhost:8888/` — you'll see an index page listing every configured prefix — then `http://localhost:8888/1337x/cat/Movies/1/`. After 30s–2min (Byparr solving Cloudflare for the first time — can be slow under load), you should see the actual 1337x Movies page render, unstyled, because we 404 static assets to keep things fast. If Docker is running on another machine, use that machine's LAN IP instead of `localhost`.

Only need one site? Set `UPSTREAM: https://1337x.to` instead of `UPSTREAMS` — the legacy single-upstream mode serves that site at `/` with no prefix routing. `UPSTREAMS` takes precedence if both are set.

If you want to run this repo standalone instead, `cd byparr-proxy && docker compose up -d` starts both `byparr` and `byparr-proxy` using the included compose file.

### 2. Drop the Cardigann definition into Prowlarr

**a. Create the `Custom/` folder if it doesn't exist.** It often isn't there by default — Prowlarr only creates it once you've actually added a custom definition through some prior workflow:

```bash
docker exec prowlarr mkdir -p /config/Definitions/Custom
```

**The `Custom/` subdirectory is required** — Prowlarr (recent versions) ignores YAML files placed directly in `Definitions/`. Those are its own internal mirror of built-in definitions; user customs only get loaded from `Custom/`. If you skip this step or drop the YAML one folder up, the indexer will silently not appear in the add list.

**b. Copy `definitions/1337x-byparr.yml` into that folder:**

```bash
docker cp byparr-proxy/definitions/1337x-byparr.yml prowlarr:/config/Definitions/Custom/1337x-byparr.yml
```

Verify from inside the container that Prowlarr can see the file:
```bash
docker exec prowlarr sh -c "ls /config/Definitions/Custom/ && head -5 /config/Definitions/Custom/1337x-byparr.yml"
```
You should see `1337x-byparr.yml` listed and its first 5 lines (with `id: 1337x-byparr`).

**c. Restart Prowlarr:**

```bash
docker restart prowlarr
```

### 3. Add the indexer in Prowlarr

1. Open Prowlarr UI.
2. **Indexers → + Add Indexer**.
3. Search for **"1337x (via Byparr)"** in the list and click it.
4. Base URL is automatically `http://byparr-proxy:8888/1337x/` (the only entry in the YAML).
5. **Do not add a FlareSolverr tag.** That's the whole point — we don't want Prowlarr's indexer-proxy mechanism kicking in.
6. Click **Test**. First test typically takes 30s–2min while Byparr solves (sometimes longer if the server is under load). Should green-check.
7. **Save**.

If the indexer doesn't appear in the add list, see [Troubleshooting](#troubleshooting).

### ⚠️ A note on search latency

Each uncached search through this proxy takes 30 seconds to 2 minutes (Byparr is solving Cloudflare freshly). The timeouts that matter are all internal and not user-configurable in modern UIs:

- **`byparr-proxy` itself** — we set `TIMEOUT_MS=120000` (2 min) above. This is the only knob you control directly. Bump it higher if you regularly see `byparr request failed: timed out` in `docker logs byparr-proxy`.
- **Prowlarr's indexer HTTP timeout** — internal (~100s+), not exposed in the UI.
- **Radarr / Sonarr → Prowlarr** — internal, generous by default.

In practice you do **not** need to change anything in Radarr or Sonarr — they just wait. The user-facing symptom of a too-slow solve is a single failed RSS sync or manual search that retries on its own next cycle. If you see *consistent* timeouts (multiple failures in a row, indexer marked unhealthy), the real fix is reducing concurrent load on Byparr — not raising timeouts further. Lower Radarr/Sonarr's interactive search concurrency, or stagger RSS sync intervals.

## Adding more indexers (multi mode)

The recommended way to run multiple indexers is **multi mode**: one container, one port, one URL prefix per site. To add a site (e.g. YTS), append it to `UPSTREAMS` and restart the container — no new services, no new ports:

```yaml
    environment:
      UPSTREAMS: "1337x=https://1337x.to,eztv=https://eztvx.to,yts=https://yts.mx"
```

Each site is then reachable at `http://byparr-proxy:8888/<prefix>/...`, and `/` serves an index page listing every prefix.

For each site, create a Cardigann YAML in Prowlarr's `/config/Definitions/Custom/` directory based on the site's built-in definition from the [Prowlarr indexers repo](https://github.com/Prowlarr/Indexers), with three changes:
- Change `id:` to something unique (`yts-byparr`)
- Change `name:` to something distinguishable (`YTS (via Byparr)`)
- Replace the `links:` block with a single entry: `- http://byparr-proxy:8888/yts/`

Restart Prowlarr, then add the new indexer through the UI.

The original single-upstream mode is still supported for existing setups: run a separate service or container with `UPSTREAM: https://yts.mx` and its own `PORT`, and point the definition at that container. New setups should prefer multi mode — one container to watch instead of N.

## Configuration reference

The proxy reads these environment variables:

| Variable                  | Default                 | Description                                              |
| ------------------------- | ----------------------- | -------------------------------------------------------- |
| `UPSTREAMS`               | —                       | **Multi mode.** Comma-separated `prefix=base-url` pairs, e.g. `1337x=https://1337x.to,eztv=https://eztvx.to`. Requests are routed by their first path segment. Takes precedence over `UPSTREAM`. |
| `UPSTREAM`                | —                       | **Single (legacy) mode.** Base URL of the one indexer the proxy fronts, served at `/` with no prefix routing. Used only when `UPSTREAMS` is not set. One of the two is required. |
| `BYPARR`                  | `http://byparr:8191/v1` | Byparr's `/v1` endpoint, reachable from the container.   |
| `TIMEOUT_MS`              | `120000`                | Per-request timeout passed to Byparr (milliseconds). Bump higher if solves regularly exceed 2 minutes. |
| `PORT`                    | `8888`                  | Local port the proxy listens on.                         |
| `LOG_LEVEL`               | `INFO`                  | Python logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `CACHE_TTL_S`             | `3600`                  | TTL (seconds) for caching successful responses. Cache is in-memory only; restart clears it. |
| `STUB_CAT_PATHS[_<prefix>]` | `true`              | When on, `/cat/...` paths (used by indexer health-tests in Prowlarr/Sonarr/Radarr/Lidarr/Readarr) skip Byparr entirely and return a synthetic results page. In multi mode `STUB_CAT_PATHS_<prefix>` controls a single prefix. Set to `false` to route them through Byparr like everything else. |
| `MAGNET_PATHS[_<prefix>]` | `[?&]d=(?:&|$)`         | Regex of (prefix-stripped) paths whose fetched page is reduced to its bare `magnet:` URI, which the *arr apps accept as a download link directly. See [why](#why-the-cache-the-stub-and-the-magnet-conversion). |

### Why the cache, the stub, and the magnet conversion

Without these, the proxy was unusable under real *arr-stack load:

- **Caching with in-flight dedup.** Sonarr, Radarr, and Prowlarr each poll the same indexer endpoints on independent schedules. Without dedup, three near-simultaneous health checks all triggered three concurrent Byparr solves, exhausting its per-request timeout window and producing `408 Request Timeout`. The cache (1h default) absorbs the *spaced-out* duplicates; in-flight coalescing absorbs *concurrent* ones. Empty result pages (no `/torrent/` links) are deliberately **not** cached, so a transient blank doesn't get pinned for an hour. Calls to Byparr itself are serialized — Byparr drives a real browser, so it effectively serializes anyway, but doing it explicitly in the proxy avoids the timeout-stacking problem.
- **`/cat/...` stub.** Cloudflare's protection on 1337x's category browse pages is observably harder than on keyword search — when Byparr can't solve the category pages it returns 408 even though `https://1337x.to/search/<keyword>/1/` works fine. Since `/cat/...` is hit *only* by indexer health-checks (real searches go to `/search/...` or `/sort-search/...`), short-circuiting `/cat/...` to a synthetic response with one fake row per major category family keeps every *arr app's test green without touching real search traffic. The stub rows have 0 seeders and sentinel titles like `byparr-proxy stub - TV/HD healthy`, so they're harmless if they ever surface in a Prowlarr browse view.
- **Magnet path conversion.** Byparr's `/v1` API takes no request headers, so a layout cookie that would reveal magnet links on listing pages can't be sent. Some indexers keep the magnet off the listing rows anyway — EZTV hides it behind a "show links" form, and the KickAss mirror keeps it on the torrent details page. For those, the Cardigann `download:` field points at the page that carries the magnet, and `MAGNET_PATHS[_<prefix>]` tells the proxy which paths to reduce to the bare `magnet:` URI. The *arr apps accept a raw magnet URI as a download link, so no second fetch is needed. The default pattern `[?&]d=(?:&|$)` matches EZTV-style episode links (`...?d=`); e.g. `MAGNET_PATHS_kickass="^/.+\.html$"` matches KickAss details pages.

### Reading the logs

Every request gets a 6-character request ID so concurrent requests stay legible. The startup banner shows which features are active:

```
2026-05-23 19:49:17.863 INFO  byparr-proxy starting (multi mode)
2026-05-23 19:49:17.866 INFO    upstreams:  1337x/ -> https://1337x.to, eztv/ -> https://eztvx.to
2026-05-23 19:49:17.866 INFO    /1337x/      stub /cat/: on | magnet paths: [?&]d=(?:&|$)
2026-05-23 19:49:17.866 INFO    /eztv/       stub /cat/: on | magnet paths: [?&]d=(?:&|$)
2026-05-23 19:49:17.872 INFO    byparr:     http://byparr:8191/v1
2026-05-23 19:49:17.872 INFO    timeout:    120000 ms
2026-05-23 19:49:17.872 INFO    port:       8888
2026-05-23 19:49:17.872 INFO    log level:  INFO
2026-05-23 19:49:17.872 INFO    cache ttl:  3600 s
```

Per-request log lines:

```
# Fresh fetch via Byparr, cached on success
... INFO  [a1b2c3] GET /1337x/search/shrek2001/1/ from 172.18.0.7 -> forwarding to byparr (https://1337x.to/search/shrek2001/1/)
... INFO  [a1b2c3] GET /1337x/search/shrek2001/1/ -> cached for 3600s (status 200, 26188 bytes)
... INFO  [a1b2c3] GET /1337x/search/shrek2001/1/ <- 200 in 23.11s (byparr 23.09s, 26188 bytes)

# Same path again within the TTL -- served from cache, no byparr roundtrip
... INFO  [b2c3d4] GET /1337x/search/shrek2001/1/ from 172.18.0.7 -> cache hit (age 142s, ttl 3600s)
... INFO  [b2c3d4] GET /1337x/search/shrek2001/1/ <- 200 in 0.01s (cache hit, 26188 bytes)

# Two clients hit the same path concurrently -- second one waits on the first
... INFO  [c3d4e5] GET /1337x/search/foo/1/ from 172.18.0.7 -> forwarding to byparr (https://1337x.to/search/foo/1/)
... INFO  [d4e5f6] GET /1337x/search/foo/1/ from 172.18.0.8 -> coalescing with in-flight request
... INFO  [c3d4e5] GET /1337x/search/foo/1/ <- 200 in 25.13s (byparr 25.11s, 18204 bytes)
... INFO  [d4e5f6] GET /1337x/search/foo/1/ <- 200 in 25.13s (coalesced with in-flight, 18204 bytes)

# /cat/... paths short-circuited by the stub
... INFO  [e5f6a7] GET /1337x/cat/TV/1/ from 172.18.0.10 -> 200 stubbed (cat path, bypassing byparr)

# Successful fetch but no torrent rows in the body -- not cached, so the next attempt retries
... INFO  [f6a7b8] GET /1337x/search/somequery/1/ -> not cached (empty results, 870 bytes)

# Static asset we never bother forwarding
... INFO  [a7b8c9] GET /1337x/css/style.css from 172.18.0.7 -> 404 skipped (static asset)

# Unknown prefix in multi mode
... INFO  [c9d0e1] GET /nosuchsite/ from 172.18.0.7 -> 404 unknown prefix

# Hard failure
... ERROR [b8c9d0] GET /1337x/cat/Movies/2/ <- byparr request failed after 60.52s: HTTP Error 408: Request Timeout
```

What to look for when something's wrong:
- `not cached (empty results, ...)` on a search that should have hits: Byparr returned a stripped/skeleton page or a CF challenge fragment. Try again — the empty page is **not** cached, so retries actually retry.
- Long `byparr` times (>60s) on every request: Byparr is overloaded or being challenged hard. Reduce concurrent searches.
- `byparr returned non-ok`: Byparr itself failed to solve. Check `docker logs byparr`.
- `HTTP Error 408: Request Timeout`: Byparr's internal solve timeout fired. Pull a fresh `byparr:latest`, give it `shm_size: 1g` and `mem_limit: 2g`, and check its own logs for `TargetClosedError` (Chromium crash, usually `/dev/shm` exhaustion).
- Bytes < 5000 on a 200 response that *should* have results: Byparr may be returning a small error/challenge page instead of real content. The empty-results detector should have logged `not cached` for this.

## Limitations

- **GET and HEAD only.** Indexer search/listing is GET; the proxy does not currently forward POST bodies. Most Cardigann definitions use GET, so this is rarely an issue.
- **The actual `.torrent` file is usually not fetched via the proxy.** The Cardigann YAML's `download:` section gets a magnet URI or an `itorrents.org` URL, both of which Prowlarr fetches directly and which are not Cloudflare-protected. (Exception: when `MAGNET_PATHS` matches, the proxy returns the bare magnet URI itself — see [why](#why-the-cache-the-stub-and-the-magnet-conversion).)
- **The proxy returns Byparr's HTML body verbatim.** It does not rewrite internal links. The Cardigann definition only cares about path-based selectors, so this works fine — but don't try to use the proxy as a general-purpose browser front-end.
- **First request to a domain is slow** — typically 30s to 2 minutes while Byparr solves, occasionally longer. Subsequent requests within the cf_clearance lifetime are usually faster, but Byparr's session reuse is best-effort.
- **Static assets (.css/.js/.png/.svg/etc.) get an instant 404** from the proxy by design. Prowlarr never requests them; this just keeps browser smoke-tests from triggering pointless Cloudflare solves.

## Troubleshooting

**"The new indexer doesn't appear in Prowlarr's add list after restart"**

Check that the file is actually in the `Custom/` subdirectory and readable inside the container:

```bash
docker exec prowlarr sh -c "ls /config/Definitions/Custom/ && head -5 /config/Definitions/Custom/1337x-byparr.yml"
```

You should see `1337x-byparr.yml` listed, with the top of the file showing `id: 1337x-byparr`. If it's not in `Custom/`, move it there — Prowlarr ignores YAMLs placed directly in `Definitions/`.

**"Test fails with `byparr request failed: Request Timeout`"**

The container can't reach Byparr. Confirm:
1. Both containers are on the same Docker network.
2. Byparr is running: `docker ps | grep byparr`.
3. From inside the proxy container, Byparr is reachable: `docker exec byparr-proxy python -c "import urllib.request; print(urllib.request.urlopen('http://byparr:8191/health').read())"`.

**"Test fails with `byparr returned: Cloudflare challenge not detected`" or similar**

Byparr itself is failing. This means the indexer's Cloudflare config is currently beating Byparr too — not something this proxy can fix. Check the Byparr issue tracker and consider whether the indexer has an `.onion` mirror you can use via a Tor proxy instead.

**"Search works but Prowlarr says 0 results"**

The Cardigann YAML's selectors didn't match anything. The site may have changed its HTML. Compare the current site structure against the `fields:` block in the YAML. Update the selectors, or pull the latest definition from the [Prowlarr indexers repo](https://github.com/Prowlarr/Indexers) as a starting point.

## Why this works when other workarounds don't

| Approach                                   | Outcome                                                            |
| ------------------------------------------ | ------------------------------------------------------------------ |
| FlareSolverr alone                         | Cookie replay caught by Cloudflare → 403                           |
| Byparr alone (drop-in for FlareSolverr)    | Same cookie replay path → 403                                       |
| Add `Accept: */*` headers to Cardigann YAML | Some sites yes, 1337x no — Cloudflare uses more than just headers   |
| Switch to Jackett                          | Same cookie replay architecture → same failure                      |
| Disable IPv6                               | Only helps when CF binds cookies to v6 but client replays over v4   |
| **byparr-proxy (this project)**            | **No replay path exists. Cloudflare detection in Prowlarr never fires.** |

## Acknowledgements

- [guyg2232/byparr-proxy](https://github.com/guyg2232/byparr-proxy) — the original project this fork extends with multi-upstream routing, per-prefix configuration, and magnet path conversion.
- [Byparr](https://github.com/ThePhaseless/Byparr) — does the actual Cloudflare-solving with a real browser. This project is just a wrapper.
- [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) — the project Byparr forked from. The [issue #1672 discussion](https://github.com/FlareSolverr/FlareSolverr/issues/1672) was where the root-cause analysis started.
- [Prowlarr](https://github.com/Prowlarr/Prowlarr) and the [Prowlarr Indexers repo](https://github.com/Prowlarr/Indexers) — the Cardigann definition for 1337x is adapted from there.
