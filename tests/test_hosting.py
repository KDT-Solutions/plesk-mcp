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
    state = {"handlers": {"example.com": "plesk-php82-fpm-dedicated",
                          "shop.example.com": "plesk-php82-fpm-dedicated",
                          "xn--kche-0ra.example": "plesk-php83-fpm-dedicated"},
             "calls": [], "fail": set()}

    def exec_(client, cmd, timeout=None, stdin_data=None):
        state["calls"].append(cmd)
        if cmd.startswith("plesk db"):
            for d, h in state["handlers"].items():
                if f"'{d}'" in cmd:
                    return h + "\n", "", 0
            return "", "", 0
        if cmd.startswith("plesk bin php_handler --list"):
            return json.dumps(HANDLERS), "", 0
        if cmd.startswith("plesk bin site --show-php-settings"):
            return PHP_SETTINGS, "", 0
        if cmd.startswith("plesk bin site --info"):
            return SITE_INFO, "", 0
        if cmd.startswith("plesk bin site --update"):
            d = cmd.split()[4]
            if d in state["fail"]:
                return "", "An error occurred", 1
            state["handlers"][d] = cmd.split()[-1]
            return f"SUCCESS: Update of domain '{d}' completed.", "", 0
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
    res = json.loads(server.set_php_handler("plesk-php83-fpm-dedicated", domain="example.com"))
    item = res["domains"][0]
    assert item["ergebnis"].startswith("Vorschau") and item["versionswechsel"] and not item["typwechsel"]
    assert "hinweis" in res and updates(plesk) == []
    assert plesk["handlers"]["example.com"] == "plesk-php82-fpm-dedicated"


def test_change_with_confirm(plesk):
    res = json.loads(server.set_php_handler("plesk-php83-fpm-dedicated", domain="example.com", confirm=True))
    assert updates(plesk) == ["plesk bin site --update example.com -php_handler_id plesk-php83-fpm-dedicated"]
    assert plesk["handlers"]["example.com"] == "plesk-php83-fpm-dedicated"
    assert res["domains"][0]["ergebnis"] == "umgestellt"
    assert res["domains"][0]["alt"]["id"] == "plesk-php82-fpm-dedicated"


def test_multiple_domains_and_idn(plesk):
    res = json.loads(server.set_php_handler(
        "plesk-php83-fpm-dedicated", domains=["example.com", "Shop.Example.com", "küche.example"], confirm=True))
    assert [r["domain"] for r in res["domains"]] == ["example.com", "shop.example.com", "xn--kche-0ra.example"]
    assert [r["ergebnis"] for r in res["domains"]] == ["umgestellt", "umgestellt",
                                                      "unverändert (nutzt diesen Handler bereits)"]
    assert len(updates(plesk)) == 2 and "fehlgeschlagen" not in res


def test_unknown_domain_changes_nothing(plesk):
    with pytest.raises(ValueError, match="nicht mit Webhosting"):
        server.set_php_handler("plesk-php83-fpm-dedicated", domains=["example.com", "other.example.com"],
                               confirm=True)
    assert updates(plesk) == []


def test_failure_reported_others_changed(plesk):
    plesk["fail"].add("example.com")
    res = json.loads(server.set_php_handler(
        "plesk-php83-fpm-dedicated", domains=["example.com", "shop.example.com"], confirm=True))
    assert res["fehlgeschlagen"] == ["example.com"] and "Exit-Code 1" in res["domains"][0]["fehler"]
    assert plesk["handlers"]["shop.example.com"] == "plesk-php83-fpm-dedicated"


@pytest.mark.parametrize("hid", ["plesk-php83-fastcgi", "plesk-php99-fpm"])
def test_disabled_or_unknown_handler_rejected(plesk, hid):
    with pytest.raises(ValueError, match="Aktivierte Handler"):
        server.set_php_handler(hid, domain="example.com", confirm=True)
    assert updates(plesk) == []


