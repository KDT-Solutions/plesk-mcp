"""subscription_create, domain_alias_create, db_create, extract_archive,
wp_db_import, wp_search_replace: Validierung und Vorschau, ohne SSH."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

IPS = (
    "State Type IP                                     Clients Hosting PublicIP     \n"
    "0     S    ens192:192.0.2.10/255.255.255.224 0       140     203.0.113.3\n"
)


@pytest.fixture
def fake_ssh(monkeypatch):
    calls = []
    state = {"domains": {"example.ch"}, "aliases": set(), "users": set()}

    def exec_(cmd, timeout=None, max_runtime=None):
        calls.append(cmd)
        if cmd.startswith("plesk db"):
            return "Standard 20GB\n", "", 0
        if "ipmanage" in cmd:
            return IPS, "", 0
        if cmd.startswith("id -u "):
            return ("1001", "", 0) if cmd.split()[-1] in state["users"] else ("", "no such user", 1)
        if "customer --info" in cmd:
            return "", "not found", 1
        if "domalias --info" in cmd:
            return ("info", "", 0) if cmd.split()[-1] in state["aliases"] else ("", "not found", 1)
        if "domain --info" in cmd:
            return ("info", "", 0) if cmd.split()[-1] in state["domains"] else ("", "Unable to find", 1)
        return "SUCCESS", "", 0

    def run(cmd, timeout=None, max_runtime=None):
        calls.append(cmd)
        return "SUCCESS"

    def no_connect():
        raise AssertionError("keine SSH-Verbindung in der Vorschau erwartet")

    monkeypatch.setattr(server, "_ssh_exec", exec_)
    monkeypatch.setattr(server, "ssh_run", run)
    monkeypatch.setattr(server, "_ssh_connect", no_connect)
    return calls, state


def test_default_sysuser():
    assert server._default_sysuser("examplemarble.ch") == "examplemarble"
    assert server._default_sysuser("my-very-long-domain-name.ch") == "myverylongdomain"
    assert server._default_sysuser("1a.ch") == "w1a"
    assert server._default_sysuser("a.ch") == "aweb"
    assert server._NEW_SYSUSER_RE.match(server._default_sysuser("x.ch"))


def test_subscription_preview(fake_ssh):
    out = server.subscription_create("New.ch", "admin", "Standard 20GB")
    assert "Vorschau" in out and "System-User: new" in out and "Webhosting" in out
    assert not any("--create" in c for c in fake_ssh[0])


def test_subscription_existing_sysuser(fake_ssh):
    fake_ssh[1]["users"].add("new")
    with pytest.raises(ValueError, match="existiert bereits"):
        server.subscription_create("new.ch", "admin", "Standard 20GB")


def test_subscription_bad_sysuser(fake_ssh):
    with pytest.raises(ValueError, match="Ungültiger system_user"):
        server.subscription_create("new.ch", "admin", "Standard 20GB", system_user="Root;x")


def test_subscription_existing_domain(fake_ssh):
    with pytest.raises(ValueError, match="existiert in Plesk bereits"):
        server.subscription_create("example.ch", "admin", "Standard 20GB")


def test_password_never_in_command():
    assert "PSA_PASSWORD" not in server._GEN_PW_BASH
    assert "/dev/urandom" in server._GEN_PW_BASH


def test_alias_preview(fake_ssh):
    out = server.domain_alias_create("example.ch", "example.net")
    assert "Vorschau" in out and "301-Weiterleitung auf example.ch: ja" in out
    assert not any("domalias --create" in c for c in fake_ssh[0])


def test_alias_create(fake_ssh):
    out = server.domain_alias_create("example.ch", "example.net", confirm=True)
    cmd = next(c for c in fake_ssh[0] if "domalias --create" in c)
    assert "-domain example.ch" in cmd and "-seo-redirect true" in cmd and "-mail false" in cmd
    assert "Kontrolle" in out


def test_alias_checks(fake_ssh):
    with pytest.raises(ValueError, match="identisch"):
        server.domain_alias_create("example.ch", "example.ch")
    with pytest.raises(ValueError, match="nicht vorhanden"):
        server.domain_alias_create("missing.ch", "example.net")
    fake_ssh[1]["aliases"].add("example.net")
    with pytest.raises(ValueError, match="existiert bereits"):
        server.domain_alias_create("example.ch", "example.net")


@pytest.mark.parametrize("db,user,cfg,msg", [
    ("bad-name", "u1", "httpdocs/wp-config.php", "Datenbankname"),
    ("db1", "u;x", "httpdocs/wp-config.php", "DB-Benutzer"),
    ("db1", "u1", "httpdocs/config.php", "wp-config.php"),
    ("db1", "u1", "../wp-config.php", "Ungültiger Pfad"),
])
def test_db_create_validation(db, user, cfg, msg):
    with pytest.raises(ValueError, match=msg):
        server.db_create("example.ch", db, user, cfg)


def test_extract_args():
    with pytest.raises(ValueError, match="path oder parts"):
        server.extract_archive("example.ch", "httpdocs")
    with pytest.raises(ValueError, match="nicht beides"):
        server.extract_archive("example.ch", "httpdocs", path="a.zip", parts=["a.z1"])


@pytest.mark.parametrize("old,new,msg", [
    ("", "x", "nicht leer"),
    ("a", "a", "identisch"),
    ("a\nb", "x", "Zeilenumbrüche"),
    ("a" * 501, "x", "500"),
])
def test_search_replace_validation(old, new, msg):
    with pytest.raises(ValueError, match=msg):
        server.wp_search_replace("example.ch", "httpdocs", old, new)


def test_customer_create_preview(fake_ssh):
    out = server.customer_create("newcust", "New Customer", "info@example.ch", company="New AG")
    assert "Vorschau" in out and "Login: newcust" in out
    assert not any("customer --create" in c for c in fake_ssh[0])


@pytest.mark.parametrize("login,email,msg", [
    ("admin", "a@example.ch", "Ungültiger Login"),
    ("Bad Login", "a@example.ch", "Ungültiger Login"),
    ("newcust", "kein-mail", "E-Mail"),
])
def test_customer_create_validation(login, email, msg):
    with pytest.raises(ValueError, match=msg):
        server.customer_create(login, "Name", email)


def test_ssl_preview(fake_ssh):
    fake_ssh[1]["aliases"].add("example.net")
    out = server.ssl_letsencrypt("example.ch", "web@example.ch", aliases=["example.net"])
    assert "example.ch, www.example.ch, example.net, www.example.net" in out
    assert not any("letsencrypt" in c for c in fake_ssh[0])


def test_ssl_unknown_alias(fake_ssh):
    with pytest.raises(ValueError, match="kein Domain-Alias"):
        server.ssl_letsencrypt("example.ch", "web@example.ch", aliases=["other.net"])
