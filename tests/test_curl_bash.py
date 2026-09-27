import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch

import pytest

from chatbot.LLM.curl_bash import (
    _SANDBOX_PROFILE,
    SANDBOX_EXEC,
    ResearchCommandError,
    check_research_command,
    finalize_research_output,
    run_research_command,
)
from chatbot.LLM.server_tools import ServerToolExecutor

NOW = datetime(2026, 9, 9, 15, 50, tzinfo=timezone(timedelta(hours=-6), "MDT"))


def test_allows_pi_style_curl_and_html_strip():
    check_research_command(
        "curl -sL -A 'Mozilla/5.0' 'https://example.com' | python3 -c "
        '\'import sys,re; t=sys.stdin.read(); print(re.sub(r"<[^>]+>", " ", t)[:500])\''
    )


def test_allows_a_url_loop_like_pi():
    check_research_command(
        'for url in "https://example.com" "https://example.org"; do curl -sL "$url" | head -c 200; done'
    )


def test_rejects_rm_and_private_hosts():
    with pytest.raises(ResearchCommandError, match="not allowed"):
        check_research_command("curl -sL https://example.com && rm -rf /")
    with pytest.raises(ResearchCommandError, match="private or loopback"):
        check_research_command("curl -sL http://127.0.0.1:8766/health")
    with pytest.raises(ResearchCommandError, match="private or loopback"):
        check_research_command("curl -sL http://localhost/secret")


def test_rejects_commands_without_curl():
    with pytest.raises(ResearchCommandError, match="must use curl"):
        check_research_command("ls -la")


def test_rejects_disallowed_binary_inside_a_for_do_body():
    with pytest.raises(ResearchCommandError, match="wget"):
        check_research_command("curl -sL https://example.com; wget https://example.com")


def test_run_returns_validator_error_without_spawning():
    with patch("chatbot.LLM.curl_bash.subprocess.run") as spawn:
        output = run_research_command("rm -rf /")
    spawn.assert_not_called()
    assert "not allowed" in output


def test_run_truncates_and_reports_timeout(monkeypatch):
    class Fake:
        stdout = "x" * 20_000
        stderr = ""
        returncode = 0

    monkeypatch.setattr("chatbot.LLM.curl_bash.subprocess.run", lambda *a, **k: Fake())
    text = run_research_command("curl -sL https://example.com", now=NOW)
    assert "[... text cut here ...]" in text
    assert "Wednesday, September 9, 2026" in text


def test_executor_bash_runs_the_research_command():
    with patch("chatbot.LLM.server_tools.run_research_command", return_value="Example Domain") as run:
        output = ServerToolExecutor().run("bash", '{"command": "curl -sL https://example.com"}')
    run.assert_called_once()
    assert output == "Example Domain"
    assert ServerToolExecutor.handles("bash")


def test_run_injects_a_browser_user_agent(monkeypatch):
    seen: dict[str, list[str]] = {}

    class Fake:
        stdout = "Example Domain is for use in documentation examples. " * 3
        stderr = ""
        returncode = 0

    def fake_run(args, **kwargs):
        seen["args"] = args
        return Fake()

    monkeypatch.setattr("chatbot.LLM.curl_bash.subprocess.run", fake_run)
    run_research_command("curl -sL https://example.com", now=NOW)
    assert seen["args"][:2] == [SANDBOX_EXEC, "-p"]
    script = seen["args"][-1]
    assert "AppleWebKit/537.36" in script
    assert "--compressed" in script
    assert "--max-time" in script
    assert script.endswith("curl -sL https://example.com")


def test_empty_search_html_tells_the_model_to_retry_rss(monkeypatch):
    class Fake:
        stdout = "\n"
        stderr = ""
        returncode = 0

    monkeypatch.setattr("chatbot.LLM.curl_bash.subprocess.run", lambda *a, **k: Fake())
    text = run_research_command(
        "curl -s 'https://html.duckduckgo.com/html/?q=AI+news+September+2026'",
        now=NOW,
    )
    assert "almost no usable text" in text
    assert "Wikipedia" in text
    assert "when:1d" in text
    assert "Wednesday, September 9, 2026" in text


def test_google_news_without_when_gets_a_freshness_window_hint():
    body = (
        "OpenAI claims a Millennium Prize\n"
        "Wed, 09 Sep 2026 18:00:00 GMT\n"
        "Follow-up on last year's story\n"
        "Mon, 01 Sep 2025 12:00:00 GMT\n"
    )
    text = finalize_research_output(
        body,
        command="curl -sL 'https://news.google.com/rss/search?q=artificial+intelligence&hl=en-US'",
        now=NOW,
    )
    assert "no when:1d window" in text
    assert "1 of 2 dated items are from the last 24 hours" in text
    assert "do not present older events as happening today" in text


