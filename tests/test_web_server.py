"""Tests für web_server_settings_show/_update/_rollback (ohne Plesk-Server)."""

import json
import os
import shlex
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

DOMAIN = "example.com"
OLD_NGINX = "location /x {\n  return 403;\n}"
VHOST_BASE = "/var/www/vhosts/system/example.com"
VHOST_CONF = VHOST_BASE + "/conf"
VHOST_FILE = VHOST_CONF + "/vhost_nginx.conf"


class FakeClient:
    def close(self):
        pass


@pytest.fixture
def plesk(monkeypatch):
    st = {"values": {"web-server-expires": "", "web-server-expires-static-only": "true",
                     "nginx-serve-static": "false", "nginx-cache-enabled": "false",
                     "nginx-cache-timeout": "5", "nginx-static-extensions": "css js jpg",
                     "additional nginx settings": "location /x {\n  return 403;\n}"},
          "fail": False, "ignore": set(), "files": {}, "updates": [], "nginx_ok": [], "nginx_tests": 0,
          "vfile": {"exists": True, "owner": "root:nginx", "mode": "600", "content": OLD_NGINX + "\n"},
          "put_fail": False, "puts": [], "reconf": [], "reconf_rc": 0, "log": [], "included": True}

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
        vf = st["vfile"]
        if cmd.startswith(f"b={VHOST_BASE}; c={VHOST_CONF}; f={VHOST_FILE}; ") and "stat -c" in cmd:
            if not vf["exists"]:
                return "ABSENT\n", "", 0
            return f"{vf['owner']} {vf['mode']}\n{vf['content']}", "", 0
        if cmd.startswith(f"c={VHOST_CONF}; f={VHOST_FILE}; ") and "mktemp" in cmd:
            st["log"].append("put")
            assert "mv -fT" in cmd and "chown root:nginx" in cmd and "chmod 600" in cmd
            if st["put_fail"]:
                return "", "mv: cannot move", 1
            st["puts"].append(stdin_data.decode())
            st["vfile"] = {"exists": True, "owner": "root:nginx", "mode": "600", "content": stdin_data.decode()}
            return "", "", 0
        if cmd.startswith(f"f={VHOST_FILE}; ") and "rm -f" in cmd:
            st["log"].append("rm")
            st["vfile"] = {"exists": False, "owner": "", "mode": "", "content": ""}
            return "", "", 0
        if cmd == f"plesk sbin httpdmng --reconfigure-domain {DOMAIN}":
            st["log"].append("reconfigure")
            st["reconf"].append(cmd)
            if st["reconf_rc"] != 0:
                return "", "httpdmng failed", st["reconf_rc"]
            # wie das Plesk-Template: include nur, wenn die Datei beim Generieren existiert
            st["included"] = st["vfile"]["exists"]
            return "", "", 0
        if cmd == "nginx -t":
            st["log"].append("nginx -t")
            st["nginx_tests"] += 1
            if st["included"] and not st["vfile"]["exists"]:
                return "", f'nginx: [emerg] open() "{VHOST_FILE}" failed (2: No such file or directory)', 1
            ok = st["nginx_ok"].pop(0) if st["nginx_ok"] else True
            return ("", "nginx: configuration file /etc/nginx/nginx.conf test is successful", 0) if ok \
                else ("", "nginx: [emerg] unknown directive", 1)
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
                                      {"additional nginx settings": "x"}, {"nginx-directives": 1},
                                      {"nginx-directives": "return 403;"}, {}])
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


ROBOTS = 'add_header X-Robots-Tag "noindex, nofollow" always;'


def test_show_lists_nginx_directives(plesk):
    res = json.loads(server.web_server_settings_show(DOMAIN))
    assert res["einstellungen"]["additional nginx settings"] == OLD_NGINX
    assert "nginx-directives" in res["aenderbar"]


@pytest.mark.parametrize("text", [
    ROBOTS,
    "# Kommentar\n\nexpires 30d;\nadd_header Cache-Control \"public\";",
    "location ~* \\.(css|js)$ {\n    expires max;  # lange cachen\n    add_header X-Test 1 always;\n}",
    "location = /robots.txt { add_header X-A \"b; c\"; }",
    "expires modified +24h;",
    "",
])
def test_nginx_allowed(text):
    server._web_nginx_check(server._web_norm(text))


