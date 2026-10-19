"""customer_update_email: Validierung und confirm-Pflicht, ohne SSH."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


def test_rejects_bad_login():
    with pytest.raises(ValueError, match="Ungültiger Plesk-Login"):
        server.customer_update_email("x y;rm", "a@example.com", confirm=True)


def test_rejects_admin():
    with pytest.raises(ValueError, match="Admin-Account"):
        server.customer_update_email("admin", "a@example.com", confirm=True)


def test_rejects_bad_email():
    with pytest.raises(ValueError, match="Ungültige E-Mail"):
        server.customer_update_email("kunde", "keine-mail", confirm=True)


def test_preview_without_confirm(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "ssh_run", lambda cmd, timeout=None: calls.append(cmd) or "Email: alt@example.com")
    out = server.customer_update_email("kunde", "neu@example.com")
    assert "Vorschau" in out and "neu@example.com" in out
    assert len(calls) == 1 and "--info kunde" in calls[0]


def test_update_with_confirm(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "ssh_run", lambda cmd, timeout=None: calls.append(cmd) or "SUCCESS")
    server.customer_update_email("kunde", "neu@example.com", confirm=True)
    assert any("--update kunde -email neu@example.com" in c for c in calls)
