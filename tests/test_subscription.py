"""subscription_suspend/activate/delete: Validierung, Vorschau und Rückbestätigung, ohne SSH."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

INFO = (
    "Domain name:                            example.com\n"
    "Owner's contact name:                   Kunde (kunde)\n"
    "Domain status:                          OK\n"
    "Size:                                   47.4 MB\n"
    "Mailboxes:                              2\n"
)


@pytest.fixture
def fake_ssh(monkeypatch):
    calls = []

    def exec_(cmd, timeout=None):
        calls.append(cmd)
        if "--info" in cmd:
            return INFO, "", 0
        return "SUCCESS", "", 0

    monkeypatch.setattr(server, "_ssh_exec", exec_)
    monkeypatch.setattr(server, "ssh_run", lambda cmd, timeout=None: calls.append(cmd) or "SUCCESS")
    return calls


def test_rejects_bad_domain():
    with pytest.raises(ValueError, match="Ungültiger Domainname"):
        server.subscription_suspend("x.ch; rm -rf /", confirm=True)


def test_not_a_subscription(monkeypatch):
    monkeypatch.setattr(server, "_ssh_exec", lambda cmd, timeout=None: ("", "Unable to find domain", 1))
    with pytest.raises(ValueError, match="Keine Subscription"):
        server.subscription_suspend("example.com")


def test_suspend_preview(fake_ssh):
    out = server.subscription_suspend("Example.com")
    assert "Vorschau" in out and "Domain status: OK" in out
    assert not any("--webspace-off" in c for c in fake_ssh)


def test_suspend_and_activate(fake_ssh):
    server.subscription_suspend("example.com", confirm=True)
    server.subscription_activate("example.com", confirm=True)
    assert "plesk bin subscription --webspace-off example.com" in fake_ssh
    assert "plesk bin subscription --webspace-on example.com" in fake_ssh


def test_delete_preview(fake_ssh):
    out = server.subscription_delete("example.com")
    assert "ENDGÜLTIG" in out and "Mailboxes: 2" in out
    assert not any("--remove" in c for c in fake_ssh)


@pytest.mark.parametrize("echo", ["", "example.org", "www.example.com"])
def test_delete_needs_matching_domain(fake_ssh, echo):
    with pytest.raises(ValueError, match="Rückbestätigung"):
        server.subscription_delete("example.com", confirm_domain=echo, confirm=True)
    assert not any("--remove" in c for c in fake_ssh)


def test_delete_with_confirmation(fake_ssh):
    out = server.subscription_delete("example.com", confirm_domain="Example.com", confirm=True)
    assert "plesk bin subscription --remove example.com" in fake_ssh
    assert "NOCH VORHANDEN" in out  # Fake liefert --info weiterhin mit exit 0
