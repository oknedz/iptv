#!/usr/bin/env python3
"""check_channels.py - zero-dependency health checker for Kodi M3U playlists.

WHAT IT DOES
    Takes a local .m3u file or an http(s) URL, parses it the way a real
    IPTV add-on sees it, probes every channel over plain HTTP(S) and
    ffprobe (both optional), and prints one verdict per channel:

        OK  DEAD-404  DEAD-403  DEAD-DNS  DEAD-TLS  DEAD-CONN  TIMEOUT
        EMPTY  NOT-HLS  REDIRECTED  UNKNOWN-ERROR  NO-URL

    It can also emit an annotated copy of the playlist with only the
    fixes that are provably M3U-level (--fix), so a Kodi box gets a
    correct mimetype or a followed redirect instead of a black screen.

WHY IT EXISTS
    Kodi's PVR IPTV Simple client shows a channel as playable when the
    playlist says it is, so a dead or mislabelled stream is
    indistinguishable from a working one until you click it.  This tool
    moves that failure forward, in bulk, on a laptop.

NO INSTALLATION REQUIRED
    Python 3.8+ standard library only.  No pip, no venv, no third-party
    imports.  If `ffprobe` happens to be on PATH it is used for deeper
    format detection; if it is not, the tool degrades to pure HTTP and
    says so.  Nothing is ever installed, downloaded or written to the
    input playlist.

NETWORK SAFETY
    The only network calls performed are GET requests (optionally with a
    Range header).  There is no code path that POSTs, PUTs or DELETEs
    anything, so "network write" does not exist here; --dry-run/--confirm
    instead govern the single local write, the --fix output file.

USAGE
    python3 tools/check_channels.py PLAYLIST [--limit 40] [--fix out.m3u]
    python3 tools/check_channels.py https://host/list.m3u --json r.json
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, urljoin, urlparse, urlsplit, urlunsplit

import http.client
import urllib.error
import urllib.request

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: Browser-ish UA.  Many CDNs answer 403 to python-urllib, so impersonate a
#: browser by default; --user-agent overrides it per playlist.
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

#: The complete verdict taxonomy.  Every channel gets exactly one of these.
VERDICTS: Tuple[str, ...] = (
    "OK",
    "DEAD-404",
    "DEAD-403",
    "DEAD-DNS",
    "DEAD-TLS",
    "DEAD-CONN",
    "TIMEOUT",
    "EMPTY",
    "NOT-HLS",
    "REDIRECTED",
    "UNKNOWN-ERROR",
    "TRUNCATED",
    "NO-URL",
)

#: Verdict -> human one-liner, used by --verbose and the README table.
VERDICT_HELP: Dict[str, str] = {
    "OK": "stream responded and looks like what the URL claims",
    "DEAD-404": "HTTP 404, or a redirect chain ending in 404",
    "DEAD-403": "HTTP 401/403 - geo-blocked, auth-walled or referer-gated",
    "DEAD-DNS": "hostname does not resolve (NXDOMAIN / no such host)",
    "DEAD-TLS": "TLS handshake or certificate verification failed",
    "DEAD-CONN": "TCP connect refused/reset/unreachable",
    "TIMEOUT": "no answer within --timeout (or hung after connecting)",
    "EMPTY": "HTTP 200 but zero bytes of body",
    "NOT-HLS": "URL claims to be HLS but the body is not an M3U playlist",
    "REDIRECTED": "answered, but the final URL differs from the playlist URL",
    "TRUNCATED": "server closed the connection before the full body arrived",
    "UNKNOWN-ERROR": "failed for a reason we do not classify",
    "NO-URL": "EXTINF entry has no URL line at all",
}

#: Verdicts that mean "the provider's stream is gone".  These are never
#: "fixed" by --fix: a dead stream is a provider problem, not something an
#: M3U attribute can repair, and pretending otherwise hides the real fault.
DEAD_VERDICTS = frozenset(
    {"DEAD-404", "DEAD-403", "DEAD-DNS", "DEAD-TLS", "DEAD-CONN", "TIMEOUT", "EMPTY"}
)

#: Transport classifications derived from URL + attributes.
TRANSPORT_TYPES = ("hls-master", "hls-media", "dash", "mpegts", "mp4", "unknown")

#: Extension -> transport.  Only consulted against the URL *path*, with the
#: query string stripped, so https://h/x.m3u8?token=1 still classifies.
EXTENSION_MAP: Dict[str, str] = {
    ".m3u8": "hls",
    ".m3u": "hls",
    ".mpd": "dash",
    ".ts": "mpegts",
    ".mpegts": "mpegts",
    ".m2ts": "mpegts",
    ".mp4": "mp4",
    ".m4v": "mp4",
    ".mov": "mp4",
    ".webm": "mp4",
    ".mkv": "mp4",
}

#: KODIPROP mimetype -> transport, when the playlist states it explicitly.
MIMETYPE_MAP: Dict[str, str] = {
    "application/x-mpegurl": "hls",
    "application/vnd.apple.mpegurl": "hls",
    "audio/mpegurl": "hls",
    "audio/x-mpegurl": "hls",
    "application/mpegurl": "hls",
    "application/dash+xml": "dash",
    "video/mp2t": "mpegts",
    "video/x-mpegts": "mpegts",
    # "video/mpeg" is the value Kodi documents for MPEG-TS and the value
    # --fix itself writes.  Without it the tool could not re-read its own
    # remediation, and a repaired playlist would still classify as unknown.
    "video/mpeg": "mpegts",
    "video/mp4": "mp4",
}

#: Content-Type observed on the wire -> transport.  Used to correct a URL
#: whose extension lies, and to justify a KODIPROP in --fix.
CONTENT_TYPE_MAP: Dict[str, str] = {
    "application/vnd.apple.mpegurl": "hls",
    "application/x-mpegurl": "hls",
    "audio/x-mpegurl": "hls",
    "audio/mpegurl": "hls",
    "application/dash+xml": "dash",
    "video/mp2t": "mpegts",
    "video/x-mpegts": "mpegts",
    "video/mp4": "mp4",
    "application/mp4": "mp4",
}

MAX_PLAYLIST_BYTES = 262_144   # cap on playlist text we pull into memory
PLAYLIST_MAX_BYTES = 64 * 1024 * 1024  # playlist download ceiling; real ones are ~2-3 MB
RANGE_BYTES = 65_536           # the "short ranged GET" size, per the spec
FFPROBE_TIMEOUT = 15          # ceiling: a live HLS stream takes ~9s for ffprobe to settle


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class Channel:
    """One playlist entry, in original file order.

    ``line_*`` fields are 0-based indexes into the raw line list of the
    source playlist.  They are what lets --fix edit the *original* text
    (keeping comments and layout) instead of re-serialising a lossy model.
    """

    index: int
    name: str
    url: str
    line_extinf: int
    line_url: int = -1
    line_last: int = -1
    tvg_id: str = ""
    group: str = ""
    logo: str = ""
    attrs: Dict[str, str] = field(default_factory=dict)
    kodiprops: List[str] = field(default_factory=list)
    extra_ext: List[str] = field(default_factory=list)
    catchup: Dict[str, str] = field(default_factory=dict)
    duration: str = ""
    malformed: bool = False
    transport: str = "unknown"
    type_reasons: List[str] = field(default_factory=list)
    duplicate_of: Optional[int] = None
    ffprobe: Dict[str, object] = field(default_factory=dict)

    @property
    def mimetype_prop(self) -> str:
        """Value of an existing ``#KODIPROP:mimetype=`` line, if any."""
        for prop in self.kodiprops:
            if "mimetype=" in prop.lower():
                return prop.split("=", 1)[1].strip()
        return ""

    @property
    def display_type(self) -> str:
        """Transport type, with ``+catchup`` appended when applicable.

        A catchup-enabled channel is still HLS underneath, so the catchup
        trait is appended rather than replacing the transport.
        """
        return f"{self.transport}+catchup" if self.catchup else self.transport


