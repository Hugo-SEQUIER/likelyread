"""CLI tests: pure helpers, then the real commands against a real server process."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "cli"))
import likelyread as lr  # noqa: E402

TOKEN = "clitesttoken0123456789abcdef0123"
GMAIL_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/42.0.2311.135 Safari/537.36 Edge/12.246 Mozilla/5.0"


# ------------------------------------------------------------ pure helpers


def test_text_to_html_keeps_the_copy_and_appends_the_pixel():
    text = "Hello Maya,\n\nSee https://example.com/portfolio\n\nBest,\nSam <me>\n"
    out = lr.text_to_html(text, lr.pixel_tag("https://t.x.com", "abc"))
    assert out.count("<p>") == 3
    assert '<a href="https://example.com/portfolio">https://example.com/portfolio</a>' in out
    assert "Best,<br>Sam &lt;me&gt;" in out
    assert out.rstrip().endswith('style="display:none!important">\n</div>')


def test_linkify_leaves_sentence_punctuation_outside_the_link():
    out = lr.text_to_html("Visit https://a.com/x.")
    assert '<a href="https://a.com/x">https://a.com/x</a>.' in out


def test_parse_time_accepts_seconds_gmail_ms_and_iso():
    assert lr.parse_time("1792251301") == 1792251301
    assert lr.parse_time("1792251301849") == pytest.approx(1792251301.849)
    assert lr.parse_time("2026-10-19T11:00:00+09:00") == pytest.approx(1792375200)
    with pytest.raises(lr.CliError):
        lr.parse_time("2026-10-19T11:00:00")


def test_split_addresses():
    assert lr.split_addresses(["A@x.com, b@y.com", "<c@z.com>", "a@x.com"]) == ["a@x.com", "b@y.com", "c@z.com"]


def test_config_env_overrides_file(tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"url": "https://file.example/", "token": "filetoken"}))
    cfg = lr.load_config({"LIKELYREAD_CONFIG": str(f), "LIKELYREAD_API_URL": "http://127.0.0.1:1/"})
    assert cfg == {"url": "https://file.example", "api_url": "http://127.0.0.1:1", "token": "filetoken"}


def test_unconfigured_is_a_clear_error(tmp_path, capsys, monkeypatch):
    for k in ("LIKELYREAD_URL", "LIKELYREAD_API_URL", "LIKELYREAD_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LIKELYREAD_CONFIG", str(tmp_path / "missing.json"))
    assert lr.main(["pixel"]) == 1
    assert "LIKELYREAD_URL" in capsys.readouterr().err


# ------------------------------------------------------------ live server


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    db = tmp_path_factory.mktemp("db") / "t.db"
    env = {**os.environ, "LIKELYREAD_TOKEN": TOKEN, "DB_PATH": str(db), "DASHBOARD": "false"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:create_app", "--factory", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT / "server", env=env,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/healthz", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    yield base
    proc.terminate()
    proc.wait(timeout=10)


def run(server, *args, token=TOKEN):
    env = {**os.environ, "LIKELYREAD_URL": server, "LIKELYREAD_TOKEN": token, "LIKELYREAD_CONFIG": "/nonexistent"}
    env.pop("LIKELYREAD_API_URL", None)
    p = subprocess.run([sys.executable, str(ROOT / "cli" / "likelyread.py"), *args], capture_output=True, text=True, env=env, timeout=30)
    return p.returncode, (json.loads(p.stdout) if p.stdout.strip().startswith(("{", "[")) else p.stdout), p.stderr


def load_pixel(url, ua=GMAIL_UA):
    urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": ua}), timeout=5).read()


def test_ping_and_bad_token(server):
    assert run(server, "ping")[0] == 0
    code, _, err = run(server, "ping", token="wrong")
    assert code == 1 and "401" in err


def test_agent_flow_pixel_then_track_after_send(server):
    # (A hit before the send time counting as a preview is covered in server tests.)
    _, px, _ = run(server, "pixel")
    sent_at = time.time() - 60  # the send happened a minute ago
    code, tr, _ = run(server, "track", "--id", px["id"], "--subject", "Proposal", "--to", "Maya@Harbourline.co",
                      "--campaign", "Q4 outreach", "--from", "Sam@Example.com", "--sent-at", str(int(sent_at * 1000)))
    assert code == 0 and tr["registered"] and tr["age_seconds"] >= 59
    load_pixel(px["url"])  # the recipient opens it
    _, e, _ = run(server, "show", px["id"])
    assert e["sender"] == "sam@example.com"
    assert e["recipients"] == ["maya@harbourline.co"]
    assert e["opens"] == 1 and e["self"] == 0
    _, rows, _ = run(server, "ls", "--campaign", "Q4 outreach", "--status", "opened")
    assert [r["id"] for r in rows] == [px["id"]]
    _, camps, _ = run(server, "campaigns")
    assert any(c["name"] == "Q4 outreach" and c["opened"] == 1 for c in camps)
    _, st, _ = run(server, "stats", "--from", "sam@example.com")
    assert st["emails"] == 1 and st["opened"] == 1


def test_track_without_id_creates_one(server):
    code, tr, _ = run(server, "track", "--subject", "Hi", "--to", "a@b.com")
    assert code == 0 and tr["img"].startswith('<img src="' + server + "/p/" + tr["id"])


def test_html_command_raw(server):
    env = {**os.environ, "LIKELYREAD_URL": server, "LIKELYREAD_TOKEN": TOKEN, "LIKELYREAD_CONFIG": "/nonexistent"}
    p = subprocess.run([sys.executable, str(ROOT / "cli" / "likelyread.py"), "html", "--raw", "--id", "abc"],
                       input="Hello\n\nBye", capture_output=True, text=True, env=env)
    assert p.returncode == 0
    assert "<p>Hello</p>" in p.stdout and f"{server}/p/abc.gif" in p.stdout


def test_set_campaign(server):
    _, tr, _ = run(server, "track", "--subject", "Move me", "--campaign", "A")
    run(server, "set-campaign", tr["id"], "B")
    _, e, _ = run(server, "show", tr["id"])
    assert e["campaign"] == "B"
