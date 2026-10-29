"""P254: Settings says when an agent's identity key changed, and whether it has a recovery key."""
import httpx

from emporia import cli


def _item(group, label, value, about, command="", level="ok", person_only=False):
    return {"group": group, "label": label, "value": value, "about": about, "command": command,
            "level": level, "person_only": person_only}


def _serve(monkeypatch, status: int, body: dict) -> None:
    monkeypatch.setattr(httpx, "get", lambda url, timeout=3: httpx.Response(status, json=body))


def test_a_rotated_and_recovered_key_is_shown_with_its_old_files(monkeypatch, tmp_path):
    (tmp_path / "beta.priv").write_text("k")
    (tmp_path / "beta.priv.rotated-20261029T173753Z").write_text("k")
    _serve(monkeypatch, 200, {"key_log": [
        {"by": "key", "at": "2026-10-29T17:37:53.000001+00:00"},
        {"by": "commit", "commitment": "ab" * 32, "at": "2026-10-29T18:00:00+00:00"},
        {"by": "recovery", "at": "2026-10-29T19:00:00+00:00"},
    ]})
    changes, recovery = cli._key_history_item(_item, "http://relay.test", "beta", "beta", tmp_path)
    assert changes["value"] == "recovered 2026-10-29 19:00 UTC (2 changes)"
    assert "beta.priv.rotated-20261029T173753Z" in changes["about"] and changes["person_only"]
    # the commitment was spent by the recovery: none is set now
    assert recovery["value"] == "none" and recovery["level"] == "warn"
    assert recovery["command"] == "emporia agent recovery-key beta --out FILE"


def test_an_agent_that_never_changed_key_and_has_a_recovery_key(monkeypatch, tmp_path):
    _serve(monkeypatch, 200, {"key_log": [{"by": "commit", "commitment": "ab" * 32, "at": "2026-10-29T18:00:00+00:00"}]})
    changes, recovery = cli._key_history_item(_item, "http://relay.test", "alpha", "alpha", tmp_path)
    assert changes["value"] == "never changed"
    assert recovery["value"] == "set" and recovery["level"] == "ok" and recovery["command"] == ""


def test_a_relay_that_does_not_answer_says_so(monkeypatch, tmp_path):
    def down(url, timeout=3):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", down)
    [only] = cli._key_history_item(_item, "http://relay.test", "alpha", "alpha", tmp_path)
    assert only["value"] == "relay not answering"