@dataclass
class ProbeResult:
    """Outcome of probing one channel.  Exactly one ``verdict``."""

    index: int
    verdict: str
    detail: str
    seconds: float
    http_status: Optional[int] = None
    content_type: str = ""
    content_length: Optional[int] = None
    content_range: str = ""
    bytes_read: int = 0
    final_url: str = ""
    redirected: bool = False
    live: Optional[bool] = None       # HLS only: False when ENDLIST present
    target_duration: str = ""
    variants: int = 0                  # HLS master only
    ffprobe: Dict[str, object] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Input
# --------------------------------------------------------------------------


def fetch_source(source: str, timeout: float, user_agent: str) -> str:
    """Read the playlist from a local path or an http(s) URL.

    A URL is fetched with ``urllib.request`` and a browser-ish User-Agent
    because a number of playlist hosts serve 403 to anything that does not
    look like a browser.

    Raises ``OSError``/``urllib.error.URLError`` on failure; the caller
    turns those into a clean message rather than a traceback.
    """
    parsed = urlparse(source)
    if parsed.scheme in ("http", "https"):
        request = urllib.request.Request(
            source, headers={"User-Agent": user_agent, "Accept": "*/*"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(PLAYLIST_MAX_BYTES)
        if len(payload) >= PLAYLIST_MAX_BYTES:
            # Never truncate silently: a half-read playlist would report
            # "these channels are missing" for channels that were simply
            # never downloaded.  The real iptv-org index is ~2.5 MB, so the
            # cap is set far above any legitimate playlist.
            print(
                f"warning     : playlist exceeded {PLAYLIST_MAX_BYTES} bytes and was truncated; "
                "results are incomplete",
                file=sys.stderr,
            )
        return payload.decode("utf-8", errors="replace")
    with open(source, "rb") as handle:
        return handle.read().decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

_ATTR_RE = re.compile(r"""([A-Za-z0-9_.\-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s,]+))""")
_CATCHUP_KEYS = ("catchup", "catchup-source", "catchup-days", "catchup-type")

#: Only CR, LF and CRLF count as line terminators for playlist rewriting.
#: ``str.splitlines()`` additionally breaks on \v \f \x1c \x1d \x1e \x85
#: \u2028 \u2029, so a playlist whose URL or name contains one of those bytes
#: would be silently cut in half and rejoined with a real newline.  Matching
#: the terminators explicitly is what makes a no-op --fix byte-exact.
_TERMINATOR_RE = re.compile(r"(\r\n|\r|\n)")

def split_lines_keepends(text: str) -> List[str]:
    """Split ``text`` into lines that each keep their original terminator.

    Two properties matter here and both are lost by ``str.splitlines()``:

    * the terminator travels with its line, so joining with ``"".join(...)``
      reproduces CRLF playlists byte for byte instead of normalising them to
      LF and turning a zero-change --fix into a whole-file rewrite;
    * only CR/LF/CRLF split, so \x85 or \u2028 inside a URL stays inside its
      line rather than becoming a line break on rejoin.
    """
    parts = _TERMINATOR_RE.split(text)
    lines: List[str] = []
    for position in range(0, len(parts), 2):
        content = parts[position]
        terminator = parts[position + 1] if position + 1 < len(parts) else ""
        if content or terminator:
            lines.append(content + terminator)
    return lines


def line_terminator(line: str) -> str:
    """Return the terminator of a keepends line (``""`` if it has none)."""
    match = _TERMINATOR_RE.search(line)
    return match.group(1) if match else ""


def line_body(line: str) -> str:
    """Return a keepends line without its terminator, for diffing/display."""
    return _TERMINATOR_RE.sub("", line, count=1)


def _split_extinf(body: str) -> Tuple[str, str]:
    """Split ``-1 tvg-id="x",Channel Name`` into (attribute blob, name).

    The separator is the *first* comma that sits outside quotes, not the
    last one and not a naive ``split(',')``: real playlists put commas in
    channel names (``ES: La 1, HD``) and commas inside quoted attribute
    values (``tvg-logo="http://x/a,b.png"``).  A naive split mis-slices
    both of those; this does not.
    """
    in_quote = ""
    for position, char in enumerate(body):
        if in_quote:
            if char == in_quote:
                in_quote = ""
        elif char in ('"', "'"):
            in_quote = char
        elif char == ",":
            return body[:position], body[position + 1 :]
    return body, ""


def _parse_attrs(blob: str) -> Dict[str, str]:
    """Extract ``key="value"`` (and unquoted ``key=value``) pairs."""
    attrs: Dict[str, str] = {}
    for match in _ATTR_RE.finditer(blob):
        key = match.group(1).lower()
        value = match.group(2) or match.group(3) or match.group(4) or ""
        attrs.setdefault(key, value)
    return attrs


def parse_m3u(text: str) -> Tuple[List[Channel], int]:
    """Parse M3U/M3U8 text into channels, preserving original order.

    Handles the real-world shapes: ``#EXTINF`` with attributes in any order,
    attributes that are missing, unquoted attributes, comma-laden names,
    ``#EXTGRP``, ``#KODIPROP:``/``#EXTVLCOPT:`` lines, ``#EXTM3U`` headers and
    bare comment lines.  Lines that cannot be understood are counted and
    reported instead of raising - one broken entry must not abort a 10k-line
    playlist.

    Returns ``(channels, unparseable_line_count)``.
    """
    # Must use the same splitter as the --fix path, otherwise channel.line_*
    # indexes would not line up with the lines being spliced.
    lines = split_lines_keepends(text)
    channels: List[Channel] = []
    unparseable = 0
    pending: Optional[Channel] = None
    seen_urls: Dict[str, int] = {}

    for line_no, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue

        if line.startswith("#EXTINF:"):
            header, name = _split_extinf(line[len("#EXTINF:") :])
            duration = header.split(" ", 1)[0].strip()
            attrs_blob = header[len(duration) :]
            attrs = _parse_attrs(attrs_blob)
            # A missing separator comma means the entry has no name and the
            # attribute blob is not trustworthy; keep the channel (so it is
            # still probed and reported) but flag it and count the line.
            malformed = "," not in line[len("#EXTINF:") :]
            catchup = {key: attrs[key] for key in _CATCHUP_KEYS if key in attrs}
            channel = Channel(
                index=len(channels),
                name=name.strip() or (f"(unnamed line {line_no + 1})"),
                url="",
                line_extinf=line_no,
                line_last=line_no,
                duration=duration,
                malformed=malformed,
                attrs=attrs,
                tvg_id=attrs.get("tvg-id", ""),
                group=attrs.get("group-title", ""),
                logo=attrs.get("tvg-logo") or attrs.get("logo", ""),
                catchup=catchup,
            )
            if malformed:
                unparseable += 1
            if channel.url:
                seen_urls.setdefault(channel.url, channel.index)
            channels.append(channel)
            pending = channel
            continue

        if line.startswith("#"):
            upper = line.upper()
            if pending is None:
                # The #EXTM3U header and free-form comments legitimately come
                # before the first #EXTINF and are not defects.  An orphaned
                # *property* line is, because it has no entry to attach to.
                if upper.startswith(("#KODIPROP:", "#EXTGRP:")):
                    unparseable += 1
                continue
            if upper.startswith("#KODIPROP:"):
                pending.kodiprops.append(line[len("#KODIPROP:") :].strip())
            elif upper.startswith("#EXTGRP:"):
                pending.group = line[len("#EXTGRP:") :].strip() or pending.group
            else:
                pending.extra_ext.append(line)
            pending.line_last = line_no
            continue

        # Anything else is a URL line.
        if pending is None:
            unparseable += 1  # orphan URL with no #EXTINF above it
            continue
        if pending.url:
            unparseable += 1  # second URL for the same entry: ambiguous
            continue
        pending.url = line
        pending.line_url = line_no
        pending.line_last = line_no
        first_seen = seen_urls.get(line)
        if first_seen is not None:
            pending.duplicate_of = first_seen
        else:
            seen_urls.setdefault(line, pending.index)
        pending = None

    # Classify here, not in the caller, so every consumer of parse_m3u
    # gets a typed channel and no call site can forget.
    for channel in channels:
        classify_channel(channel)
    return channels, unparseable


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def split_kodi_options(url: str) -> Tuple[str, str]:
    """Split a Kodi M3U URL into ``(url, options)``.

    Kodi appends ``|name=value`` pairs to stream URLs (``|User-Agent=...``,
    ``|Referer=...``) and they are common in real playlists.  That tail is
    Kodi's own option syntax, not part of the URL: sent to the server it
    becomes part of the path and the channel 404s while Kodi plays it.  So
    classification, type detection and every HTTP probe use the base URL,
    and ``--fix`` re-attaches the tail to anything it writes back.
    """
    marker = url.find("|")
    if marker < 0:
        return url, ""
    return url[:marker], url[marker:]


def classify_channel(channel: Channel) -> None:
    """Set ``channel.transport`` and explain, in code comments and data, why.

    Rule order is deliberate: an explicit statement by the playlist author
    (a ``#KODIPROP:mimetype=``) outranks the URL, because that is the
    attribute Kodi itself is told to trust.

      1. No URL at all -> ``unknown`` (the probe will say NO-URL anyway).
      2. ``#KODIPROP:mimetype=`` -> the stated transport wins outright.
      3. Otherwise the URL *path* extension decides; the query string is
         ignored, so ``/live.m3u8?token=abc`` is still HLS.
      4. A ``path=`` attribute is a full alternate URL and is used when the
         primary URL carries no usable extension.
      5. No extension and no mimetype -> ``unknown``, which routes the
         channel to a plain ranged GET and lets the response say what it is.
    """
    reasons: List[str] = []
    transport = "unknown"

    if not channel.url:
        channel.transport = "unknown"
        channel.type_reasons = ["no URL line in the entry"]
        return

    parsed = urlparse(split_kodi_options(channel.url)[0])
    path_only = parsed.path or ""
    extension = os.path.splitext(path_only)[1].lower()

    mimetype = channel.mimetype_prop.split(";")[0].strip().lower()
    if mimetype and mimetype in MIMETYPE_MAP:
        # Rule 2: the playlist states the type, trust the author.
        transport = MIMETYPE_MAP[mimetype]
        reasons.append(f"#KODIPROP mimetype={mimetype} declares {transport}")
    elif mimetype:
        reasons.append(f"#KODIPROP mimetype={mimetype} is not a known type")
        transport = "unknown"

    if transport == "unknown" and extension in EXTENSION_MAP:
        # Rule 3: extension of the path, query string deliberately ignored.
        transport = EXTENSION_MAP[extension]
        reasons.append(f"URL path ends in {extension} -> {transport}")

    if transport == "unknown" and mimetype not in MIMETYPE_MAP:
        # Rule 4: some providers hide the real location in a path= attribute.
        alt = channel.attrs.get("path", "")
        alt_ext = os.path.splitext(urlparse(alt).path or "")[1].lower()
        if alt_ext in EXTENSION_MAP:
            transport = EXTENSION_MAP[alt_ext]
            reasons.append(f"path={alt_ext} attribute -> {transport}")

    if transport == "unknown" and not extension:
        # Rule 5: extensionless URL - the wire response decides, not us.
        reasons.append("no file extension and no mimetype attribute -> unknown")

    if transport == "hls":
        # HLS master vs media is decided by the body, not the filename, so
        # this is only a display hint.  Keeping it inside TRANSPORT_TYPES
        # means a channel stays well-typed even when the probe dies early.
        channel.transport = "hls-master" if _guess_master(channel) else "hls-media"
        reasons.append(f"assumed {channel.transport} from the filename; probe refines this")
    else:
        channel.transport = transport
    channel.type_reasons = reasons


def _guess_master(channel: Channel) -> bool:
    """Pre-probe guess for HLS flavour.

    ``variant.m3u8``/``index.m3u8``/``playlist.m3u8`` are conventionally
    masters, but this is only a display hint: ``probe_hls`` overwrites the
    transport with the truth from the ``#EXT-X-STREAM-INF`` count.
    """
    name = os.path.basename(urlparse(split_kodi_options(channel.url)[0]).path or "").lower()
    return any(name.endswith(suffix) for suffix in ("index", "master", "playlist", "main"))


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------


def _normalise_ct(content_type: str) -> str:
    """Lowercase a Content-Type and drop parameters (``; charset=...``)."""
    return (content_type or "").split(";")[0].strip().lower()


def _verdict_for_exception(exc: BaseException) -> Tuple[str, str]:
    """Map a network exception onto the verdict taxonomy.

    Order matters: ``ssl.SSLError`` and ``socket.gaierror`` are both
    ``OSError`` subclasses, so the specific ones are tested before the
    generic connection failures.  Each branch returns (verdict, detail).
    """
    if isinstance(exc, urllib.error.HTTPError):
        # An HTTPError is also a response object, so it can carry a body hint.
        if exc.code in (401, 403):
            return "DEAD-403", f"HTTP {exc.code} {exc.reason}"
        if exc.code == 404:
            return "DEAD-404", "HTTP 404 Not Found"
        if exc.code in (408, 504):
            return "TIMEOUT", f"HTTP {exc.code} {exc.reason}"
        return "UNKNOWN-ERROR", f"HTTP {exc.code} {exc.reason}"

    if isinstance(exc, http.client.IncompleteRead):
        # The server hung up mid-body.  Routine on flaky IPTV providers and
        # NOT an OSError, so without this branch it escaped the probe and
        # destroyed every other channel result in the run.
        return "TRUNCATED", f"response truncated by server: {exc}"
    if isinstance(exc, http.client.HTTPException):
        # BadStatusLine, LineTooLong, ResponseNotReady, CannotSendRequest,
        # InvalidURL, NotConnected: the HTTP layer itself failed.
        return "UNKNOWN-ERROR", f"HTTP protocol error: {type(exc).__name__}: {exc}"

    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ssl.SSLError) or isinstance(exc, ssl.SSLError):
        return "DEAD-TLS", f"TLS failure: {reason}"
    if isinstance(reason, socket.gaierror) or isinstance(exc, socket.gaierror):
        return "DEAD-DNS", f"DNS lookup failed: {reason}"
    if isinstance(reason, (socket.timeout, TimeoutError)) or isinstance(exc, (socket.timeout, TimeoutError)):
        return "TIMEOUT", f"timed out: {reason}"
    if isinstance(
        reason,
        (
            ConnectionRefusedError,
            ConnectionResetError,
            ConnectionAbortedError,
            socket.herror,
        ),
    ) or isinstance(exc, (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError)):
        return "DEAD-CONN", f"connection failed: {reason}"
    # A peer that hangs up mid-response surfaces as EPIPE/ECONNRESET, and
    # urllib sometimes reports the reason as a bare errno int rather than an
    # OSError instance.  Both are connection failures, not unknown errors.
    errno = reason if isinstance(reason, int) else getattr(reason, "errno", None)
    if errno in (32, 54, 104):  # EPIPE, ECONNRESET, ECONNABORTED
        return "DEAD-CONN", f"connection failed (errno {errno}): {reason}"
    if errno in (101, 113, 65):  # ENETUNREACH, EHOSTUNREACH, EHOSTDOWN
        return "DEAD-CONN", f"host unreachable (errno {errno}): {reason}"
    return "UNKNOWN-ERROR", f"{type(exc).__name__}: {reason}"


def _final_url(response, requested: str) -> Tuple[str, bool]:
    """Return the post-redirect URL and whether it differs from the request."""
    try:
        final = response.geturl()
    except AttributeError:
        final = requested
    redirected = final.rstrip("/") != requested.rstrip("/")
    return final, redirected


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------


def probe_hls(
    url: str,
    channel: Channel,
    cfg: "Config",
    depth: int = 0,
) -> Tuple[str, str, Dict[str, object]]:
    """Fetch and structurally validate an HLS playlist.

    Validation is the whole point: an HTTP 200 that returns an HTML error
    page is *not* a working channel, and Kodi will happily try to play it.
    Checks, in the order a player would hit them:
      * ``#EXTM3U`` on the first non-blank line
      * at least one ``#EXTINF`` (a playlist with no segments is useless)
      * ``#EXT-X-STREAM-INF`` present  => master, so the first variant is
        probed too (one level only, to stay bounded)
      * ``#EXT-X-TARGETDURATION`` present
      * ``#EXT-X-ENDLIST`` present => VOD; absent => still live

    Returns ``(verdict, detail, info)``.
    """
    request = urllib.request.Request(
        safe_url(url),
        headers={"User-Agent": cfg.user_agent, "Accept": "*/*", "Accept-Encoding": "identity"},
    )
    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout) as response:
            status = response.getcode()
            body = response.read(MAX_PLAYLIST_BYTES)
            content_type = _normalise_ct(response.headers.get("Content-Type", ""))
            declared_length = response.headers.get("Content-Length", "")
            final, redirected = _final_url(response, url)
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        http.client.HTTPException,
        OSError,
        ValueError,
    ) as exc:
        verdict, detail = _verdict_for_exception(exc)
        return verdict, detail, {"http_status": getattr(exc, "code", None)}

    text = body.decode("utf-8", errors="replace")
    playlist_lines = [line.strip() for line in text.splitlines()]
    playlist_lines = [line for line in playlist_lines if line]

    info: Dict[str, object] = {
        "http_status": status,
        "content_type": content_type,
        "final_url": final,
        "redirected": redirected,
        "bytes_read": len(body),
    }

    # A bounded read() returns whatever arrived without raising, so a server
    # that hangs up mid-body is otherwise indistinguishable from a complete
    # playlist.  Only a declared length larger than what we got can prove
    # truncation; chunked responses declare nothing and are left alone.
    if declared_length.isdigit() and int(declared_length) > len(body):
        return (
            "TRUNCATED",
            f"HTTP {status} declared {declared_length} bytes but only {len(body)} arrived",
            info,
        )

    if not playlist_lines or not playlist_lines[0].upper().startswith("#EXTM3U"):
        snippet = playlist_lines[0][:40] if playlist_lines else "<empty>"
        return (
            "NOT-HLS",
            f"HTTP {status} but body is not an M3U (content-type={content_type or 'n/a'}, "
            f"first line={snippet!r})",
            info,
        )

    # A master playlist lists variants by URI and normally carries no
    # #EXTINF at all, so the "must contain #EXTINF" rule applies to media
    # playlists only.  Getting this backwards fails every real master.
    extinf_count = sum(1 for line in playlist_lines if line.startswith("#EXTINF"))
    streaminf = [line for line in playlist_lines if line.upper().startswith("#EXT-X-STREAM-INF")]
    if not streaminf and extinf_count == 0:
        return (
            "NOT-HLS",
            f"HTTP {status}, #EXTM3U present but neither #EXTINF nor #EXT-X-STREAM-INF",
            info,
        )

    target = ""
    for line in playlist_lines:
        if line.upper().startswith("#EXT-X-TARGETDURATION:"):
            target = line.split(":", 1)[1].strip()
            break
    has_endlist = any(line.upper().startswith("#EXT-X-ENDLIST") for line in playlist_lines)
    live = not has_endlist

    info["live"] = live
    info["target_duration"] = target
    info["variants"] = len(streaminf)

    if channel is not None:
        # Truth from the wire beats our pre-probe guess.
        channel.transport = "hls-master" if streaminf else "hls-media"
        reasons = [f"{len(streaminf)} #EXT-X-STREAM-INF variant(s)" if streaminf else "no variants: media playlist"]
        reasons.append(f"targetduration={target or 'absent'}")
        reasons.append("ENDLIST present (VOD)" if has_endlist else "no ENDLIST (live)")
        if depth == 0:
            reasons.append("probed HLS body")
        channel.type_reasons = reasons

    if streaminf and depth == 0:
        variant_url = _first_variant_uri(playlist_lines)
        if variant_url:
            # Resolve against the master's own URL, not against a fake
            # directory: urljoin(".../master.m3u", "variant.m3u") correctly
            # yields ".../variant.m3u", whereas a trailing slash would make
            # it ".../master.m3u/variant.m3u" and 404 every real master.
            absolute = urljoin(final, variant_url)
            v_verdict, v_detail, v_info = probe_hls(absolute, None, cfg, depth=1)
            info["variant"] = {"url": absolute, "verdict": v_verdict, "detail": v_detail}
            if v_verdict != "OK" and v_verdict != "REDIRECTED":
                return v_verdict, f"master ok, but first variant {v_verdict}: {v_detail}", info

    # Masters report variants, media playlists report segments; using the
    # wrong counter would print "0 entries" for every healthy master.
    count_text = f"{len(streaminf)} variants" if streaminf else f"{extinf_count} entries"
    detail = (
        f"HTTP {status} {'master' if streaminf else 'media'}, {count_text}, "
        f"targetdur={target or 'n/a'}, {'live' if live else 'vod'}"
    )
    if redirected:
        return "REDIRECTED", f"{detail} -> {final}", info
    if not target:
        detail += " (no #EXT-X-TARGETDURATION)"
    return "OK", detail, info


