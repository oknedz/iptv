#!/usr/bin/env python3
"""Verify every stream in this repository and emit playlists that actually play.

The published playlists in this repo are *catalogues*: they list every publicly
available stream iptv-org knows about, including links that are dead, expired,
geo-blocked or gated behind a User-Agent check.  A player that loads one of
them therefore shows a long list of channels that do not work.

This script probes every entry and splits the catalogue into playlists that
reflect reality from *the machine that runs it*:

  playlists/verified.m3u   streams that answered and look like what they claim
  playlists/blocked.m3u    everything else, each labelled with the reason
  playlists/verify-report.md   human-readable breakdown

Two failure classes can be repaired from the playlist side, and this script
does repair them:

  * User-Agent / Referer gating - a provider that answers 403 to a generic
    client but serves a browser is a playlist defect, not a dead stream.  Each
    401/403 is re-probed with alternative User-Agents and, when one works, the
    winning value is written into the entry as #EXTVLCOPT:http-user-agent.
  * Redirecting CDNs - followed and, optionally, pinned with --apply-redirects
    while preserving any |option tail.

One failure class CANNOT be repaired from a playlist, and this script will not
pretend otherwise:

  * Geo-blocking.  The decision is made by the broadcaster's CDN from the
    source IP address, before any request header is read.  Editing an M3U does
    not change where the packets come from.  Upstream says the same in
    docs/geo-blocking.md: "The easiest way to make sure the stream works
    outside your country is to use services like check-host.net or a VPN."
    Those channels are separated into playlists/blocked.m3u so the main
    playlist is not polluted with entries that cannot play, and are counted
    in the report so you know exactly how many a VPN would recover.

Usage
-----
    python3 scripts/verify_streams.py                      # all of streams/
    python3 scripts/verify_streams.py streams/es.m3u ...  # specific lists
    python3 scripts/verify_streams.py --no-ua-probe       # faster, less repair
    python3 scripts/verify_streams.py --apply-redirects   # pin redirect targets

Requires only Python 3.8+ (standard library).  `ffprobe` is used if present and
is never required.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "lib"))

import check_channels as cc  # noqa: E402  (path set above)

DEFAULT_INPUT = os.path.join(REPO, "streams")
OUT_DIR = os.path.join(REPO, "playlists")

# Ordered by how likely a provider is to accept them.  The default checker UA
# is tried first by the main probe; these are only used for channels that came
# back 401/403.
FALLBACK_USER_AGENTS: Tuple[Tuple[str, str], ...] = (
    ("Chrome/Windows", cc.DEFAULT_UA),
    ("Safari/iOS", "Mozilla/5.5 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"),
    ("VLC", "VLC/3.0.20 LibVLC/3.0.20"),
    ("Kodi", "Kodi/22.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/119.0.0.0 Safari/537.36"),
    ("ffmpeg", "Lavf/60.16.100"),
)

# Verdicts that mean "this is not a transport problem, the provider said no".
AUTH_VERDICTS = ("DEAD-403",)
# Verdicts that are conclusively a dead link - nothing in a playlist fixes these.
DEAD_VERDICTS = ("DEAD-404", "DEAD-DNS", "DEAD-TLS", "DEAD-CONN")
# Verdict that usually means a geographic restriction rather than a defect.
GEO_HINT_BODIES = ("geofence", "not available in your region", "not available in your country",
                   "geo blocked", "geo-blocked", "outside your region")

PLAYABLE = ("OK", "REDIRECTED")


def build_config(args: argparse.Namespace) -> "cc.Config":
    parser = cc.build_parser()
    # check_channels.parse_args() needs its own `source` positional; the config
    # it returns is what we reuse, the source is never opened here.
    argv: List[str] = ["--timeout", str(args.timeout), "--concurrency",
                      str(args.concurrency), "unused-source.m3u"]
    if args.no_ffprobe:
        argv.append("--no-ffprobe")
    namespace, config = cc.parse_args(argv)
    del parser, namespace
    return config


def collect_inputs(paths: Sequence[str]) -> List[str]:
    files: List[str] = []
    for path in paths:
        if os.path.isdir(path):
            files.extend(
                os.path.join(path, name)
                for name in sorted(os.listdir(path))
                if name.endswith(".m3u") or name.endswith(".m3u8")
            )
        else:
            files.append(path)
    return files


def read_playlist_lines(path: str) -> List[str]:
    if path.startswith("http://") or path.startswith("https://"):
        text = cc.fetch_source(path, timeout=30.0, user_agent=cc.DEFAULT_UA)
    else:
        with open(path, "r", encoding="utf-8", errors="surrogateescape") as handle:
            text = handle.read()
    return cc.split_lines_keepends(text)


def entry_lines(lines: Sequence[str], channel: "cc.Channel") -> List[str]:
    """Return the verbatim source lines belonging to ``channel``.

    Preserving the original bytes (logo attribute, group-title, comment lines
    and the exact line terminator) is what lets the emitted playlists stay
    byte-identical to their source entries apart from the repair we apply.
    """
    start = channel.line_extinf
    end = channel.line_url if channel.line_url >= 0 else channel.line_last
    return list(lines[start:end + 1])


def try_user_agents(channel: "cc.Channel", cfg: "cc.Config",
                     reasons: Sequence[str]) -> Tuple[bool, str, str]:
    """Re-probe a rejected channel with other User-Agents.

    Returns ``(unblocked, user_agent, note)``.  Only a verdict change counts:
    a channel that is 403 for one UA and 200 for another is a playlist defect
    we can repair, and the winning UA goes into the entry.
    """
    base, _ = cc.split_kodi_options(channel.url)
    for label, ua in FALLBACK_USER_AGENTS:
        if ua == cfg.user_agent:
            continue
        probe_cfg = cc.Config(**{**vars(cfg), "user_agent": ua, "use_ffprobe": False})
        try:
            if channel.transport.startswith("hls"):
                verdict, detail, _info = cc.probe_hls(base, channel, probe_cfg)
            else:
                verdict, detail, _info = cc.probe_range_get(base, probe_cfg)
        except Exception as exc:  # a probe crash must not kill the run
            continue
        if verdict in PLAYABLE:
            return True, ua, f"403 for the default agent, 200 for {label}"
    return False, "", ""


def looks_geo_blocked(result: "cc.ProbeResult") -> bool:
    haystack = (result.detail or "").lower()
    return any(hint in haystack for hint in GEO_HINT_BODIES)


def render_playlist(header: str, entries: Iterable[Sequence[str]]) -> str:
    out = [header, ""]
    for entry in entries:
        out.extend(entry)
    return "\n".join(out) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inputs", nargs="*", default=[DEFAULT_INPUT],
                        help="M3U files or directories (default: streams/)")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--concurrency", type=int, default=24)
    parser.add_argument("--no-ffprobe", action="store_true")
    parser.add_argument("--no-ua-probe", action="store_true",
                        help="skip the second pass that tries to unblock 403s")
    parser.add_argument("--apply-redirects", action="store_true",
                        help="pin redirect targets, keeping any |option tail")
    parser.add_argument("--out-dir", default=OUT_DIR)
    args = parser.parse_args(argv)

    cfg = build_config(args)
    os.makedirs(args.out_dir, exist_ok=True)

    channels: List["cc.Channel"] = []
    all_lines: List[str] = []
    line_offset = 0
    seen_urls: Dict[str, int] = {}

    for path in collect_inputs(args.inputs):
        lines = read_playlist_lines(path)
        parsed, _unparseable = cc.parse_m3u("".join(lines))
        for channel in parsed:
            channel.index = len(channels)
            channel.line_extinf += line_offset
            channel.line_url += line_offset
            if channel.url in seen_urls:
                channel.duplicate_of = seen_urls[channel.url]
            else:
                seen_urls[channel.url] = channel.index
            channels.append(channel)
        all_lines.extend(lines)
        line_offset += len(lines)

    print(f"parsed {len(channels)} channel(s) from {len(collect_inputs(args.inputs))} file(s)",
          file=sys.stderr)

    results = cc.probe_all(channels, cfg)
    by_index = {result.index: result for result in results}

    unblocked = 0
    if not args.no_ua_probe:
        blocked_idx = [c.index for c in channels
                       if by_index.get(c.index) and by_index[c.index].verdict in AUTH_VERDICTS]
        print(f"re-probing {len(blocked_idx)} rejected channel(s) with other user-agents",
              file=sys.stderr)
        for index in blocked_idx:
            channel = channels[index]
            ok, ua, note = try_user_agents(channel, cfg, GEO_HINT_BODIES)
            if not ok:
                continue
            unblocked += 1
            result = by_index[index]
            result.verdict = "OK"
            result.detail = f"unblocked by user-agent: {note}"
            channel.ua_fix = ua

    verified: List[List[str]] = []
    blocked: List[List[str]] = []
    reasons: Counter = Counter()
    hosts: Dict[str, Counter] = defaultdict(Counter)
    geo = 0
    dead = 0
    repaired = 0

    for channel in channels:
        result = by_index.get(channel.index)
        if result is None:
            continue
        verdict = result.verdict
        reasons[verdict] += 1
        host = cc.urlparse(cc.split_kodi_options(channel.url)[0]).netloc if channel.url else "?"
        hosts[host][verdict] += 1

        entry = entry_lines(all_lines, channel)
        if getattr(channel, "ua_fix", ""):
            repaired += 1
            name = channel.name
            header = entry[0]
            if header.rstrip().endswith("\r\n"):
                body, term = header[:-2], "\r\n"
            elif header.rstrip().endswith("\n"):
                body, term = header[:-1], "\n"
            else:
                body, term = header, ""
            entry = [body + term, f"#EXTVLCOPT:http-user-agent={channel.ua_fix}{term}"] + entry[1:]

        if verdict in PLAYABLE and args.apply_redirects and result.final_url and result.redirected:
            base, tail = cc.split_kodi_options(channel.url)
            if result.final_url != base:
                entry[-1] = result.final_url + tail + ("\n" if entry[-1].endswith("\n") else "")
                repaired += 1

        if verdict in PLAYABLE:
            verified.append(entry)
        else:
            if looks_geo_blocked(result):
                geo += 1
            if verdict in DEAD_VERDICTS:
                dead += 1
            label = f"[{verdict}]"
            if channel.name.endswith(label) or label in channel.name:
                blocked.append(entry)
            else:
                header = entry[0]
                if channel.name and not channel.name.endswith(label):
                    updated = header.replace(channel.name, f"{channel.name} {label}", 1)
                    entry[0] = updated
                blocked.append(entry)

    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    total = len(channels)
    verified_path = os.path.join(args.out_dir, "verified.m3u")
    blocked_path = os.path.join(args.out_dir, "blocked.m3u")
    report_path = os.path.join(args.out_dir, "verify-report.md")

    with open(verified_path, "w", encoding="utf-8") as handle:
        handle.write(render_playlist(f"#EXTM3U\n# Verified {stamp} from {total} catalogue entries",
                                     verified))
    with open(blocked_path, "w", encoding="utf-8") as handle:
        handle.write(render_playlist(f"#EXTM3U\n# Not playable from here, {stamp} "
                                     f"({geo} geo-blocked, {dead} dead links)", blocked))

    playable = len(verified)
    lines = [
        f"# Stream verification report",
        "",
        f"Generated {stamp} from {total} catalogue entries.",
        "",
        f"- **Playable: {playable}/{total} ({100.0 * playable / max(total, 1):.1f}%)**",
        f"- Geo-blocked: {geo} (needs a VPN or a proxy, not a playlist edit)",
        f"- Dead links: {dead} (retired token, wrong path, dead host, refused)",
        f"- Repaired while verifying: {unblocked} unblocked by User-Agent, {repaired} entries rewritten",
        "",
        "## Verdicts",
        "",
        "| verdict | count | meaning |",
        "|---|---|---|",
    ]
    for verdict, count in reasons.most_common():
        lines.append(f"| `{verdict}` | {count} | {cc.VERDICT_HELP.get(verdict, '')} |")
    lines += ["", "## Worst hosts", "", "| host | blocked | dominant reason |", "|---|---|---|"]
    for host, counts in sorted(hosts.items(), key=lambda kv: -sum(kv[1].values()))[:20]:
        if sum(counts.values()) < 3:
            continue
        dominant, _n = counts.most_common(1)[0]
        lines.append(f"| `{host}` | {sum(counts.values())} | `{dominant}` |")
    lines += [
        "",
        "## How to use the result",
        "",
        "Load `playlists/verified.m3u` in Kodi (IPTV Simple Client -> M3U playlist URL).",
        "It contains only streams that answered from this machine at the time above.",
        "",
        "Channels in `playlists/blocked.m3u` labelled `DEAD-403` with a geo-blocked body",
        "cannot play from this network no matter what the playlist says. To recover them:",
        "route Kodi through a proxy/VPN in the relevant country, then re-run this script:",
        "",
        "    python3 scripts/verify_streams.py",
        "",
    ]
    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

    print(f"playable {playable}/{total}  geo-blocked {geo}  dead {dead}  "
          f"ua-unblocked {unblocked}", file=sys.stderr)
    print(f"wrote {verified_path}\n      {blocked_path}\n      {report_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
