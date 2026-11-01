"""domain_create: Validierung, Vorschau und Kommando, ohne SSH."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

IPS = (
    "State Type IP                                     Clients Hosting PublicIP     \n"
    "0     S    ens192:192.0.2.10/255.255.255.224 0       140     203.0.113.3\n"
)
CUST = "Company name:                           Kunde AG\nDomains:                                1\n"


@pytest.fixture
def fake_ssh(monkeypatch):
    calls = []
    state = {"exists": False}

    def exec_(cmd, timeout=None):
        calls.append(cmd)
        if cmd.startswith("plesk db"):
            return "Standard 20GB\nUnlimited\n", "", 0
        if "ipmanage" in cmd:
            return IPS, "", 0
        if "customer --info kunde" in cmd:
            return CUST, "", 0
        if "customer --info" in cmd:
            return "", "not found", 1
        if cmd.startswith("id -u"):
            return ("1001", "", 0) if "vergeben" in cmd else ("", "no such user", 1)
        if "--info" in cmd:
            return ("info", "", 0) if state["exists"] else ("", "Unable to find", 1)
        return "SUCCESS", "", 0

    def run(cmd, timeout=None):
        calls.append(cmd)
        if "subscription --create" in cmd:
            state["exists"] = True
        return "SUCCESS"

    class Client:
        def close(self):
            pass

    def client_exec(client, cmd, timeout=None, stdin_data=None, max_runtime=None):
        calls.append(cmd)
        state["stdin"] = stdin_data
        if "subscription --create" in cmd:
            state["exists"] = True
        return "SUCCESS: Creation of domain 'example.ch' complete.", "", 0

    monkeypatch.setattr(server, "_ssh_exec", exec_)
    monkeypatch.setattr(server, "ssh_run", run)
    monkeypatch.setattr(server, "_ssh_connect", lambda: Client())
    monkeypatch.setattr(server, "_client_exec", client_exec)
    return calls, state


def test_rejects_bad_domain():
    with pytest.raises(ValueError, match="Ungültiger Domainname"):
        server.domain_create("x.ch; rm -rf /", "admin", "Standard 20GB")


def test_unknown_plan(fake_ssh):
    with pytest.raises(ValueError, match="Vorhanden: Standard 20GB, Unlimited"):
        server.domain_create("example.ch", "admin", "Gibts nicht")


def test_unknown_ip(fake_ssh):
    with pytest.raises(ValueError, match="keine Shared-IP"):
        server.domain_create("example.ch", "admin", "Standard 20GB", ip="198.51.100.1")


def test_unknown_customer(fake_ssh):
    with pytest.raises(ValueError, match="Kein Kundenaccount"):
        server.domain_create("example.ch", "fremd", "Standard 20GB")


def test_existing_domain(fake_ssh):
    fake_ssh[1]["exists"] = True
    with pytest.raises(ValueError, match="existiert in Plesk bereits"):
        server.domain_create("example.ch", "admin", "Standard 20GB", confirm=True)
    assert not any("--create" in c for c in fake_ssh[0])


def test_preview(fake_ssh):
    out = server.domain_create("Example.ch", "kunde", "Standard 20GB")
    assert "Vorschau" in out and "kunde (Kunde AG)" in out and "IP: 192.0.2.10" in out
    assert not any("--create" in c for c in fake_ssh[0])


def test_create(fake_ssh):
    out = server.domain_create("example.ch", "kunde", "Standard 20GB", confirm=True)
    assert (
        "plesk bin subscription --create example.ch -owner kunde -service-plan 'Standard 20GB'"
        " -ip 192.0.2.10 -hosting false -notify false"
    ) in fake_ssh[0]
    assert "Subscription vorhanden" in out
    assert "plesk bin dns --info example.ch" in fake_ssh[0]


def test_ftp_without_hosting(fake_ssh):
    with pytest.raises(ValueError, match="nur zusammen mit hosting=true"):
        server.domain_create("example.ch", "admin", "Standard 20GB", ftp_login="exuser")


def test_hosting_needs_login(fake_ssh):
    with pytest.raises(ValueError, match="ftp_login Pflicht"):
        server.domain_create("example.ch", "admin", "Standard 20GB", hosting=True)


def test_hosting_bad_login(fake_ssh):
    with pytest.raises(ValueError, match="Ungültiger FTP-Login"):
        server.domain_create("example.ch", "admin", "Standard 20GB", hosting=True, ftp_login="Ex user;")


def test_hosting_short_password(fake_ssh):
    with pytest.raises(ValueError, match="8-255 Zeichen"):
        server.domain_create("example.ch", "admin", "Standard 20GB", hosting=True,
                             ftp_login="exuser", ftp_password="kurz")


def test_hosting_login_taken(fake_ssh):
    with pytest.raises(ValueError, match="existiert auf dem Server bereits"):
        server.domain_create("example.ch", "admin", "Standard 20GB", hosting=True,
                             ftp_login="vergeben", confirm=True)
    assert not any("--create" in c for c in fake_ssh[0])


def test_hosting_preview(fake_ssh):
    out = server.domain_create("example.ch", "kunde", "Standard 20GB", hosting=True, ftp_login="exuser")
    assert "Vorschau" in out and "Webhosting (FTP-Login: exuser, Passwort: wird generiert)" in out
    assert not any("--create" in c for c in fake_ssh[0])


def test_hosting_create_generated_password(fake_ssh):
    calls, state = fake_ssh
    out = server.domain_create("example.ch", "kunde", "Standard 20GB", hosting=True,
                               ftp_login="exuser", confirm=True)
    cmd = next(c for c in calls if "--create" in c)
    assert cmd == (
        "IFS= read -r PSA_PASSWORD && export PSA_PASSWORD && "
        "plesk bin subscription --create example.ch -owner kunde -service-plan 'Standard 20GB'"
        " -ip 192.0.2.10 -hosting true -hst_type phys -login exuser -passwd '' -notify false"
    )
    pw = state["stdin"].decode().rstrip("\n")
    assert len(pw) == 20 and pw not in cmd
    assert f"FTP-Zugang: exuser / {pw}" in out and "Subscription vorhanden" in out


def test_hosting_create_given_password(fake_ssh):
    calls, state = fake_ssh
    out = server.domain_create("example.ch", "kunde", "Standard 20GB", hosting=True,
                               ftp_login="exuser", ftp_password="Geheim-12345", confirm=True)
    assert state["stdin"] == b"Geheim-12345\n"
    assert not any("Geheim-12345" in c for c in calls)
    assert "Geheim-12345" not in out