def _first_variant_uri(playlist_lines: Sequence[str]) -> str:
    """Return the URI of the first variant that follows a STREAM-INF tag."""
    for position, line in enumerate(playlist_lines):
        if line.upper().startswith("#EXT-X-STREAM-INF"):
            for candidate in playlist_lines[position + 1 :]:
                if candidate and not candidate.startswith("#"):
                    return candidate
    return ""


def _looks_like_error_document(content_type: str, body: bytes) -> bool:
    """True when a 2xx body is an error or login page rather than media.

    Geo-walls, paywalls, soft-404s and "please log in" interstitials all
    answer 200 with text.  Counting those as working is the one way this
    tool could report a dead channel as OK, so they are named explicitly.
    A real manifest is never rejected: ``#EXTM3U`` and ``<MPD`` win first,
    whatever the Content-Type claims.
    """
    head = body[:512].lstrip()
    if head.startswith(b"#EXTM3U") or b"<MPD" in head:
        return False
    if content_type in ("text/html", "application/xhtml+xml"):
        return True
    if content_type in ("text/plain", "application/json", "application/javascript"):
        return True
    return head[:64].lower().startswith((b"<!doctype", b"<html", b"<?xml"))


def safe_url(url: str) -> str:
    """Percent-encode a URL so http.client can send it.

    Playlists contain non-ASCII paths (Thai channel names appear in URLs such
    as ``/Transcoder/มายาHD.stream_576p/playlist.m3u8``).  http.client
    encodes the request target as ASCII and raises UnicodeEncodeError, which
    the verdict taxonomy then reports as UNKNOWN-ERROR -- i.e. a working
    channel is scored as an unclassifiable failure.  Encoding the path and
    query up front fixes that and is what a browser does anyway.
    """
    parts = urlsplit(url)
    if parts.scheme in ("", "file") and not parts.netloc:
        return url
    path = quote(parts.path, safe="/%:@&=+$,;~!*'()[]-._")
    query = quote(parts.query, safe="=&?/:;%+@$,~!*'()[]-._")
    return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def probe_range_get(url: str, cfg: "Config") -> Tuple[str, str, Dict[str, object]]:
    """Issue a short ranged GET and describe what came back.

    Used for direct ``.ts``/``.mp4`` payloads, DASH manifests and
    extensionless URLs.  A 64 KiB window is enough to prove the socket
    carries video bytes while keeping a 5 GB VOD file off the disk.
    Servers that ignore ``Range`` answer 200 with the full body; we read
    only the first window and close, which is still cheap.

    Returns ``(verdict, detail, info)``.
    """
    headers = {
        "User-Agent": cfg.user_agent,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Range": f"bytes=0-{RANGE_BYTES - 1}",
    }
    request = urllib.request.Request(safe_url(url), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout) as response:
            status = response.getcode()
            body = response.read(RANGE_BYTES)
            content_type = _normalise_ct(response.headers.get("Content-Type", ""))
            content_length = response.headers.get("Content-Length")
            content_range = response.headers.get("Content-Range", "")
            final, redirected = _final_url(response, url)
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        http.client.HTTPException,
        OSError,
        ValueError,
    ) as exc:
        verdict, detail = _verdict_for_exception(exc)
        return verdict, detail, {"http_status": getattr(exc, "code", None)}

    size = len(body)
    info: Dict[str, object] = {
        "http_status": status,
        "content_type": content_type,
        "content_length": int(content_length) if content_length and content_length.isdigit() else None,
        "content_range": content_range,
        "final_url": final,
        "redirected": redirected,
        "bytes_read": size,
    }

    length_text = content_range or (f"content-length={content_length}" if content_length else "content-length=?")
    if size == 0:
        return "EMPTY", f"HTTP {status}, {length_text}, zero bytes returned", info

    detail = f"HTTP {status} {content_type or 'no content-type'}, {length_text}, {size}B read"
    if _looks_like_error_document(content_type, body):
        return "NOT-HLS", f"{detail}, body is a text error/login page, not a stream", info
    if redirected:
        return "REDIRECTED", f"{detail} -> {final}", info
    return "OK", detail, info