@pytest.mark.parametrize("hid", ["", "x y", "a;rm -rf /", "-php true", "$(id)"])
def test_invalid_handler_id(plesk, hid):
    with pytest.raises(ValueError, match="handler_id"):
        server.set_php_handler(hid, domain="example.com", confirm=True)
    assert plesk["calls"] == []


@pytest.mark.parametrize("domain", ["example.com; id", "../x", "Example Com", "-php_handler_id"])
def test_invalid_domain(plesk, domain):
    with pytest.raises(ValueError):
        server.set_php_handler("plesk-php83-fpm-dedicated", domain=domain, confirm=True)
    assert plesk["calls"] == []


def test_domain_required_and_limit(plesk):
    with pytest.raises(ValueError, match="domain oder domains"):
        server.set_php_handler("plesk-php83-fpm-dedicated", confirm=True)
    with pytest.raises(ValueError, match="Maximal"):
        server.set_php_handler("plesk-php83-fpm-dedicated", domains=[f"d{i}.example.com" for i in range(51)])
    assert plesk["calls"] == []


# --- set_php_settings --------------------------------------------------------

SECTIONS = {
    "General settings": ["display_errors", "log_errors", "allow_url_fopen", "open_basedir"],
    "Performance settings": ["memory_limit", "max_execution_time", "post_max_size", "upload_max_filesize"],
    "PHP-FPM settings": ["pm", "pm.max_children"],
}
DEFAULTS = {"display_errors": "", "log_errors": "1", "allow_url_fopen": "1",
            "open_basedir": "{WEBSPACEROOT}{/}{:}{TMP}{/}", "memory_limit": "128M",
            "max_execution_time": "30", "post_max_size": "8M", "upload_max_filesize": "2M",
            "pm": "ondemand", "pm.max_children": "10"}


@pytest.fixture
def php(monkeypatch):
    """Simuliert show/update-php-settings. mode="merge": nur genannte Werte
    ändern; mode="replace": nicht genannte Werte fallen auf Defaults zurück."""
    state = {"values": dict(DEFAULTS, memory_limit="256M", max_execution_time="120", **{"pm.max_children": "200"}),
             "additional": ["pm.status_path = /status_phpfpm", "; Kommentar bleibt"],
             "mode": "merge", "calls": [], "files": [], "fail": False}

    def show():
        lines = []
        for sec, keys in SECTIONS.items():
            lines.append(sec + ":")
            lines += [f"{k} = {state['values'][k]}" for k in keys]
        lines.append("Additional directives:")
        lines += state["additional"]
        return "\n".join(lines)

    def exec_(client, cmd, timeout=None, stdin_data=None):
        state["calls"].append(cmd)
        if cmd.startswith("plesk db"):
            return ("plesk-php83-fpm-dedicated\n", "", 0) if "'example.com'" in cmd else ("", "", 0)
        if cmd.startswith("plesk bin site --show-php-settings"):
            return show(), "", 0
        if "--update-php-settings" in cmd:
            assert "example.com" in cmd and stdin_data is not None
            text = stdin_data.decode()
            state["files"].append(text)
            if state["fail"]:
                return "", "ERROR", 1
            lines = [l for l in text.splitlines() if l.strip()]
            if "-additional-settings" in cmd:
                state["additional"] = lines
                return "SUCCESS", "", 0
            new = dict(DEFAULTS) if state["mode"] == "replace" else dict(state["values"])
            for l in lines:
                k, _, v = l.partition(" = ")
                new[k] = "1" if v.lower() in ("on", "1", "true") else ("" if v.lower() in ("off", "0") else v)
            state["values"] = new
            state["mode"] = "merge"
            return "SUCCESS", "", 0
        raise AssertionError(cmd)

    monkeypatch.setattr(server, "_ssh_connect", lambda: FakeClient())
    monkeypatch.setattr(server, "_client_exec", exec_)
    return state


def php_updates(state):
    return [c for c in state["calls"] if "--update-php-settings" in c]


