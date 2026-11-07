"""mail_dkim_set: Validierung, Vorschau, Ausführung und Kontrolle, ohne SSH."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

PREF = "What to do with mail sent to non-existent users:  'reject'\nDKIM Sign Outgoing Mail:                          {state}\nWWW: true\n"
DNS = "example.com. MX 10 mail.example.com.\ndefault._domainkey.example.com. TXT  v=DKIM1; p=ABC\n"


@pytest.fixture
def fake_ssh(monkeypatch):
    state = {"dkim": "false", "calls": []}

    def exec_(cmd, timeout=None):
        state["calls"].append(cmd)
        if "domain_pref --info" in cmd:
            return PREF.format(state=state["dkim"]), "", 0
        if "dns --info" in cmd:
            return (DNS if state["dkim"] == "true" else "example.com. MX 10 mail.example.com.\n"), "", 0
        return "", "", 1

    def run(cmd, timeout=None):
        state["calls"].append(cmd)
        if "-sign_outgoing_mail true" in cmd:
            state["dkim"] = "true"
        elif "-sign_outgoing_mail false" in cmd:
            state["dkim"] = "false"
        return "SUCCESS"

    monkeypatch.setattr(server, "_ssh_exec", exec_)
    monkeypatch.setattr(server, "ssh_run", run)
    monkeypatch.setattr(server, "_audit", lambda *a, **k: None)
    return state


def test_rejects_bad_domain():
    with pytest.raises(ValueError, match="Ungültiger Domainname"):
        server.mail_dkim_set("x.ch; rm -rf /", confirm=True)


def test_unknown_domain(monkeypatch):
    monkeypatch.setattr(server, "_ssh_exec", lambda cmd, timeout=None: ("", "Unable to find domain", 1))
    with pytest.raises(ValueError, match="nicht gefunden"):
        server.mail_dkim_set("example.com")


def test_preview_changes_nothing(fake_ssh):
    out = server.mail_dkim_set("Example.com")
    assert "Vorschau" in out and "false -> true" in out
    assert not any("--update" in c for c in fake_ssh["calls"])


def test_enable(fake_ssh):
    out = server.mail_dkim_set("example.com", confirm=True)
    assert "plesk bin domain_pref --update example.com -sign_outgoing_mail true" in fake_ssh["calls"]
    assert "jetzt true" in out and "default._domainkey" in out and "WARNUNG" not in out


def test_already_enabled(fake_ssh):
    fake_ssh["dkim"] = "true"
    out = server.mail_dkim_set("example.com", confirm=True)
    assert "bereits true" in out
    assert not any("--update" in c for c in fake_ssh["calls"])


def test_disable(fake_ssh):
    fake_ssh["dkim"] = "true"
    server.mail_dkim_set("example.com", enabled=False, confirm=True)
    assert "plesk bin domain_pref --update example.com -sign_outgoing_mail false" in fake_ssh["calls"]