def run_ffprobe(url: str, cfg: "Config") -> Dict[str, object]:
    """Ask ffprobe to identify the stream.  Returns ``{}`` when unavailable.

    Degradation is the contract here: no ffprobe on PATH, ``--no-ffprobe``,
    or a non-zero exit with unparseable output must all yield a neutral
    result, never an exception.  ffprobe itself is given ``-rw_timeout`` so
    a dead TCP connection cannot outlive the socket timeout, and the
    subprocess is additionally bounded by ``timeout=FFPROBE_TIMEOUT``.

    Only ever called as an argv list - never ``shell=True`` - so a channel
    name or URL containing shell metacharacters cannot be interpreted.
    """
    if not cfg.use_ffprobe or not cfg.ffprobe_path:
        return {}
    argv = [
        cfg.ffprobe_path,
        "-v", "error",
        "-user_agent", cfg.user_agent,
        "-rw_timeout", str(int(cfg.timeout * 1_000_000)),
        # Bound the analysis too: a live playlist with no ENDLIST otherwise
        # keeps ffprobe waiting for segments that never arrive, and every
        # such channel would burn the full wall-clock ceiling.
        "-analyzeduration", "2000000",
        "-probesize", "200000",
        "-i", url,
        "-show_entries", "format=format_name,duration,bit_rate",
        "-of", "json",
    ]
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT, check=False
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ffprobe timed out"}
    except OSError as exc:
        # Binary vanished between `which` and exec, or is not executable.
        return {"ok": False, "error": f"ffprobe could not run: {exc}"}

    if completed.returncode != 0 or not completed.stdout.strip():
        stderr = (completed.stderr or "").strip().splitlines()
        message = stderr[-1] if stderr else f"exit {completed.returncode}"
        return {"ok": False, "error": message, "invalid_data": "Invalid data" in (completed.stderr or "")}

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"unparseable ffprobe json: {exc}"}

    fmt = payload.get("format") or {}
    result: Dict[str, object] = {
        "ok": True,
        "format_name": str(fmt.get("format_name", "")),
        "duration": fmt.get("duration"),
        "bit_rate": fmt.get("bit_rate"),
    }
    result["invalid_data"] = "Invalid data" in (completed.stderr or "")
    return result


