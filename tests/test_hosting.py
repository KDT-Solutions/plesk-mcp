"""Tests für hosting_settings/set_php_handler (ohne Plesk-Server).

SSH wird durch einen Fake ersetzt, der die Plesk-Kommandos beantwortet.
Ausführen: pip install -r requirements.txt pytest && pytest
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

HANDLERS = [
    {"id": "plesk-php82-fpm-dedicated", "fullVersion": "8.2.34", "version": "8.2", "type": "fpm",
     "clipath": "/opt/plesk/php/8.2/bin/php", "status": "enabled"},
    {"id": "plesk-php83-fpm-dedicated", "fullVersion": "8.3.35", "version": "8.3", "type": "fpm",
     "clipath": "/opt/plesk/php/8.3/bin/php", "status": "enabled"},
    {"id": "plesk-php83-fastcgi", "fullVersion": "8.3.35", "version": "8.3", "type": "fastcgi",
     "clipath": "/opt/plesk/php/8.3/bin/php", "status": "disabled"},
]

PHP_SETTINGS = """General settings:
memory_limit_dummy = x
display_errors = 
Performance settings:
memory_limit = 256M
max_execution_time = 30
PHP-FPM settings:
pm = ondemand
Additional directives:"""

SITE_INFO = """General
=============================
Domain name:                            example.com

Hosting
=============================
Hosting type:                           Physical hosting
FTP Login:                              kunde
FTP Password:                           ************
SSL/TLS support:                        On
Permanent SEO-safe 301 redirect from HTTP to HTTPS:On
PHP support:                            Yes

Mail Accounts
=============================
Mail service:                           Off

--WWW-Root--: /var/www/vhosts/example.com/httpdocs
"""


class FakeClient:
    def close(self):
        pass


@pytest.fixture
def plesk(monkeypatch):
    state = {"handler": "plesk-php82-fpm-dedicated", "calls": [], "update_rc": 0}

    def exec_(client, cmd, timeout=None, stdin_data=None):
        state["calls"].append(cmd)
        if cmd.startswith("plesk db"):
            if "'example.com'" in cmd:
                return state["handler"] + "\n", "", 0
            return "", "", 0
        if cmd.startswith("plesk bin php_handler --list"):
            return json.dumps(HANDLERS), "", 0
        if cmd.startswith("plesk bin site --show-php-settings"):
            return PHP_SETTINGS, "", 0
        if cmd.startswith("plesk bin site --info"):
            return SITE_INFO, "", 0
        if cmd.startswith("plesk bin site --update"):
            if state["update_rc"] == 0:
                state["handler"] = cmd.split()[-1]
                return "SUCCESS: Update of domain 'example.com' completed.", "", 0
            return "", "An error occurred", state["update_rc"]
        raise AssertionError(cmd)

    monkeypatch.setattr(server, "_ssh_connect", lambda: FakeClient())
    monkeypatch.setattr(server, "_client_exec", exec_)
    return state


def updates(state):
    return [c for c in state["calls"] if "--update" in c]


def test_hosting_settings(plesk):
    res = json.loads(server.hosting_settings("example.com"))
    assert res["php_handler"] == {"id": "plesk-php82-fpm-dedicated", "version": "8.2.34", "typ": "fpm",
                                  "status": "enabled"}
    assert res["php_einstellungen"]["Performance settings"]["memory_limit"] == "256M"
    assert res["php_einstellungen"]["General settings"]["display_errors"] == ""
    assert res["php_einstellungen"]["PHP-FPM settings"]["pm"] == "ondemand"
    assert res["hosting"]["SSL/TLS support"] == "On"
    assert res["hosting"]["Permanent SEO-safe 301 redirect from HTTP to HTTPS"] == "On"
    assert res["hosting"]["WWW-Root"] == "/var/www/vhosts/example.com/httpdocs"
    assert not any(k.lower().startswith("ftp") for k in res["hosting"])
    assert "Mail service" not in res["hosting"]
    assert [h["id"] for h in res["verfuegbare_php_handler"]] == [
        "plesk-php82-fpm-dedicated", "plesk-php83-fpm-dedicated"]
    assert updates(plesk) == []


def test_preview_without_confirm(plesk):
    res = json.loads(server.set_php_handler("example.com", "plesk-php83-fpm-dedicated"))
    assert "Vorschau" in res["ergebnis"] and res["versionswechsel"] and not res["typwechsel"]
    assert updates(plesk) == [] and plesk["handler"] == "plesk-php82-fpm-dedicated"


def test_change_with_confirm(plesk):
    res = json.loads(server.set_php_handler("example.com", "plesk-php83-fpm-dedicated", confirm=True))
    assert updates(plesk) == ["plesk bin site --update example.com -php_handler_id plesk-php83-fpm-dedicated"]
    assert plesk["handler"] == "plesk-php83-fpm-dedicated"
    assert res["alt"]["id"] == "plesk-php82-fpm-dedicated" and "plesk-php82-fpm-dedicated" in res["ergebnis"]


def test_same_handler_noop(plesk):
    res = json.loads(server.set_php_handler("example.com", "plesk-php82-fpm-dedicated", confirm=True))
    assert "bereits" in res["ergebnis"] and updates(plesk) == []


@pytest.mark.parametrize("hid", ["plesk-php83-fastcgi", "plesk-php99-fpm"])
def test_disabled_or_unknown_handler_rejected(plesk, hid):
    with pytest.raises(ValueError, match="Aktivierte Handler"):
        server.set_php_handler("example.com", hid, confirm=True)
    assert updates(plesk) == []


@pytest.mark.parametrize("hid", ["", "x y", "a;rm -rf /", "-php true", "$(id)"])
def test_invalid_handler_id(plesk, hid):
    with pytest.raises(ValueError, match="handler_id"):
        server.set_php_handler("example.com", hid, confirm=True)
    assert plesk["calls"] == []


@pytest.mark.parametrize("domain", ["", "example.com; id", "../x", "Example Com"])
def test_invalid_domain(plesk, domain):
    with pytest.raises(ValueError):
        server.set_php_handler(domain, "plesk-php83-fpm-dedicated", confirm=True)
    assert plesk["calls"] == []


def test_unknown_domain(plesk):
    with pytest.raises(ValueError, match="nicht mit Webhosting"):
        server.set_php_handler("other.example.com", "plesk-php83-fpm-dedicated", confirm=True)


def test_plesk_error_reported(plesk):
    plesk["update_rc"] = 1
    with pytest.raises(server.SSHError, match="nicht umgestellt"):
        server.set_php_handler("example.com", "plesk-php83-fpm-dedicated", confirm=True)