def test_dated_google_news_rss_skips_the_window_hint():
    text = finalize_research_output(
        "Reuters: something happened\nWed, 09 Sep 2026 18:00:00 GMT\n"
        "AP: another thing\nWed, 09 Sep 2026 17:00:00 GMT\n",
        command="curl -sL 'https://news.google.com/rss/search?q=world+news+when:1d+September+9+2026'",
        now=NOW,
    )
    assert "no when:1d window" not in text
    assert "2 of 2 dated items are from the last 24 hours" in text


def test_fact_pages_are_not_lectured_about_news_pubdates():
    text = finalize_research_output(
        "Incumbent Donald Trump since January 20, 2025. The president is elected.\n",
        command="curl -sL 'https://en.wikipedia.org/wiki/President_of_the_United_States'",
        now=NOW,
    )
    assert "treat the page as the source of truth" in text
    assert "pubDate" not in text
    assert "when:1d" not in text


def test_keeps_parentheses_in_wikipedia_paths():
    check_research_command("curl -sL 'https://en.wikipedia.org/wiki/Something_(2024)'")


def test_validator_errors_are_not_date_stamped():
    with patch("chatbot.LLM.curl_bash.subprocess.run") as spawn:
        output = run_research_command("rm -rf /", now=NOW)
    spawn.assert_not_called()
    assert "not allowed" in output
    assert "Checked at" not in output


def test_live_google_news_rss_keeps_today_in_view():
    today = datetime.now().astimezone()
    query = today.strftime("%B+%d+%Y")
    command = (
        "curl -sL "
        f"'https://news.google.com/rss/search?q=world+news+when:1d+{query}&hl=en-US&gl=US&ceid=US:en' "
        '| python3 -c "import sys,re; t=sys.stdin.read(); '
        "print('items', len(re.findall(r'<item>', t))); print(t[:3000])\""
    )
    text = run_research_command(command, timeout=20, now=today)
    if any(marker in text for marker in ("Could not resolve", "timed out", "almost no usable text")):
        pytest.skip(text[:240])
    assert "Checked at" in text
    assert str(today.year) in text
    assert "no when:1d window" not in text
    assert "items 0" not in text


def test_live_wikipedia_office_holder_page_is_current():
    command = (
        "curl -sL 'https://en.wikipedia.org/wiki/President_of_the_United_States' "
        "| python3 -c \"import sys,re; t=re.sub(r'<[^>]+>', ' ', sys.stdin.read()); "
        "t=re.sub(r'\\s+', ' ', t); i=t.lower().find('incumbent'); print(t[i:i+240] if i>=0 else t[:400])\""
    )
    text = run_research_command(command, timeout=15)
    if any(marker in text for marker in ("Could not resolve", "timed out", "almost no usable text")):
        pytest.skip(text[:240])
    assert "Checked at" in text
    assert "pubDate" not in text
    assert "trump" in text.casefold()


def test_screen_lets_through_writes_the_sandbox_must_stop():
    """The text screen is not the enforcement: these pass it, so only the sandbox stops them."""
    for command in (
        "curl -sL -o /Users/someone/x https://1.1.1.1/",
        "curl -sL https://1.1.1.1/ | python3 -c \"open('/Users/someone/x','w')\"",
        "curl -sL https://1.1.1.1/ $(touch /Users/someone/x)",
    ):
        check_research_command(command)


def test_run_refuses_without_the_sandbox(monkeypatch):
    monkeypatch.setattr("chatbot.LLM.curl_bash.os.path.exists", lambda path: False)
    with patch("chatbot.LLM.curl_bash.subprocess.run") as spawn:
        output = run_research_command("curl -sL https://example.com", now=NOW)
    spawn.assert_not_called()
    assert "sandbox" in output


def _sandboxed(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [SANDBOX_EXEC, "-p", _SANDBOX_PROFILE, "/bin/bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox only")
def test_sandbox_blocks_writes_outside_temp():
    target = Path.home() / f".chatbot-sandbox-probe-{uuid.uuid4().hex}"
    try:
        _sandboxed(f"python3 -c \"open('{target}','w').write('x')\"")
        _sandboxed(f"touch '{target}'")
        _sandboxed(f"echo x > '{target}'")
        assert not target.exists()
    finally:
        target.unlink(missing_ok=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox only")
def test_sandbox_allows_temp_writes(tmp_path):
    target = tmp_path / "ok.txt"
    result = _sandboxed(
        f"echo hi > '{target}' && cat '{target}' && echo hi > /tmp/chatbot-sandbox-ok-$$ && rm /tmp/chatbot-sandbox-ok-$$"
    )
    assert result.returncode == 0, result.stderr
    assert target.read_text().strip() == "hi"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox only")
def test_sandbox_blocks_loopback():
    server = HTTPServer(("127.0.0.1", 0), SimpleHTTPRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    try:
        # Reachable outside the sandbox, so a failure inside it is the sandbox.
        outside = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", url], capture_output=True, text=True
        )
        assert outside.stdout == "200"
        inside = _sandboxed(f"curl -s -o /dev/null -w '%{{http_code}}' {url}")
        assert inside.stdout != "200"
    finally:
        server.shutdown()