def probe_channel(channel: Channel, cfg: "Config") -> ProbeResult:
    """Probe one channel and fold every signal into a single verdict.

    Dispatch is by transport:
      * hls-master / hls-media -> playlist fetch + structural validation
      * dash                   -> ranged GET (manifest is small text)
      * mpegts / mp4           -> ranged GET on the media payload
      * unknown                -> ranged GET, then let the observed
                                  Content-Type correct the classification
    """
    started = time.monotonic()
    if not channel.url:
        return ProbeResult(channel.index, "NO-URL", "#EXTINF entry has no URL line", 0.0)

    info: Dict[str, object] = {}
    # Kodi's "|name=value" tail is option syntax, not part of the URL.
    probe_url = split_kodi_options(channel.url)[0]
    if channel.transport in ("hls-master", "hls-media"):
        verdict, detail, info = probe_hls(probe_url, channel, cfg)
    else:
        verdict, detail, info = probe_range_get(probe_url, cfg)

    # A Content-Type that contradicts the URL is ground truth; promote the
    # transport so the table and --fix both see the real format.
    content_type = str(info.get("content_type", "") or "")
    observed = CONTENT_TYPE_MAP.get(content_type)
    # CONTENT_TYPE_MAP yields the coarse "hls"; the declared transport set
    # is hls-master/hls-media, and only an HLS body can split the two.
    if observed == "hls":
        # Only an HLS body can split master from media; if the channel is
        # already hls-* the wire told us nothing new, so leave it alone.
        observed = "hls-media" if not channel.transport.startswith("hls") else None
    if observed and observed != channel.transport and verdict in ("OK", "REDIRECTED"):
        channel.transport = observed
        channel.type_reasons.append(f"server said Content-Type={content_type} -> {observed}")

    wants_ffprobe = cfg.use_ffprobe and (
        channel.transport.startswith("hls") or channel.transport == "unknown"
    )
    if wants_ffprobe:
        channel.ffprobe = run_ffprobe(probe_url, cfg)
        info["ffprobe"] = channel.ffprobe

    seconds = time.monotonic() - started
    return ProbeResult(
        index=channel.index,
        verdict=verdict,
        detail=detail,
        seconds=seconds,
        http_status=info.get("http_status"),  # type: ignore[arg-type]
        content_type=content_type,
        content_length=info.get("content_length"),  # type: ignore[arg-type]
        content_range=str(info.get("content_range", "") or ""),
        bytes_read=int(info.get("bytes_read", 0) or 0),
        final_url=str(info.get("final_url", "") or ""),
        redirected=bool(info.get("redirected")),
        live=info.get("live"),  # type: ignore[arg-type]
        target_duration=str(info.get("target_duration", "") or ""),
        variants=int(info.get("variants", 0) or 0),
        ffprobe=channel.ffprobe,
    )


