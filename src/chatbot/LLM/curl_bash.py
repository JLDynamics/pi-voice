"""Pi-style research: the model writes a bash command, usually ``curl``.

Pi has no web_search / web_fetch tools. It gives the model ``bash`` and the
model fetches pages itself (``curl -sL URL``, then strip HTML). This module
is that idea for Voice, behind two layers so a spoken assistant cannot run
arbitrary shell on the Mac.

``check_research_command`` is a first-pass screen: ``curl`` plus common text
filters (``python3``, ``head``, ``rg``, …) and simple ``for`` loops over
URLs, literal URLs must be public, and destructive words are refused. It is a
text filter, so it cannot see inside ``python3 -c`` or ``$(...)``.

The enforcement is the macOS sandbox the command runs under
(``_SANDBOX_PROFILE``): the kernel denies every file write outside the temp
directories and every connection to localhost. The screen never runs a
command the sandbox cannot wrap.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from chatbot.LLM.voice_prompt import format_now

# Spoken turns cannot wait on a hung curl. Pi leaves timeout optional; we cap it.
DEFAULT_TIMEOUT_S = 12.0
MAX_TIMEOUT_S = 30.0
MAX_OUTPUT_CHARS = 16_000
# DuckDuckGo HTML in the live log returned 11 chars and was treated as a
# successful search. Anything this short is a blocked page or an empty parse.
MIN_USEFUL_OUTPUT_CHARS = 40

# Search pages often reject the default curl UA. A browser UA is prepended
# unless the model already set one. --max-time is filled in per invocation so
# a hung TCP connect dies before the bash timeout.
_CURL_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_EMPTY_FETCH_HINT = (
    "That fetch returned almost no usable text (search pages often block curl "
    "or change their HTML). Retry a primary page (Wikipedia or an official site). "
    "For latest news, use a Google News RSS URL with when:1d and today's date."
)
_NEWS_WINDOW_HINT = (
    "This Google News URL has no when:1d window, so older follow-ups will appear. "
    "Retry with when:1d and today's calendar date in q=."
)
_BLOCKED_PAGE_MARKERS = (
    "enable javascript",
    "unusual traffic",
    "captcha",
    "access denied",
    "sorry, you have been blocked",
    "checking your browser",
)
_RFC822_DATE = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{1,2} "
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"\d{4} \d{2}:\d{2}:\d{2}(?: [A-Z]{2,5})?"
)
_FRESH_WHEN = re.compile(r"when:1[hd]", re.I)

_ALLOWED_BINARIES = frozenset(
    {
        "awk",
        "curl",
        "cut",
        "echo",
        "grep",
        "head",
        "printf",
        "python3",
        "rg",
        "sed",
        "sort",
        "tail",
        "tr",
        "uniq",
        "wc",
    }
)
_BLOCKED = re.compile(
    r"\b(rm|sudo|ssh|scp|sftp|dd|mkfs|chmod|chown|reboot|shutdown|kill|crontab|launchctl|osascript)\b",
    re.I,
)
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# Writes are limited to the temp directories (``/tmp`` resolves to
# ``/private/tmp``; ``$TMPDIR`` lives under ``/private/var/folders``) and the
# terminal/pipe devices a shell needs. Loopback is denied so a command cannot
# reach this backend or anything else listening on the Mac. Private LAN
# addresses are refused by the URL screen only: the sandbox cannot express
# them, so a ``curl -L`` redirect to one is not caught.
_SANDBOX_PROFILE = """\
(version 1)
(allow default)
(deny file-write*
  (require-not
    (require-any
      (subpath "/private/tmp")
      (subpath "/private/var/folders")
      (literal "/dev/null")
      (literal "/dev/tty")
      (regex #"^/dev/fd/"))))
(deny network-outbound (remote ip "localhost:*"))
"""
_REDIRECT_OUTSIDE_TMP = re.compile(r"(?:>>?|tee(?:\s+-a)?)\s+(?!/dev/null\b|/tmp/)")
_URL = re.compile(r"https?://[^\s\"'`]+", re.I)


class ResearchCommandError(ValueError):
    """The command is not a allowed research invocation."""


def run_research_command(
    command: str,
    timeout: float | None = None,
    *,
    now: datetime | None = None,
) -> str:
    """Run one model-written research command and return stdout+stderr text."""
    cleaned = (command or "").strip()
    if not cleaned:
        return "No command provided. Use curl to search or fetch a public page."
    try:
        check_research_command(cleaned)
    except ResearchCommandError as exc:
        return str(exc)
    seconds = DEFAULT_TIMEOUT_S if timeout is None else float(timeout)
    if seconds <= 0:
        seconds = DEFAULT_TIMEOUT_S
    seconds = min(seconds, MAX_TIMEOUT_S)
    if not os.path.exists(SANDBOX_EXEC):
        return "Research bash is unavailable: the macOS sandbox (sandbox-exec) was not found."
    try:
        completed = subprocess.run(
            [SANDBOX_EXEC, "-p", _SANDBOX_PROFILE, "/bin/bash", "-lc", _curl_wrapper(seconds) + cleaned],
            capture_output=True,
            text=True,
            timeout=seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return finalize_research_output(
            f"Command timed out after {seconds:.0f}s. Try a smaller page or a shorter pipeline.",
            command=cleaned,
            now=now,
        )
    except OSError as exc:
        return f"Could not run bash: {exc}"
    text = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode not in (0, None) and not text.strip():
        text = f"Command exited with code {completed.returncode}."
    elif completed.returncode not in (0, None):
        text = text.rstrip() + f"\n[exit {completed.returncode}]"
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS].rstrip() + "\n[... text cut here ...]"
    return finalize_research_output(text or "(no output)", command=cleaned, now=now)


def finalize_research_output(
    text: str,
    *,
    command: str = "",
    now: datetime | None = None,
) -> str:
    """Stamp the date, flag empty fetches, and warn when a news feed is undated."""
    now = now or datetime.now().astimezone()
    body = text.strip() or "(no output)"
    if _is_unusable_fetch(body):
        body = _EMPTY_FETCH_HINT
    if _is_news_command(command):
        notes = [
            f"[Checked at {format_now(now)}. For news, keep only items whose pubDate "
            "is in the last 24 hours; do not present older events as happening today.]"
        ]
        if _needs_news_window_hint(command):
            notes.append(_NEWS_WINDOW_HINT)
        freshness = _freshness_note(body, now)
        if freshness:
            notes.append(freshness)
    else:
        notes = [f"[Checked at {format_now(now)}. If this fact can change, treat the page as the source of truth.]"]
    return "\n".join(notes) + "\n" + body


def _curl_wrapper(seconds: float) -> str:
    curl_limit = max(1, int(seconds) - 1)
    return f"curl() {{ command curl -A '{_CURL_USER_AGENT}' --compressed --max-time {curl_limit} \"$@\"; }}; "


def _is_news_command(command: str) -> bool:
    lower = command.lower()
    return "news.google.com" in lower or "/rss" in lower or "rss/" in lower or "when:1" in lower


def _is_unusable_fetch(text: str) -> bool:
    stripped = text.strip()
    if stripped.startswith("Command ") or "timed out" in stripped:
        return False
    if not stripped or stripped == "(no output)":
        return True
    if len(stripped) < MIN_USEFUL_OUTPUT_CHARS:
        return True
    lower = stripped.lower()
    return any(marker in lower for marker in _BLOCKED_PAGE_MARKERS)


def _needs_news_window_hint(command: str) -> bool:
    return "news.google.com" in command.lower() and _FRESH_WHEN.search(command) is None


def _freshness_note(text: str, now: datetime) -> str:
    found: list[datetime] = []
    for match in _RFC822_DATE.findall(text):
        try:
            parsed = parsedate_to_datetime(match)
        except (TypeError, ValueError, OverflowError):
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        found.append(parsed)
    if len(found) < 2:
        return ""
    cutoff = now.astimezone(timezone.utc) - timedelta(hours=24)
    recent = sum(1 for stamp in found if stamp.astimezone(timezone.utc) >= cutoff)
    return (
        f"[Freshness: {recent} of {len(found)} dated items are from the last 24 hours. "
        "Speak only those as current news; older items are follow-ups, not happening today.]"
    )


def check_research_command(command: str) -> None:
    """Raise ``ResearchCommandError`` if this is not a safe curl-style command."""
    if _BLOCKED.search(command):
        raise ResearchCommandError("That command is not allowed. Research bash may use curl and text filters only.")
    if _REDIRECT_OUTSIDE_TMP.search(command):
        raise ResearchCommandError("Redirects are only allowed to /tmp or /dev/null.")
    if "curl" not in command:
        raise ResearchCommandError("Research bash must use curl to fetch a public URL.")
    for binary in _command_binaries(command):
        if binary not in _ALLOWED_BINARIES and binary != "for":
            raise ResearchCommandError(
                f"Command {binary!r} is not allowed. Use curl, python3, head, rg, or similar text filters."
            )
    urls = _URL.findall(command)
    if not urls:
        raise ResearchCommandError("No public http(s) URL found in the command.")
    for raw in urls:
        allowed, reason = is_public_http_url(_normalize_fetched_url(raw))
        if not allowed:
            raise ResearchCommandError(reason)


def _normalize_fetched_url(raw: str) -> str:
    """Trim trailing sentence punctuation without breaking Wikipedia ``(2024)`` paths."""
    url = raw.strip().rstrip(".,;'\"")
    while url.endswith(")") and url.count("(") < url.count(")"):
        url = url[:-1]
    return url


def is_public_http_url(raw_url: str) -> tuple[bool, str]:
    """Same idea as the sidecar fetch guard: no localhost, no RFC1918, no file:."""
    url = raw_url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False, "Only http and https URLs are allowed."
    host = (parsed.hostname or "").strip().rstrip(".")
    if not host:
        return False, "That URL has no host."
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return False, "Refusing to fetch a private or loopback address."
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, f"Could not resolve {host}."
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            return False, "Refusing to fetch a private or loopback address."
    return True, ""


def _command_binaries(command: str) -> list[str]:
    """First word of each pipeline / list segment (skip ``for x in`` headers)."""
    binaries: list[str] = []
    for segment in _split_command_segments(command):
        tokens = segment.split()
        if not tokens:
            continue
        if tokens[0] == "for" and "in" in tokens:
            # ``for url in a b; do curl ...; done`` — the body is a later segment.
            continue
        while tokens and tokens[0] in {"do", "done", "then", "else", "fi"}:
            tokens = tokens[1:]
        if not tokens:
            continue
        name = tokens[0].rsplit("/", 1)[-1]
        binaries.append(name)
    return binaries


def _split_command_segments(command: str) -> list[str]:
    """Split on ``|``, ``;``, ``&&``, ``||``, and newlines, but not inside quotes."""
    segments: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == quote and command[i - 1] != "\\":
                quote = ""
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\n" or ch == ";":
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        if ch == "|":
            if i + 1 < len(command) and command[i + 1] == "|":
                segments.append("".join(buf))
                buf = []
                i += 2
                continue
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        if ch == "&" and i + 1 < len(command) and command[i + 1] == "&":
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        buf.append(ch)
        i += 1
    if buf:
        segments.append("".join(buf))
    return segments
