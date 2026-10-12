"""Tests für web_server_settings_show/_update/_rollback (ohne Plesk-Server)."""

import json
import os
import shlex
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

DOMAIN = "example.com"


class FakeClient:
    def close(self):
        pass


@pytest.fixture
def plesk(monkeypatch):
    st = {"values": {"web-server-expires": "", "web-server-expires-static-only": "true",
                     "nginx-serve-static": "false", "nginx-cache-enabled": "false",
                     "nginx-cache-timeout": "5", "nginx-static-extensions": "css js jpg",
                     "additional nginx settings": "location /x {\n  return 403;\n}"},
          "fail": False, "ignore": set(), "files": {}, "updates": []}

    def show():
        return "\n".join(f"[{k}]\n{v}\n" for k, v in st["values"].items())

    def exec_(client, cmd, timeout=None, stdin_data=None):
        if cmd.startswith("plesk bin domain --show-web-server-settings"):
            return (show(), "", 0) if cmd.split()[-1] == DOMAIN else ("", "Unable to find domain", 1)
        if cmd.startswith("plesk bin domain --update-web-server-settings"):
            st["updates"].append(cmd)
            if st["fail"]:
                return "", "ERROR", 1
            toks = shlex.split(cmd)[5:]
            for k, v in zip(toks[0::2], toks[1::2]):
                if k[1:] not in st["ignore"]:
                    st["values"][k[1:]] = v
            return "SUCCESS", "", 0
        if "set -C && cat >" in cmd:
            st["files"][cmd.rsplit("cat > ", 1)[1].strip("'")] = stdin_data.decode()
            return "", "", 0
        if cmd.startswith("cat -- "):
            p = cmd[len("cat -- "):].strip("'")
            return (st["files"][p], "", 0) if p in st["files"] else ("", "No such file", 1)
        raise AssertionError(cmd)

    monkeypatch.setattr(server, "_ssh_connect", lambda: FakeClient())
    monkeypatch.setattr(server, "_client_exec", exec_)
    monkeypatch.setattr(server, "_phps_http", lambda d: (200, "ok"))
    return st


def upd(settings, confirm=False):
    return json.loads(server.web_server_settings_update(DOMAIN, settings, confirm=confirm))


def test_show_parses_multiline(plesk):
    res = json.loads(server.web_server_settings_show(DOMAIN))
    assert res["einstellungen"]["additional nginx settings"] == "location /x {\n  return 403;\n}"
    assert res["einstellungen"]["web-server-expires"] == ""


def test_preview_and_update(plesk):
    prev = upd({"web-server-expires": 2592000, "nginx-serve-static": True})
    assert prev["ergebnis"].startswith("Vorschau") and plesk["updates"] == []
    res = upd({"web-server-expires": "2592000", "nginx-serve-static": "true"}, confirm=True)
    assert res["ergebnis"] == "Geschrieben und kontrolliert." and res["backup_id"]
    assert plesk["values"]["web-server-expires"] == "2592000" and plesk["values"]["nginx-serve-static"] == "true"
    assert plesk["updates"] == ["plesk bin domain --update-web-server-settings example.com "
                                "-web-server-expires 2592000 -nginx-serve-static true"]


def test_unchanged(plesk):
    assert upd({"web-server-expires-static-only": "true"}, confirm=True)["ergebnis"].startswith("Keine")


@pytest.mark.parametrize("settings", [{"nginx-cache-key": "x"}, {"nginx-static-extensions": "php"},
                                      {"web-server-expires": "1d"}, {"nginx-cache-enabled": "yes; rm"},
                                      {"additional nginx settings": "x"}, {}])
def test_rejected(plesk, settings):
    with pytest.raises(ValueError):
        upd(settings, confirm=True)
    assert plesk["updates"] == []


def test_mismatch_rolls_back(plesk):
    plesk["ignore"] = {"nginx-cache-timeout"}
    res = upd({"nginx-cache-enabled": "true", "nginx-cache-timeout": "60"}, confirm=True)
    assert res["ergebnis"].startswith("FEHLER - automatisch")
    assert plesk["values"]["nginx-cache-enabled"] == "false"


def test_http_5xx_rolls_back(plesk, monkeypatch):
    calls = iter([(200, "a"), (502, "b")])
    monkeypatch.setattr(server, "_phps_http", lambda d: next(calls))
    res = upd({"nginx-cache-enabled": "true"}, confirm=True)
    assert res["ergebnis"].startswith("FEHLER - automatisch") and plesk["values"]["nginx-cache-enabled"] == "false"


def test_plesk_error_rolls_back(plesk):
    plesk["fail"] = True
    res = upd({"nginx-cache-enabled": "true"}, confirm=True)
    assert res["ergebnis"].startswith("FEHLER")


def test_rollback(plesk):
    bid = upd({"web-server-expires": "86400", "nginx-cache-enabled": "true"}, confirm=True)["backup_id"]
    prev = json.loads(server.web_server_settings_rollback(DOMAIN, bid))
    assert prev["ergebnis"].startswith("Vorschau")
    res = json.loads(server.web_server_settings_rollback(DOMAIN, bid, confirm=True))
    assert res["ergebnis"] == "Geschrieben und kontrolliert."
    assert plesk["values"]["web-server-expires"] == "" and plesk["values"]["nginx-cache-enabled"] == "false"
    with pytest.raises(ValueError):
        server.web_server_settings_rollback(DOMAIN, "20260101120000-1a2b3c4d@other.example.com", confirm=True)


def test_unknown_domain(plesk):
    with pytest.raises(ValueError, match="nicht lesbar"):
        server.web_server_settings_update("other.example.com", {"nginx-cache-enabled": "true"})