def _probe_channel_guarded(channel: Channel, cfg: "Config") -> ProbeResult:
    """Probe one channel, guaranteeing a verdict no matter what happens.

    The targeted ``except`` clauses in probe_hls/probe_range_get cover the
    failures we understand.  This is the backstop for the ones nobody
    anticipated: a bug in the probe, or an exception type from the standard
    library that inherits straight from ``Exception`` (as
    ``http.client.IncompleteRead`` does).  Losing an entire multi-hour scan
    because one channel raised is far worse than recording that channel as
    unprobeable, so the exception is converted into a verdict here.

    ``KeyboardInterrupt`` and ``SystemExit`` are re-raised: they mean the
    operator asked to stop, and swallowing them would make the tool
    uncancellable.
    """
    try:
        return probe_channel(channel, cfg)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - deliberate last-resort backstop
        verdict, detail = _verdict_for_exception(exc)
        return ProbeResult(
            index=channel.index,
            verdict=verdict,
            detail=f"uncaught {type(exc).__name__}: {detail}",
            seconds=0.0,
        )


def probe_all(channels: List[Channel], cfg: "Config") -> List[ProbeResult]:
    """Probe every channel with a bounded thread pool, in playlist order.

    Concurrency is capped by ``--concurrency`` because each worker may hold
    a socket and (for HLS) an ffprobe process.  Results are re-sorted by
    index so output order matches the playlist regardless of completion
    order - deterministic output matters for diffing two runs.  Every channel
    is guaranteed a result: see :func:`_probe_channel_guarded`.
    """
    if not channels:
        return []
    with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
        results = list(pool.map(lambda ch: _probe_channel_guarded(ch, cfg), channels))
    results.sort(key=lambda result: result.index)
    return results


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------


def select_channels(
    channels: List[Channel], include: Optional[str], exclude: Optional[str], limit: Optional[int]
) -> List[Channel]:
    """Apply --include/--exclude (regex over name+url+group) then --limit.

    ``--include`` is a whitelist, ``--exclude`` a blacklist; include is
    evaluated first so "everything except X" works.  Order is preserved.
    """
    selected = channels
    if include:
        pattern = re.compile(include, re.IGNORECASE)
        selected = [c for c in selected if pattern.search(f"{c.name} {c.group} {c.url}")]
    if exclude:
        pattern = re.compile(exclude, re.IGNORECASE)
        selected = [c for c in selected if not pattern.search(f"{c.name} {c.group} {c.url}")]
    if limit is not None and limit >= 0:
        selected = selected[:limit]
    # Renumber so the table and JSON index match the printed rows.
    for position, channel in enumerate(selected):
        channel.index = position
    return selected


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _clip(text: str, width: int) -> str:
    """Truncate to ``width`` chars with a single-character ellipsis."""
    text = text.replace("\t", " ").strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def render_table(channels: List[Channel], results: List[ProbeResult], verbose: bool) -> str:
    """Render the per-channel table, widened to the longest real value."""
    rows: List[Tuple[str, str, str, str, str, str]] = []
    for channel, result in zip(channels, results):
        type_text = channel.display_type
        if verbose:
            type_text += " | " + "; ".join(channel.type_reasons)
        if channel.duplicate_of is not None:
            type_text += f" [dup of #{channel.duplicate_of}]"
        rows.append(
            (
                f"#{channel.index}",
                channel.name,
                type_text,
                result.verdict,
                result.detail,
                f"{result.seconds:.2f}s",
            )
        )
    headers = ("#", "CHANNEL", "TYPE", "VERDICT", "DETAIL", "TIME")
    widths = [
        max(len(headers[i]), max((len(row[i]) for row in rows), default=0))
        for i in range(len(headers))
    ]
    widths = [min(width, cap) for width, cap in zip(widths, (5, 40, 46, 14, 78, 7))]

    def line(cells: Sequence[str]) -> str:
        return "  ".join(_clip(cell, width).ljust(width) for cell, width in zip(cells, widths))

    out = [line(headers), "  ".join("-" * width for width in widths)]
    out.extend(line(row) for row in rows)
    return "\n".join(out)


def render_summary(channels: List[Channel], results: List[ProbeResult]) -> Tuple[str, float]:
    """Return the summary block and the percentage of strictly-OK channels.

    "working" means verdict == OK.  REDIRECTED streams do play, so they are
    reported on their own line; --fail-under uses the strict OK figure
    because a playlist full of redirects is usually a playlist full of
    expiring CDN links.
    """
    total = len(results)
    counts = Counter(result.verdict for result in results)
    ok = counts.get("OK", 0)
    redirected = counts.get("REDIRECTED", 0)
    percent = (100.0 * ok / total) if total else 0.0
    unknown_lines = sum(1 for channel in channels if channel.malformed)

    lines = ["", "Summary", "  " + "-" * 46, f"  total channels     : {total}"]
    if unknown_lines:
        lines.append(f"  malformed #EXTINF   : {unknown_lines} (counted, not fatal)")
    for verdict in VERDICTS:
        if counts.get(verdict):
            note = f"   {VERDICT_HELP[verdict]}" if verdict != "OK" else ""
            lines.append(f"  {verdict:<18} : {counts[verdict]}{note}")
    lines.append("  " + "-" * 46)
    lines.append(f"  working (OK)        : {ok}/{total} = {percent:.1f}%")
    if redirected:
        playable = ok + redirected
        lines.append(
            f"  OK + REDIRECTED     : {playable}/{total} = {100.0 * playable / total:.1f}% (also playable)"
        )
    dead = sum(counts.get(verdict, 0) for verdict in DEAD_VERDICTS)
    not_ok = total - ok
    if not_ok:
        lines.append(
            f"  NOT confirmed OK    : {not_ok}/{total} = {100.0 * not_ok / total:.1f}% "
            f"(everything that is not OK, incl. truncated / not-HLS / unknown)"
        )
    if dead:
        lines.append(f"  of which dead       : {dead}/{total} = {100.0 * dead / total:.1f}%")
    return "\n".join(lines), percent


# --------------------------------------------------------------------------
# Remediation
# --------------------------------------------------------------------------


@dataclass
class Modification:
    """One line of the --fix audit log."""

    channel: str
    attribute: str
    old: str
    new: str
    reason: str
    applied: bool


