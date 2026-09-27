# Verifying streams and building a playlist that actually plays

The playlists in this repository are a **catalogue**: every publicly available
stream iptv-org knows about, including links that are dead, expired, restricted
or gated behind a browser User-Agent. A player that loads
`https://iptv-org.github.io/iptv/index.country.m3u` will therefore show a long
list of channels that do not play.

`scripts/verify_streams.py` probes every entry and writes playlists that reflect
reality **from the machine that runs it**:

| output | contents |
|---|---|
| `playlists/verified.m3u` | only streams that answered and look like what the URL claims |
| `playlists/blocked.m3u` | everything else, each entry labelled with its reason |
| `playlists/verify-report.md` | counts per verdict, worst hosts, and what was repaired |

```bash
python3 scripts/verify_streams.py                     # every list in streams/
python3 scripts/verify_streams.py streams/es.m3u      # one country
python3 scripts/verify_streams.py --apply-redirects  # also pin redirect targets
python3 scripts/verify_streams.py --no-ua-probe      # faster, repairs nothing
```

Python 3.8+ standard library only. `ffprobe` is used when present and is never
required. Run it from the machine whose network you want the result for — the
answer is location-dependent, and a result produced in one country is not valid
in another.

Load the result in Kodi via **IPTV Simple Client → M3U playlist URL**, pointing at
`playlists/verified.m3u`.

## What it repairs

**User-Agent and Referer gating.** A provider that answers `403` to a generic
client but serves a browser is a playlist defect, not a dead stream. Every
`401`/`403` is re-probed with alternative User-Agents (Chrome, Safari/iOS, VLC,
Kodi, ffmpeg) and, when one of them works, the winning value is written into the
entry as `#EXTVLCOPT:http-user-agent=…`. The report counts how many channels
were recovered this way.

**Redirecting CDNs.** A `302` to a real stream is followed and verified. With
`--apply-redirects` the redirect target is pinned into the playlist, preserving
any `|name=value` option tail Kodi expects.

**Kodi's `|option` tails.** A URL carrying `|User-Agent=…` is Kodi option syntax,
not part of the address. It is stripped before probing, so those channels are
classified and liveness-checked correctly instead of being reported dead.

## What it cannot repair

**Geo-blocking.** The decision is made by the broadcaster's CDN from the source IP
address, before any request header is read. Editing an M3U does not change where
the packets come from. This is not a limitation of the script — it is how the
block works, and this repository's own
[geo-blocking documentation](./geo-blocking.md) says so: *"The easiest way to make
sure the stream works outside your country is to use services like
check-host.net or a VPN."*

The script therefore does not pretend. Geo-blocked channels are counted
separately in the report and moved into `playlists/blocked.m3u`, so the main
playlist is never polluted with entries that cannot play from your network.

To recover them, route traffic through a proxy or VPN in the relevant country and
run the script again:

- **Kodi:** *Settings → System → Internet access → General → "Use proxy servers"*
  (HTTP type, host and port). Kodi's own traffic, including PVR streams, then
  goes through it.
- **Network-wide:** a VPN on the box or on the router, which also covers EPG
  downloads and any other add-on.

Because the result is location-dependent, re-run the script from behind the
proxy/VPN and the recovered channels move into `playlists/verified.m3u`
automatically.

## Verdict meanings

| verdict | meaning | repairable from the playlist? |
|---|---|---|
| `OK` | answered and looks like the URL claims | — |
| `REDIRECTED` | answered, but via a redirect | yes, `--apply-redirects` |
| `DEAD-404` | the link no longer exists | no — provider retired it |
| `DEAD-403` | rejected: geo-block, auth or referer gate | sometimes, via User-Agent |
| `DEAD-DNS` | hostname does not resolve | no — domain is gone |
| `DEAD-TLS` | TLS handshake failed | no — the server is broken |
| `DEAD-CONN` | connection refused or reset | no |
| `TIMEOUT` | no answer within the timeout | retry later; often transient |
| `NOT-HLS` | answers, but not with the claimed format | sometimes, a mimetype fixes it |
| `TRUNCATED` | hung up mid-body | usually transient |
| `EMPTY` | 200 with zero bytes | no |
| `NO-URL` | `#EXTINF` with no URL line | no — malformed source entry |

A verdict is a sample, not a measurement: flaky providers produce different
results between runs. Re-run before concluding a channel is permanently gone.

## Implementation

`scripts/lib/check_channels.py` does the probing and the verdict taxonomy; see
[its documentation](./../scripts/lib/check_channels.md). It is vendored here so
that this repository stays self-contained and the script runs without installing
anything.