@pytest.mark.parametrize("text", [
    "proxy_pass http://127.0.0.1:8080;", "include /etc/passwd;", "root /tmp;", "alias /tmp/;",
    "content_by_lua_block { ngx.say(1) }", "return 301 https://example.com/;", "rewrite ^ /x;",
    "auth_basic \"x\";", "ssl_protocols TLSv1;",
    "location / {\n  proxy_pass http://127.0.0.1;\n}",
    "location / {\n  location /a { expires 1d; }\n}",
    "add_header X-A b", "add_header X-A;", "add_header X-A b never;", "add_header X;A b;",
    "location / {\n  expires 1d;", "}", "add_header X-A \"b;", "expires 1d; rewrite ^ /x;",
    "add_header X-A b; #\nreturn 403;",
])
def test_nginx_rejected_check(text):
    with pytest.raises(ValueError):
        server._web_nginx_check(server._web_norm(text))


def test_nginx_preview_shows_diff(plesk):
    prev = upd({"nginx-directives": ROBOTS})
    assert prev["ergebnis"].startswith("Vorschau") and plesk["log"] == [] and plesk["files"] == {}
    c = prev["aenderungen"][0]
    assert prev["datei"] == VHOST_FILE
    assert c["option"] == "nginx-directives" and c["alt"] == OLD_NGINX and c["neu"] == ROBOTS
    assert "-  return 403;" in c["diff"] and "+" + ROBOTS in c["diff"] and "hinweis" in c


def test_nginx_write_and_test_ok(plesk):
    res = upd({"nginx-directives": ROBOTS}, confirm=True)
    assert res["ergebnis"] == "Geschrieben und kontrolliert." and res["backup_id"]
    assert plesk["vfile"]["content"] == ROBOTS + "\n"
    # nginx -t vorher, nach dem Schreiben, nach dem Reconfigure; Reconfigure erst nach bestandenem Test
    assert plesk["log"] == ["nginx -t", "put", "nginx -t", "reconfigure", "nginx -t"]
    assert plesk["updates"] == []  # kein plesk bin domain --update-web-server-settings
    backup = json.loads(next(iter(plesk["files"].values())))
    assert backup["nginx_file"] == {"exists": True, "owner": "root:nginx", "mode": "600",
                                    "content": OLD_NGINX + "\n"}
    assert upd({"nginx-directives": ROBOTS}, confirm=True)["ergebnis"].startswith("Keine")


def test_nginx_new_file(plesk):
    plesk["vfile"] = {"exists": False, "owner": "", "mode": "", "content": ""}
    plesk["included"] = False
    res = upd({"nginx-directives": ROBOTS}, confirm=True)
    assert res["ergebnis"] == "Geschrieben und kontrolliert."
    assert plesk["vfile"]["content"] == ROBOTS + "\n"


def test_nginx_rejected_directive(plesk):
    with pytest.raises(ValueError, match="proxy_pass"):
        upd({"nginx-directives": "proxy_pass http://127.0.0.1:8080;"}, confirm=True)
    with pytest.raises(ValueError):
        server.web_server_settings_update(DOMAIN, {"nginx-directives": "include /etc/x;"}, confirm=True,
                                          mode="append")
    assert plesk["log"] == [] and plesk["files"] == {}


def test_nginx_test_failure_keeps_file(plesk):
    plesk["nginx_ok"] = [True, False, True]
    res = upd({"nginx-directives": ROBOTS}, confirm=True)
    assert res["ergebnis"] == "FEHLER - automatisch auf den alten Stand zurückgestellt, nichts neu geladen."
    assert any(f.startswith("nginx -t") for f in res["fehlgeschlagene_pruefungen"])
    assert plesk["vfile"]["content"] == OLD_NGINX + "\n"
    assert plesk["reconf"] == []  # nichts neu geladen
    assert "fehler_beim_zurueckstellen" not in res


def test_nginx_test_failing_before_changes_nothing(plesk):
    plesk["nginx_ok"] = [False]
    with pytest.raises(ValueError, match="vor der Änderung"):
        upd({"nginx-directives": ROBOTS}, confirm=True)
    assert plesk["puts"] == [] and plesk["files"] == {}


def test_nginx_write_error_without_change_no_rollback(plesk):
    plesk["put_fail"] = True
    res = upd({"nginx-directives": ROBOTS}, confirm=True)
    assert res["ergebnis"] == "FEHLER - nichts geändert."
    assert "fehler_beim_zurueckstellen" not in res
    assert plesk["log"] == ["nginx -t", "put"] and plesk["reconf"] == []
    assert plesk["vfile"]["content"] == OLD_NGINX + "\n"