def plan_fixes(
    lines: List[str],
    channels: List[Channel],
    results: List[ProbeResult],
    cfg: "Config",
) -> Tuple[List[str], List[Modification]]:
    """Return ``(new_lines, log)`` for an annotated copy of the playlist.

    Only remedies that are deterministic at the M3U level are emitted:

    1. ``NOT-HLS`` on an ``.m3u8`` URL that actually serves ``video/mp2t``
       -> add ``#KODIPROP:mimetype=video/mpeg``.  The provider changed the
       container without changing the path; telling Kodi the truth fixes it.
    2. Extensionless URL that answers fine but that ffprobe could not
       classify -> add ``mimetype=video/mp4`` or ``video/mpeg``, and only
       when the observed Content-Type or ffprobe format_name justifies it.
       Without that evidence we refuse, because a wrong mimetype breaks a
       stream that currently works.
    3. ``REDIRECTED`` -> rewrite the URL to the redirect target, but only
       behind ``--apply-redirects``.  Kodi can follow redirects itself; this
       exists for setups behind a broken DNS/proxy, and it is logged loudly
       because it hard-codes a host that may rotate.

    Dead streams are deliberately untouched: no attribute makes a 404 play.
    Each skipped dead channel gets its own log line saying so.
    """
    by_index = {result.index: result for result in results}
    log: List[Modification] = []
    edits: List[Tuple[int, str, str]] = []  # (line_index, kind, text)

    for channel in channels:
        result = by_index.get(channel.index)
        if result is None:
            continue
        label = channel.name

        if result.verdict in DEAD_VERDICTS:
            log.append(
                Modification(
                    label, "-", "-", "-",
                    f"{result.verdict}: dead stream is a provider problem, not an M3U problem - left unchanged",
                    False,
                )
            )
            continue

        if result.verdict == "NO-URL":
            log.append(
                Modification(
                    label, "url", "-", "-",
                    "NO-URL: entry has no URL line; nothing to fix mechanically, check the source playlist",
                    False,
                )
            )
            continue

        if result.verdict == "NOT-HLS":
            # Drive this off CONTENT_TYPE_MAP, not a substring test: MPEG-TS
            # advertises itself as "video/mp2t", which never contains the
            # substring "mpeg", so a naive check silently skips the exact
            # channel this remedy exists for.
            if CONTENT_TYPE_MAP.get(result.content_type) == "mpegts":
                _queue_mimetype(channel, result, "video/mpeg", "mpegts", log, edits)
            else:
                log.append(
                    Modification(
                        label, "-", "-", "-",
                        f"NOT-HLS but content-type={result.content_type or 'unknown'}: no safe mimetype, left unchanged",
                        False,
                    )
                )
            continue

        base_url, url_options = split_kodi_options(channel.url)
        if result.verdict == "REDIRECTED" and result.final_url and result.final_url != base_url:
            if cfg.apply_redirects:
                rewritten = result.final_url + url_options
                edits.append((channel.line_url, "replace", rewritten))
                log.append(
                    Modification(
                        label, "url", channel.url, rewritten,
                        "REDIRECTED: rewrote URL to the redirect target (--apply-redirects), "
                        "keeping any |options tail",
                        True,
                    )
                )
            else:
                log.append(
                    Modification(
                        label, "url", channel.url, result.final_url + url_options,
                        "REDIRECTED: would rewrite URL to the redirect target; pass --apply-redirects to do it",
                        False,
                    )
                )
            continue

        if result.verdict == "OK":
            extension = os.path.splitext(urlparse(split_kodi_options(channel.url)[0]).path or "")[1].lower()
            if not extension and not channel.mimetype_prop:
                mimetype = _justified_mimetype(result)
                if mimetype:
                    _queue_mimetype(channel, result, mimetype, mimetype, log, edits)
                else:
                    log.append(
                        Modification(
                            label, "-", "-", "-",
                            f"OK but extensionless and inconclusive (content-type={result.content_type or 'unknown'}): "
                            "no mimetype justified, left unchanged",
                            False,
                        )
                    )
            elif channel.malformed:
                log.append(
                    Modification(
                        label, "#EXTINF", "malformed", "malformed",
                        "malformed #EXTINF: attribute order/name is unusual but the stream answers; "
                        "rewriting names is a judgement call, left unchanged",
                        False,
                    )
                )
        elif result.verdict not in ("OK", "REDIRECTED", "NOT-HLS", "NO-URL"):
            # Silence is not a diagnosis: UNKNOWN-ERROR, and anything else that
            # falls through every rule above, still gets an explicit line.
            log.append(
                Modification(
                    label, "-", "-", "-",
                    f"{result.verdict}: no deterministic M3U-level remedy applies; left unchanged",
                    False,
                )
            )


    # Apply edits back-to-front so earlier line indexes stay valid.  Each
    # spliced line inherits the terminator of the line it sits next to, so a
    # CRLF playlist stays CRLF and a no-op run rejoins byte-for-byte.
    new_lines = list(lines)
    for line_index, kind, text in sorted(edits, key=lambda item: item[0], reverse=True):
        terminator = line_terminator(lines[line_index])
        if kind == "replace":
            new_lines[line_index] = text + terminator
        else:  # insert after
            new_lines.insert(line_index + 1, text + terminator)
    return new_lines, log


def _justified_mimetype(result: ProbeResult) -> str:
    """Return a KODIPROP mimetype only when the wire evidence supports it.

    Content-Type is checked first because it is cheap and authoritative;
    ffprobe's ``format_name`` is the fallback for servers that send
    ``application/octet-stream`` for everything.
    """
    content_type = result.content_type
    if content_type == "video/mp4" or content_type == "application/mp4":
        return "video/mp4"
    if content_type in ("video/mp2t", "video/x-mpegts"):
        return "video/mpeg"
    ff = result.ffprobe or {}
    if ff.get("ok"):
        formats = str(ff.get("format_name", "")).lower()
        if "mp4" in formats or "mov," in formats:
            return "video/mp4"
        if "mpegts" in formats or "mpegtsraw" in formats:
            return "video/mpeg"
    return ""


def _queue_mimetype(
    channel: Channel,
    result: ProbeResult,
    mimetype: str,
    _transport: str,
    log: List[Modification],
    edits: List[Tuple[int, str, str]],
) -> None:
    """Append a #KODIPROP:mimetype line, or explain why we did not."""
    existing = channel.mimetype_prop
    if existing:
        log.append(
            Modification(
                channel.name, "#KODIPROP:mimetype", existing, mimetype,
                f"already set (probe says {result.verdict}); left unchanged", False,
            )
        )
        return
    if channel.line_extinf < 0 or channel.line_url < 0:
        log.append(
            Modification(channel.name, "#KODIPROP:mimetype", "-", mimetype,
                        "entry has no URL line to attach the property to; left unchanged", False)
        )
        return
    prop_line = f"#KODIPROP:mimetype={mimetype}"
    edits.append((channel.line_extinf, "insert", prop_line))
    log.append(
        Modification(
            channel.name, "#KODIPROP:mimetype", "(absent)", prop_line,
            f"{result.verdict} with content-type={result.content_type or 'unknown'}: "
            "mimetype matches the bytes the server actually sends", True,
        )
    )


def render_fix_log(log: List[Modification]) -> str:
    """Render the --fix audit log, applied changes first."""
    header = (
        f"{'CHANNEL':<34}  {'ATTRIBUTE':<20}  {'OLD':<28}  {'NEW':<28}  WHY"
    )
    out = ["", "Modification log", "  " + "-" * 116, "  " + header, "  " + "-" * 116]
    for entry in sorted(log, key=lambda item: (not item.applied, item.channel)):
        tag = "CHANGE" if entry.applied else "no-op "
        out.append(
            f"  {tag} {_clip(entry.channel, 28):<28}  {_clip(entry.attribute, 20):<20}  "
            f"{_clip(entry.old, 26):<26}  {_clip(entry.new, 26):<26}  {_clip(entry.reason, 60)}"
        )
    out.append("  " + "-" * 116)
    applied = sum(1 for entry in log if entry.applied)
    out.append(f"  {applied} change(s), {len(log) - applied} channel(s) left unchanged")
    return "\n".join(out)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


