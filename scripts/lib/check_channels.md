# check_channels.py — M3U channel health checker

Finds out which channels in a Kodi M3U playlist are **dead, blocked, mislabelled
or just need a fix attribute**, in bulk, from a laptop — with no Kodi box.

## No installation

Pure **Python 3.8+ standard library**. No `pip`, no virtualenv, no third-party
imports, no package structure. Run it straight from the repo:
`python3 tools/check_channels.py playlist.m3u`

`ffprobe` is used *if it happens to be installed*; the tool degrades to pure
HTTP when it is not. Nothing is ever installed, and the input is never written.

## Usage

`python3 tools/check_channels.py SOURCE [flags]`, where SOURCE is a local
`.m3u` path or an `http(s)` URL to fetch.

| Flag | Default | Meaning |
|---|---|---|
| `--user-agent UA` | browser Chrome UA | UA for playlist fetches and probes; many CDNs 403 python-urllib |
| `--timeout SEC` | `10.0` | per-request socket timeout |
| `--concurrency N` | `8` | channels probed in parallel |
| `--limit N` | no limit | probe at most N channels (after filtering) |
| `--include REGEX` | — | only channels whose name/url/group matches (case-insensitive) |
| `--exclude REGEX` | — | skip channels whose name/url/group matches |
| `--json PATH` | — | write machine-readable results |
| `--fail-under PCT` | — | exit `1` if the OK percentage is below PCT (for CI) |
| `--fix OUT.m3u` | — | emit an annotated copy with M3U-level remedies (see below) |
| `--apply-redirects` | off | with `--fix`, rewrite redirected channels to their final URL |
| `--no-ffprobe` | ffprobe on | pure-HTTP mode even if ffprobe is installed |
| `--dry-run` / `--confirm` | `--dry-run` | `--fix` prints a diff; `--confirm` writes the file |
| `-v`, `--verbose` | off | show classification reasoning in the TYPE column |

Exit codes: `0` ok · `1` below `--fail-under` · `2` bad input — an unreadable
path, or a file that is not a parseable M3U playlist (also a refused write).

## Verdict taxonomy

Every channel gets exactly one verdict.

| Verdict | Meaning |
|---|---|
| `OK` | responded and looks like what the URL claims |
| `DEAD-404` | HTTP 404, or a redirect chain ending in 404 |
| `DEAD-403` | HTTP 401/403 — geo-blocked, auth-walled or referer-gated |
| `DEAD-DNS` | hostname does not resolve |
| `DEAD-TLS` | TLS handshake or certificate verification failed |
| `DEAD-CONN` | TCP refused/reset/unreachable |
| `TIMEOUT` | no answer within `--timeout` |
| `EMPTY` | HTTP 200 but zero bytes of body |
| `NOT-HLS` | URL claims HLS but the body is not an M3U playlist |
| `REDIRECTED` | answered, but the final URL differs from the playlist URL |
| `TRUNCATED` | server hung up mid-body — connection closed before the full response |
| `UNKNOWN-ERROR` | failed for a reason we do not classify |
| `NO-URL` | `#EXTINF` entry has no URL line at all |

HLS is checked structurally, not just for a 200: `#EXTM3U`, `#EXTINF` (media)
or `#EXT-X-STREAM-INF` (master), `#EXT-X-TARGETDURATION`, and the presence of
`#EXT-X-ENDLIST` — which is what distinguishes a live channel from VOD. For a
master the first variant is probed too. An HTTP 200 that returns an HTML error page
is therefore *not* reported as `OK` —
for HLS that follows from the structural check, and for every other transport from
an explicit body check: a response whose `Content-Type` is `text/html`,
`application/xhtml+xml`, `text/plain`, `application/json` or `application/javascript`,
or whose body starts with `<!doctype`/`<html`/`<?xml`, is reported as `NOT-HLS`. A real
manifest is never caught by that rule: a body starting with `#EXTM3U` or containing
`<MPD` is accepted whatever the `Content-Type` claims. Without it, a geo-wall, a
paywall or a soft-404 that answers 200 with a login page was scored as a working
channel.

A truncated response is routine on flaky providers and is reported as
`TRUNCATED` on that one channel — no single channel can abort a run.

A body that stops short of the `Content-Length` the server declared is reported as
`TRUNCATED`. A bounded `read()` returns whatever arrived without raising, so a
server that hangs up mid-body is otherwise indistinguishable from a complete
playlist. Chunked responses declare no length and are unaffected.

**Kodi's `|name=value` tails are not part of the URL.** Playlists commonly carry
`|User-Agent=…`, `|Referer=…` and friends. That tail is Kodi's own option syntax:
sent to the server it becomes part of the path, the request 404s, and a channel that
plays perfectly in Kodi would be reported dead. The tail is therefore stripped
before classification and before every HTTP probe, and `--fix` re-attaches it to
whatever it writes back — including a rewritten redirect target.

Stream types are derived per channel: `hls-master`, `hls-media`, `dash`,
`mpegts`, `mp4`, `unknown`, with `+catchup` appended when catchup attributes
are present. A `#KODIPROP:mimetype=` outranks the URL extension, because that
is the attribute Kodi is told to trust.

## Examples

```

python3 tools/check_channels.py playlist.m3u
python3 tools/check_channels.py https://iptv-org.github.io/iptv/countries/ad.m3u \
    --limit 40 --json results.json --fail-under 50
python3 tools/check_channels.py p.m3u --include "News" --exclude "Geo-blocked"
```

The summary reports `working (OK)`, then `NOT confirmed OK` (everything that is not
`OK`: dead, redirected, truncated, not-HLS, unknown), then `of which dead` as a
sub-line. The older `dead / unreachable` line alone was misleading: a run in which
half the channels are truncated reported almost no dead.

## `--fix` remediation

`--fix` reads the **original** text and writes an annotated copy, preserving
line order, comments and layout. It only applies remedies that are provably
M3U-level:

- `NOT-HLS` on a `.m3u8` URL that actually serves `video/mp2t` → adds
  `#KODIPROP:mimetype=video/mpeg`.
- an extensionless URL that answers but that ffprobe could not classify →
  adds `mimetype=video/mp4` or `video/mpeg`, **only** when the observed
  Content-Type or ffprobe `format_name` justifies it.
- `REDIRECTED` → rewrites the URL to the redirect target, only behind
  `--apply-redirects`, and logs it.

Every modification is logged as `CHANNEL | ATTRIBUTE | OLD | NEW | WHY`.
Channels that get no remedy — dead ones included — also get a log line saying
why, so silence is never mistaken for a fix.

**Dead streams are never "fixed".** A 404, 403, DNS failure, refused connection
or timeout is a provider problem, not an M3U problem — no attribute makes a dead
stream play, so the tool leaves those entries alone and says so in the log
rather than pretending otherwise.

Dry-run is the default, so `--fix` prints a unified diff and writes nothing;
add `--confirm` to write the file.

The tool refuses to write the output over the input playlist, and performs only
GET requests — there is no network write anywhere in it. `--fix` preserves the
playlist byte for byte: lines keep their own terminator (CRLF stays CRLF,
mixed endings stay mixed) and only edited lines change, so a run that finds
nothing to fix yields a **byte-identical** file, verifiable with `cmp` or
`shasum`. Only CR, LF and CRLF count as line breaks, so an unusual byte inside
a URL is never turned into a new line.

`ffprobe` is enrichment, never a gate: the verdict is already decided by the
HTTP probe, so an ffprobe failure or timeout is recorded neutrally and changes
nothing. A live HLS stream takes ffprobe ~9s to settle, so use `--no-ffprobe`
for fast bulk runs.
