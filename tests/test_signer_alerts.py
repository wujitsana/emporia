"""signer_alerts: config, Telegram send, safety, the hourly cap. No network."""

import stat
import threading

import pytest

from emporia import signer, signer_alerts as sa

TOKEN = "123456789:" + "A" * 35


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body if body is not None else {"ok": True}

    def json(self):
        return self._body


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(tmp_path / "signer"))
    sa._SENT.clear()
    sa._CAPPED.clear()
    calls = []
    reply = {"r": Resp()}

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json, timeout))
        return reply["r"]

    monkeypatch.setattr(sa.httpx, "post", fake_post)
    return calls, reply


def audit_lines():
    return signer.audit_rows()


def test_config_is_private_and_describe_hides_token(env):
    sa.set_telegram(TOKEN, "-100123")
    path = signer.signer_dir() / "alerts.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sa.channels() == ["telegram"]
    shown = sa.describe()
    assert TOKEN not in str(shown) and shown["channels"]["telegram"]["chat_id"] == "-100123"
    sa.clear("telegram")
    assert sa.channels() == []


@pytest.mark.parametrize("token,chat", [("nope", "1"), (TOKEN, "abc"), (TOKEN, "12 3")])
def test_bad_values_refused_without_echo(env, token, chat):
    with pytest.raises(ValueError) as e:
        sa.set_telegram(token, chat)
    assert token not in str(e.value)
    assert sa.channels() == []


def test_notify_sync_posts_and_audits(env):
    calls, _ = env
    sa.set_telegram(TOKEN, "@mychan")
    out = sa.notify_sync("alpha", "refused", "asked to be paid at 0xabc")
    assert out == [("telegram", True, "sent")]
    url, body, timeout = calls[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert body["chat_id"] == "@mychan" and body["disable_web_page_preview"] is True
    assert body["text"] == "Emporia signer · alpha · refused: asked to be paid at 0xabc"
    assert timeout == 10
    assert audit_lines()[-1]["result"] == "sent"


def test_telegram_error_reported_audited_not_raised(env):
    calls, reply = env
    reply["r"] = Resp(400, {"ok": False, "description": "chat not found"})
    sa.set_telegram(TOKEN, "1")
    assert sa.notify_sync("alpha", "refused", "x") == [("telegram", False, "chat not found")]
    last = audit_lines()[-1]
    assert last["result"] == "error" and last["why"] == "chat not found" and TOKEN not in str(last)
    done = threading.Event()
    orig = sa._record
    sa._record = lambda *a: (orig(*a), done.set())
    try:
        sa.notify("alpha", "refused", "x")
        assert done.wait(5)
    finally:
        sa._record = orig


def test_secret_looking_text_stripped(env):
    calls, _ = env
    sa.set_telegram(TOKEN, "1")
    sa.notify_sync("alpha", "refused", "key " + "ab" * 40 + " and sk_live_abc123 whsec_xyz " + "z" * 500)
    text = calls[0][1]["text"]
    assert "ab" * 40 not in text and "sk_live" not in text and "whsec_" not in text
    assert len(text) < 400


def test_hourly_cap(env):
    calls, _ = env
    sa.set_telegram(TOKEN, "1")
    for _ in range(30):
        sa.notify_sync("alpha", "refused", "x")
    assert len(calls) == 21
    assert "more refused" in calls[-1][1]["text"] and "emporia signer log" in calls[-1][1]["text"]
    sa.notify_sync("beta", "refused", "x")
    assert len(calls) == 22


def test_no_channels_sends_nothing(env):
    calls, _ = env
    assert sa.notify_sync("alpha", "refused", "x") == []
    sa.notify("alpha", "refused", "x")
    assert calls == []


def test_notify_never_raises_on_bad_event(env):
    sa.notify("alpha", "bogus", "x")