@dataclass
class Config:
    """Runtime knobs, built once in :func:`parse_args`."""

    user_agent: str = DEFAULT_UA
    timeout: float = 10.0
    concurrency: int = 8
    use_ffprobe: bool = True
    ffprobe_path: str = ""
    apply_redirects: bool = False


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI.  Every flag documents its own default."""
    parser = argparse.ArgumentParser(
        prog="check_channels.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Check every channel in a Kodi M3U playlist and report one verdict per channel.\n"
            "Runs on the Python 3 standard library alone - nothing is installed, and the input\n"
            "playlist is never written to."
        ),
        epilog=(
            "verdicts: " + ", ".join(VERDICTS) + "\n"
            "examples:\n"
            "  python3 tools/check_channels.py playlist.m3u\n"
            "  python3 tools/check_channels.py playlist.m3u --limit 40 --concurrency 8\n"
            "  python3 tools/check_channels.py https://host/p.m3u --json r.json --fail-under 50\n"
            "  python3 tools/check_channels.py p.m3u --fix fixed.m3u --confirm\n"
        ),
    )
    parser.add_argument("source", help="path to a local .m3u file, or an http(s) URL to fetch")
    parser.add_argument(
        "--user-agent", default=DEFAULT_UA,
        help="User-Agent sent with playlist fetches and probes (default: a browser Chrome UA)",
    )
    parser.add_argument(
        "--timeout", type=float, default=10.0, metavar="SEC",
        help="per-request socket timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--concurrency", type=int, default=8, metavar="N",
        help="number of channels probed in parallel (default: %(default)s)",
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="probe at most N channels after filtering (default: no limit)",
    )
    parser.add_argument(
        "--include", default=None, metavar="REGEX",
        help="only probe channels whose name/url/group matches this regex (case-insensitive)",
    )
    parser.add_argument(
        "--exclude", default=None, metavar="REGEX",
        help="skip channels whose name/url/group matches this regex (case-insensitive)",
    )
    parser.add_argument(
        "--json", dest="json_path", default=None, metavar="PATH",
        help="write machine-readable results to PATH",
    )
    parser.add_argument(
        "--fail-under", type=float, default=None, metavar="PCT",
        help="exit 1 if the percentage of OK channels is below PCT (for CI)",
    )
    parser.add_argument(
        "--fix", dest="fix_path", default=None, metavar="OUT.m3u",
        help="write an annotated copy of the playlist with M3U-level remedies to OUT.m3u "
             "(never touches the input; needs --confirm to actually write)",
    )
    parser.add_argument(
        "--apply-redirects", action="store_true",
        help="with --fix, rewrite redirected channels to their final URL (logged; off by default)",
    )
    parser.add_argument(
        "--no-ffprobe", dest="use_ffprobe", action="store_false", default=True,
        help="do not shell out to ffprobe even if it is installed (pure-HTTP mode)",
    )
    parser.add_argument(
        "--dry-run", dest="dry_run", action="store_true", default=True,
        help="print the --fix diff without writing anything (default)",
    )
    parser.add_argument(
        "--confirm", dest="dry_run", action="store_false",
        help="allow --fix to actually write its output file",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="show classification reasoning and the verdict legend in the TYPE column",
    )
    return parser


def _resolve_ffprobe(use_ffprobe: bool) -> str:
    """Locate ffprobe once; an empty string means 'pure HTTP'."""
    if not use_ffprobe:
        return ""
    return shutil.which("ffprobe") or ""


def parse_args(argv: Optional[Sequence[str]] = None) -> Tuple[argparse.Namespace, Config]:
    """Parse argv and freeze the runtime :class:`Config`."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    if args.fail_under is not None and not 0.0 <= args.fail_under <= 100.0:
        parser.error("--fail-under must be between 0 and 100")
    config = Config(
        user_agent=args.user_agent,
        timeout=args.timeout,
        concurrency=args.concurrency,
        use_ffprobe=args.use_ffprobe,
        ffprobe_path=_resolve_ffprobe(args.use_ffprobe),
        apply_redirects=args.apply_redirects,
    )
    return args, config


def write_json(
    path: str,
    source: str,
    channels: List[Channel],
    results: List[ProbeResult],
    percent: float,
    unparseable: int,
    cfg: Config,
) -> None:
    """Write the machine-readable report."""
    document = {
        "source": source,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "config": {
            "user_agent": cfg.user_agent,
            "timeout": cfg.timeout,
            "concurrency": cfg.concurrency,
            "ffprobe": cfg.ffprobe_path or None,
        },
        "unparseable_lines": unparseable,
        "summary": {
            "total": len(results),
            "verdicts": dict(Counter(result.verdict for result in results)),
            "working_percent": round(percent, 2),
        },
        "channels": [
            {
                "index": channel.index,
                "name": channel.name,
                "url": channel.url,
                "tvg_id": channel.tvg_id,
                "group": channel.group,
                "logo": channel.logo,
                "stream_type": channel.transport,
                "display_type": channel.display_type,
                "type_reasons": channel.type_reasons,
                "kodiprops": channel.kodiprops,
                "catchup": channel.catchup,
                "malformed": channel.malformed,
                "duplicate_of": channel.duplicate_of,
                "verdict": result.verdict,
                "detail": result.detail,
                "seconds": round(result.seconds, 3),
                "http_status": result.http_status,
                "content_type": result.content_type,
                "content_length": result.content_length,
                "content_range": result.content_range,
                "bytes_read": result.bytes_read,
                "redirected_to": result.final_url if result.redirected else "",
                "hls_live": result.live,
                "hls_variants": result.variants,
                "ffprobe": result.ffprobe,
            }
            for channel, result in zip(channels, results)
        ],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point.  Returns the process exit code."""
    args, cfg = parse_args(argv)

    print(f"source       : {args.source}")
    print(f"timeout      : {cfg.timeout:g}s   concurrency: {cfg.concurrency}")
    print(f"ffprobe      : {cfg.ffprobe_path or 'not used (pure-HTTP mode)'}")

    try:
        text = fetch_source(args.source, cfg.timeout, cfg.user_agent)
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        OSError,
        ValueError,
    ) as exc:
        verdict, detail = _verdict_for_exception(exc)
        print(f"error        : cannot read playlist - {verdict}: {detail}", file=sys.stderr)
        return 2

    channels, unparseable = parse_m3u(text)
    total_parsed = len(channels)
    if total_parsed == 0 and unparseable > 0:
        # The file parsed to zero entries but had content we could not read as
        # a playlist: a wrong path, an HTML error page, or a truncated
        # download.  That is bad input, and the documented contract is exit 2
        # for bad input - not a silent 0% "success" that would let CI pass.
        print(
            f"error        : {args.source} is not a readable M3U playlist "
            f"({unparseable} line(s) could not be parsed, 0 entries found)",
            file=sys.stderr,
        )
        return 2
    channels = select_channels(channels, args.include, args.exclude, args.limit)
    # Always report what the parser saw, not just what we are about to probe:
    # with --limit, "0 probed" and "0 found" are very different failures.
    print(
        f"parsed       : {total_parsed} entries, {unparseable} unparseable line(s)"
    )
    print(f"probing      : {len(channels)} channel(s)")
    print("")

    started = time.monotonic()
    results = probe_all(channels, cfg)
    elapsed = time.monotonic() - started

    print(render_table(channels, results, args.verbose))
    summary, percent = render_summary(channels, results)
    print(summary)
    print(f"  wall clock      : {elapsed:.1f}s for {len(channels)} channel(s)")

    if args.json_path:
        write_json(args.json_path, args.source, channels, results, percent, unparseable, cfg)
        print(f"\njson written : {args.json_path}")

    if args.fix_path:
        if not args.dry_run:
            source_real = os.path.realpath(args.source) if not urlparse(args.source).scheme else ""
            target_real = os.path.realpath(args.fix_path)
            if source_real and target_real == source_real:
                print("\nrefusing to write the fix over the input playlist", file=sys.stderr)
                return 2
        lines = split_lines_keepends(text)
        new_lines, log = plan_fixes(lines, channels, results, cfg)
        print(render_fix_log(log))
        diff = list(
            difflib.unified_diff(
                [line_body(item) for item in lines],
                [line_body(item) for item in new_lines],
                fromfile=f"{os.path.basename(args.source)} (original)",
                tofile=f"{os.path.basename(args.fix_path)} (fixed)",
                lineterm="",
                n=1,
            )
        )
        if diff:
            print("\nDiff")
            print("\n".join(diff))
        else:
            print("\nDiff\n  (no M3U-level remedy applies to this playlist)")
        if args.dry_run:
            print(
                f"\ndry-run: nothing written to disk. "
                f"Re-run with --confirm to write {args.fix_path}"
            )
        else:
            # newline="" disables platform newline translation and "".join
            # preserves each line's own terminator, so a no-op run reproduces
            # the input byte for byte instead of normalising CRLF to LF.
            with open(args.fix_path, "w", encoding="utf-8", newline="") as handle:
                handle.write("".join(new_lines))
            print(f"\nfix written  : {args.fix_path}")

    if args.fail_under is not None and percent < args.fail_under:
        print(f"\nFAIL: {percent:.1f}% working is below --fail-under {args.fail_under:g}%")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