def test_php_preview(php):
    res = json.loads(server.set_php_settings({"memory_limit": "512M", "max_input_vars": "5000"},
                                             domain="example.com"))
    d = res["domains"][0]
    assert d["ergebnis"].startswith("Vorschau") and "hinweis" in res
    assert {"wert": "memory_limit", "alt": "256M", "neu": "512M"} in d["aenderungen"]
    assert {"wert": "max_input_vars", "alt": None, "neu": "5000"} in d["aenderungen"]
    assert php_updates(php) == []


def test_php_change_merge_keeps_additional(php):
    res = json.loads(server.set_php_settings(
        {"memory_limit": "512M", "display_errors": "off", "max_input_vars": "5000"},
        domain="example.com", confirm=True))
    d = res["domains"][0]
    assert d["ergebnis"] == "geändert", d
    # display_errors ist bereits aus -> keine Änderung
    assert [a["wert"] for a in d["aenderungen"]] == ["memory_limit", "max_input_vars"]
    assert php["values"]["memory_limit"] == "512M" and php["values"]["pm.max_children"] == "200"
    assert php["files"][0] == "memory_limit = 512M\n"
    assert php["additional"] == ["pm.status_path = /status_phpfpm", "; Kommentar bleibt", "max_input_vars = 5000"]
    assert d["ruecksetzen"] == {"memory_limit": "256M"}


def test_php_replace_semantics_restored(php):
    php["mode"] = "replace"
    res = json.loads(server.set_php_settings({"memory_limit": "512M"}, domain="example.com", confirm=True))
    d = res["domains"][0]
    assert d["ergebnis"] == "geändert", d
    assert sorted(d["wiederhergestellt"]) == ["max_execution_time", "pm.max_children"]
    assert php["values"]["max_execution_time"] == "120" and php["values"]["pm.max_children"] == "200"
    assert php["values"]["memory_limit"] == "512M"


def test_php_additional_existing_key_replaced(php):
    php["additional"] = ["max_input_vars = 1000", "pm.status_path = /status_phpfpm"]
    server.set_php_settings({"max_input_vars": "3000"}, domain="example.com", confirm=True)
    assert php["additional"] == ["max_input_vars = 3000", "pm.status_path = /status_phpfpm"]


def test_php_unchanged(php):
    res = json.loads(server.set_php_settings({"memory_limit": "256M", "log_errors": "on"},
                                             domain="example.com", confirm=True))
    assert res["domains"][0]["ergebnis"].startswith("unverändert") and php_updates(php) == []


@pytest.mark.parametrize("settings", [
    {"open_basedir": "/"}, {"disable_functions": ""}, {"memory_limit": "512M\nopen_basedir = /"},
    {"memory_limit": "viel"}, {"pm": "always"}, {"max_execution_time": "1; rm"}, {},
    {"session.save_path": "/tmp"}, {"error_reporting": "E_ALL\n"},
])
def test_php_rejected(php, settings):
    with pytest.raises(ValueError):
        server.set_php_settings(settings, domain="example.com", confirm=True)
    assert php_updates(php) == []


def test_php_key_not_managed_by_plesk(php):
    with pytest.raises(ValueError, match="nicht geführt"):
        server.set_php_settings({"pm.max_spare_servers": "5"}, domain="example.com", confirm=True)
    assert php_updates(php) == []


def test_php_unknown_domain_changes_nothing(php):
    with pytest.raises(ValueError, match="nicht mit Webhosting"):
        server.set_php_settings({"memory_limit": "512M"}, domains=["example.com", "other.example.com"],
                                confirm=True)
    assert php_updates(php) == []


def test_php_plesk_error_reported(php):
    php["fail"] = True
    res = json.loads(server.set_php_settings({"memory_limit": "512M"}, domain="example.com", confirm=True))
    assert res["fehlgeschlagen"] == ["example.com"]
    assert any("Exit-Code 1" in f for f in res["domains"][0]["fehler"])