def test_plesk_error_without_change_no_rollback(plesk):
    plesk["fail"] = True
    res = upd({"nginx-cache-enabled": "true"}, confirm=True)
    assert res["ergebnis"] == "FEHLER - nichts geändert." and "fehler_beim_zurueckstellen" not in res
    assert len(plesk["updates"]) == 1  # kein zweiter Aufruf zum Zurückstellen


def test_nginx_http_5xx_rolls_back_and_reconfigures(plesk, monkeypatch):
    calls = iter([(200, "a"), (503, "b")])
    monkeypatch.setattr(server, "_phps_http", lambda d: next(calls))
    res = upd({"nginx-directives": ROBOTS}, confirm=True)
    assert res["ergebnis"] == "FEHLER - automatisch auf den alten Stand zurückgestellt."
    assert plesk["vfile"]["content"] == OLD_NGINX + "\n" and len(plesk["reconf"]) == 2


def test_nginx_reconfigure_error_rolls_back(plesk):
    plesk["reconf_rc"] = 1
    res = upd({"nginx-directives": ROBOTS}, confirm=True)
    assert res["ergebnis"].startswith("FEHLER - Zurückstellen ebenfalls")  # 2. Reconfigure scheitert auch
    assert plesk["vfile"]["content"] == OLD_NGINX + "\n"


def test_nginx_append(plesk):
    prev = json.loads(server.web_server_settings_update(DOMAIN, {"nginx-directives": ROBOTS}, mode="append"))
    c = prev["aenderungen"][0]
    assert c["neu"] == OLD_NGINX + "\n" + ROBOTS and "hinweis" not in c
    assert all(not line.startswith("-") for line in c["diff"].splitlines()[2:])
    res = json.loads(server.web_server_settings_update(DOMAIN, {"nginx-directives": ROBOTS}, confirm=True,
                                                       mode="append"))
    assert res["ergebnis"] == "Geschrieben und kontrolliert."
    assert plesk["vfile"]["content"] == OLD_NGINX + "\n" + ROBOTS + "\n"
    again = json.loads(server.web_server_settings_update(DOMAIN, {"nginx-directives": ROBOTS}, confirm=True,
                                                         mode="append"))
    assert again["ergebnis"].startswith("Keine")


@pytest.mark.parametrize("kwargs", [{"mode": "prepend"},
                                    {"mode": "append", "settings": {"nginx-cache-enabled": "true"}},
                                    {"settings": {"nginx-directives": ROBOTS, "nginx-cache-enabled": "true"}}])
def test_nginx_bad_call(plesk, kwargs):
    settings = kwargs.pop("settings", {"nginx-directives": ROBOTS})
    with pytest.raises(ValueError):
        server.web_server_settings_update(DOMAIN, settings, confirm=True, **kwargs)
    assert plesk["log"] == []


@pytest.mark.parametrize("domain", ["a/b.example.com", "..example.com", "example", "-x.example.com"])
def test_nginx_paths_reject(domain):
    with pytest.raises(ValueError):
        server._web_nginx_paths(domain)


def test_nginx_rollback(plesk):
    bid = upd({"nginx-directives": ROBOTS}, confirm=True)["backup_id"]
    plesk["log"].clear()
    prev = json.loads(server.web_server_settings_rollback(DOMAIN, bid))
    assert prev["ergebnis"].startswith("Vorschau") and plesk["log"] == []
    res = json.loads(server.web_server_settings_rollback(DOMAIN, bid, confirm=True))
    assert res["ergebnis"] == "Geschrieben und kontrolliert."
    assert plesk["vfile"]["content"] == OLD_NGINX + "\n"
    assert plesk["log"] == ["nginx -t", "put", "nginx -t", "reconfigure", "nginx -t"]


def test_nginx_rollback_removes_new_file(plesk):
    plesk["vfile"] = {"exists": False, "owner": "", "mode": "", "content": ""}
    plesk["included"] = False
    bid = upd({"nginx-directives": ROBOTS}, confirm=True)["backup_id"]
    assert plesk["included"] is True
    plesk["log"].clear()
    res = json.loads(server.web_server_settings_rollback(DOMAIN, bid, confirm=True))
    assert res["ergebnis"] == "Geschrieben und kontrolliert." and plesk["vfile"]["exists"] is False
    # Entfernen: kein nginx -t zwischen rm und Reconfigure (include zeigt noch auf die Datei)
    assert plesk["log"] == ["nginx -t", "rm", "reconfigure", "nginx -t"]
    assert plesk["included"] is False
