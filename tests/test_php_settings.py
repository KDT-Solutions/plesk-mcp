"""Tests für php_settings_show/_update/_rollback (ohne Plesk-Server).

Ein Fake ersetzt SSH und bildet Plesk nach: Plesk-DB (eigene Werte und
Additional-Block wörtlich), --show-php-settings (eigene + geerbte Werte, ohne
Abschnittsüberschriften), --update-php-settings (zusammenführend oder
ersetzend), systemctl, cagefsctl und die Backup-Dateien.
Ausführen: pip install -r requirements.txt pytest && pytest
"""

import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

DOMAIN = "example.com"
INHERITED = {"include_path": ".:/opt/plesk/php/8.3/share/pear", "log_errors": "1",
             "open_basedir": "{WEBSPACEROOT}{/}{:}{TMP}{/}", "memory_limit": "128M",
             "max_execution_time": "30", "max_input_time": "60", "post_max_size": "8M",
             "upload_max_filesize": "2M", "disable_functions": "", "pm": "ondemand",
             "pm.max_children": "10", "pm.max_requests": "0", "pm.start_servers": "1",
             "pm.min_spare_servers": "1", "pm.max_spare_servers": "1"}
SECTIONS = {"General settings": ["include_path", "open_basedir", "log_errors"],
            "Performance settings": ["memory_limit", "max_execution_time", "max_input_time",
                                     "post_max_size", "upload_max_filesize", "disable_functions"],
            "PHP-FPM settings": ["pm", "pm.max_children", "pm.max_requests", "pm.start_servers",
                                 "pm.min_spare_servers", "pm.max_spare_servers"]}


class FakeClient:
    def close(self):
        pass


def hx(s):
    return s.encode().hex().upper()


@pytest.fixture
def plesk(monkeypatch):
    st = {
        "sid": "319",
        "own": {"memory_limit": "256M", "max_execution_time": "120", "pm.max_children": "200",
                "disable_functions": "opcache_get_status", "enableFastcgi": "off"},
        "additional": "[php-fpm-pool-settings]\npm.status_path = /status_phpfpm",
        "mode": "merge", "fpm": "active", "fpm_after_write": [], "cagefs": "Enabled",
        "files": {}, "writes": [], "fail_write": False,
    }

    def show():
        eff = dict(INHERITED, **st["own"])
        lines = []
        for sec, keys in SECTIONS.items():
            lines.append(sec + ":")
            lines += [f"{k} = {eff[k]}" for k in keys]
        lines.append("Additional directives:")
        lines += [l for l in st["additional"].split("\n") if l.strip() and not l.startswith("[")]
        return "\n".join(lines)

    def exec_(client, cmd, timeout=None, stdin_data=None):
        if cmd.startswith("plesk db -Ne"):
            if "sys_users" in cmd:
                if f"'{DOMAIN}'" not in cmd:
                    return "", "", 0
                return f"kunde\t{st['sid'] or ''}\n", "", 0
            if "PhpSettingsParameters" in cmd:
                return "".join(f"{hx(k)}\t{hx(v)}\n" for k, v in st["own"].items()), "", 0
            if "PhpSettingsCustom" in cmd:
                return (hx(st["additional"]) + "\n") if st["additional"] else "", "", 0
            raise AssertionError(cmd)
        if cmd.startswith("plesk bin site --show-php-settings"):
            return show(), "", 0
        if "--update-php-settings" in cmd:
            assert "/root/.plesk-mcp/tmp" in cmd and "trap" in cmd and DOMAIN in cmd
            text = stdin_data.decode()
            opt = "-additional-settings" if "-additional-settings" in cmd else "-settings"
            st["writes"].append((opt, text))
            if st["fail_write"]:
                return "", "ERROR", 1
            if opt == "-additional-settings":
                st["additional"] = text.rstrip("\n")
            else:
                new = {} if st["mode"] == "replace" else dict(st["own"])
                for line in text.splitlines():
                    if line.strip():
                        k, _, v = line.partition(" = ")
                        new[k] = v
                st["own"] = new
                st["sid"] = st["sid"] or "999"
            if st["fpm_after_write"]:
                st["fpm"] = st["fpm_after_write"].pop(0) if st["fpm_after_write"] else st["fpm"]
            return "SUCCESS", "", 0
        if cmd.startswith("systemctl list-units"):
            return f"plesk-php83-fpm_{DOMAIN}_1.service loaded active running x\n", "", 0
        if cmd.startswith("systemctl is-active"):
            return st["fpm"] + "\n", "", 0 if st["fpm"] == "active" else 3
        if "cagefsctl" in cmd:
            return st["cagefs"] + "\n", "", 0
        if "set -C && cat >" in cmd:
            path = cmd.rsplit("cat > ", 1)[1].strip("'")
            assert path.startswith("/root/.plesk-mcp/php-settings-backups/")
            if path in st["files"]:
                return "", "exists", 1
            st["files"][path] = stdin_data.decode()
            return "", "", 0
        if cmd.startswith("cat -- "):
            path = cmd[len("cat -- "):].strip("'")
            if path not in st["files"]:
                return "", "No such file", 1
            return st["files"][path], "", 0
        raise AssertionError(cmd)

    monkeypatch.setattr(server, "_ssh_connect", lambda: FakeClient())
    monkeypatch.setattr(server, "_client_exec", exec_)
    monkeypatch.setattr(server, "_phps_http", lambda d: (200, "ok"))
    monkeypatch.setattr(server, "_PHPS_FPM_RETRY_DELAY", 0)
    monkeypatch.delenv("PHP_SETTINGS_ALLOWLIST", raising=False)
    return st


def upd(**kw):
    return json.loads(server.php_settings_update(DOMAIN, **kw))


def test_show_includes_raw_block_and_own_values(plesk):
    res = json.loads(server.php_settings_show(DOMAIN))
    assert res["additional_directives_roh"] == "[php-fpm-pool-settings]\npm.status_path = /status_phpfpm"
    assert res["eigene_werte"]["memory_limit"] == "256M"
    assert res["abschnitte"]["Performance settings"]["memory_limit"] == "256M"
    assert res["abschnitte"]["Additional directives"] == {"pm.status_path": "/status_phpfpm"}
    assert "disable_functions" not in res["aenderbar"]


def test_preview_writes_nothing(plesk):
    res = upd(settings={"memory_limit": "512M", "upload_max_filesize": "64M"})
    assert res["ergebnis"].startswith("Vorschau") and "backup_id" not in res
    by = {c["wert"]: c for c in res["aenderungen"]}
    assert by["memory_limit"]["alt_war"] == "eigen" and by["upload_max_filesize"]["alt_war"] == "geerbt"
    assert by["upload_max_filesize"]["alt"] == "2M"
    assert plesk["writes"] == [] and plesk["files"] == {}


def test_update_keeps_sections_and_other_values(plesk):
    res = upd(settings={"memory_limit": "512M"},
              additional_directives={"max_input_vars": "5000", "opcache.revalidate_freq": "60"}, confirm=True)
    assert res["ergebnis"] == "Geschrieben und kontrolliert.", res
    assert plesk["own"]["memory_limit"] == "512M" and plesk["own"]["pm.max_children"] == "200"
    assert plesk["own"]["disable_functions"] == "opcache_get_status"
    assert plesk["additional"] == ("max_input_vars = 5000\nopcache.revalidate_freq = 60\n"
                                   "[php-fpm-pool-settings]\npm.status_path = /status_phpfpm")
    assert plesk["writes"][0] == ("-settings", "memory_limit = 512M\n")
    assert res["schreibvorgang"]["settings_schreibvorgaenge"] == 1
    assert res["schreibvorgang"]["abweichend_nach_teilschreiben"] == []
    assert res["schreibvorgang"]["plesk_verhalten_settings"].startswith("zusammenführend")
    backup = json.loads(next(iter(plesk["files"].values())))
    assert backup["id"] == res["backup_id"] and backup["own_settings"]["memory_limit"] == "256M"
    assert backup["additional_directives"].startswith("[php-fpm-pool-settings]")


def test_update_with_replace_semantics_writes_full_set(plesk):
    plesk["mode"] = "replace"
    res = upd(settings={"memory_limit": "512M"}, confirm=True)
    assert res["ergebnis"] == "Geschrieben und kontrolliert.", res
    assert plesk["own"]["pm.max_children"] == "200" and plesk["own"]["enableFastcgi"] == "off"
    assert [w[0] for w in plesk["writes"]] == ["-settings", "-settings"]
    info = res["schreibvorgang"]
    assert info["settings_schreibvorgaenge"] == 2 and info["plesk_verhalten_settings"].startswith("abweichend")
    assert {"wert": "pm.max_children", "soll": "200", "ist": None} in info["abweichend_nach_teilschreiben"]


def test_failed_check_rolls_back(plesk):
    # erste Änderung legt den FPM-Dienst lahm, das Zurückstellen bringt ihn wieder hoch
    plesk["fpm_after_write"] = ["failed", "active", "active"]
    res = upd(settings={"memory_limit": "512M", "upload_max_filesize": "64M"}, confirm=True)
    assert res["ergebnis"].startswith("FEHLER - automatisch"), res
    assert any("PHP-FPM" in f for f in res["fehlgeschlagene_pruefungen"])
    assert plesk["own"]["memory_limit"] == "256M"
    # vorher geerbter Wert ist jetzt eigen, aber wieder auf dem alten Wert
    assert plesk["own"]["upload_max_filesize"] == "2M"


def test_http_5xx_rolls_back_but_baseline_5xx_is_skipped(plesk, monkeypatch):
    calls = iter([(200, "a"), (502, "b"), (200, "c")])
    monkeypatch.setattr(server, "_phps_http", lambda d: next(calls))
    res = upd(settings={"memory_limit": "512M"}, confirm=True)
    assert res["ergebnis"].startswith("FEHLER - automatisch") and plesk["own"]["memory_limit"] == "256M"
    monkeypatch.setattr(server, "_phps_http", lambda d: (503, "x"))
    res = upd(settings={"memory_limit": "512M"}, confirm=True)
    assert res["ergebnis"] == "Geschrieben und kontrolliert." and "übersprungen" in res["pruefungen"]["startseite"]


def test_plesk_error_rolls_back(plesk):
    plesk["fail_write"] = True
    res = upd(settings={"memory_limit": "512M"}, confirm=True)
    assert res["ergebnis"].startswith("FEHLER") and plesk["own"]["memory_limit"] == "256M"


def test_rollback_tool(plesk):
    res = upd(settings={"memory_limit": "512M", "upload_max_filesize": "64M"},
              additional_directives={"max_input_vars": "5000"}, confirm=True)
    bid = res["backup_id"]
    prev = json.loads(server.php_settings_rollback(DOMAIN, bid))
    assert prev["ergebnis"].startswith("Vorschau") and any("upload_max_filesize" in h for h in prev["hinweise"])
    res = json.loads(server.php_settings_rollback(DOMAIN, bid, confirm=True))
    assert res["ergebnis"] == "Geschrieben und kontrolliert.", res
    assert plesk["own"]["memory_limit"] == "256M" and plesk["own"]["upload_max_filesize"] == "2M"
    assert plesk["additional"] == "[php-fpm-pool-settings]\npm.status_path = /status_phpfpm"
    assert len(plesk["files"]) == 2  # Rollback legt selbst ein Backup an


def test_rollback_rejects_foreign_or_bad_id(plesk):
    for bid in ["20260101120000-1a2b3c4d@other.example.com", "../../etc/passwd", ""]:
        with pytest.raises(ValueError):
            server.php_settings_rollback(DOMAIN, bid, confirm=True)
    with pytest.raises(ValueError, match="nicht gefunden"):
        server.php_settings_rollback(DOMAIN, "20260101120000-1a2b3c4d@example.com", confirm=True)


def test_domain_without_own_settings(plesk):
    plesk.update(sid="", own={}, additional="")
    res = upd(settings={"memory_limit": "512M"}, confirm=True)
    assert res["ergebnis"] == "Geschrieben und kontrolliert." and plesk["own"] == {"memory_limit": "512M"}


@pytest.mark.parametrize("settings,additional,match", [
    ({"disable_functions": ""}, None, "nie änderbar"),
    ({"auto_prepend_file": "/tmp/x.php"}, None, "nie änderbar"),
    (None, {"allow_url_include": "1"}, "nie änderbar"),
    ({"display_errors": "1"}, None, "nicht freigegeben"),
    ({"memory_limit": "512M\nauto_prepend_file = /tmp/x"}, None, "Zeilenumbr"),
    ({"memory_limit": "512M;"}, None, "Zeilenumbr"),
    (None, {"max_input_vars": "[x]"}, "eckigen"),
    ({"memory_limit": "\"1G\""}, None, "Anführungszeichen"),
    ({"memory_limit": "viel"}, None, "Ungültiger Wert"),
    ({"pm": "always"}, None, "Ungültiger Wert"),
    ({"open_basedir": "/"}, None, "open_basedir"),
    ({"Memory_Limit": "1G"}, None, "Schlüssel"),
    ({"[php-fpm-pool-settings]": "x"}, None, "Schlüssel"),
    (None, {"pm.max_children": "5"}, "Standardfeld"),
    (None, {"memory_limit": "1G"}, "Standardfeld"),
    ({"max_input_vars": "5000"}, None, "additional_directives"),
])
def test_rejections(plesk, settings, additional, match):
    with pytest.raises(ValueError, match=match):
        upd(settings=settings, additional_directives=additional, confirm=True)
    assert plesk["writes"] == [] and plesk["files"] == {}


def test_open_basedir_none_needs_cagefs(plesk):
    plesk["cagefs"] = "Disabled"
    with pytest.raises(ValueError, match="CageFS"):
        upd(settings={"open_basedir": "none"}, confirm=True)
    assert plesk["writes"] == []
    plesk["cagefs"] = "Enabled"
    res = upd(settings={"open_basedir": "none"}, confirm=True)
    assert res["ergebnis"] == "Geschrieben und kontrolliert." and plesk["own"]["open_basedir"] == "none"


def test_env_allowlist(plesk, monkeypatch):
    monkeypatch.setenv("PHP_SETTINGS_ALLOWLIST", "session.gc_maxlifetime, disable_functions, Bad Key")
    res = upd(additional_directives={"session.gc_maxlifetime": "7200"}, confirm=True)
    assert res["ergebnis"] == "Geschrieben und kontrolliert."
    with pytest.raises(ValueError, match="nie änderbar"):
        upd(settings={"disable_functions": ""}, confirm=True)


def test_lock_per_domain(plesk):
    lock = server._phps_lock(DOMAIN)
    try:
        with pytest.raises(ValueError, match="läuft bereits"):
            upd(settings={"memory_limit": "512M"}, confirm=True)
    finally:
        lock.release()
    upd(settings={"memory_limit": "512M"}, confirm=True)


def test_unknown_domain(plesk):
    with pytest.raises(ValueError, match="nicht mit Webhosting"):
        server.php_settings_update("other.example.com", settings={"memory_limit": "512M"}, confirm=True)


def test_merge_additional_unit():
    raw = "max_input_vars = 1000\n; Kommentar\n[php-fpm-pool-settings]\npm.status_path = /s\nmax_input_vars = 9"
    out = server._phps_merge_additional(raw, {"max_input_vars": "3000", "realpath_cache_ttl": "600"})
    assert out == ("max_input_vars = 3000\n; Kommentar\nrealpath_cache_ttl = 600\n"
                   "[php-fpm-pool-settings]\npm.status_path = /s\nmax_input_vars = 9")
    assert server._phps_merge_additional("", {"a.b": "1"}) == "a.b = 1"
    assert server._phps_block_values(raw) == {"max_input_vars": "1000"}


def test_show_parser_unit():
    parsed = server._parse_php_settings("General settings:\ndisplay_errors = \nPerformance settings:\n"
                                        "memory_limit = 256M\nAdditional directives:\npm.status_path = /s")
    assert parsed == {"General settings": {"display_errors": ""},
                      "Performance settings": {"memory_limit": "256M"},
                      "Additional directives": {"pm.status_path": "/s"}}


def test_rollback_failure_reported(plesk):
    plesk["fpm_after_write"] = ["failed"] * 10
    res = upd(settings={"memory_limit": "512M"}, confirm=True)
    assert res["ergebnis"].startswith("FEHLER - Zurückstellen ebenfalls fehlgeschlagen")
    assert res["fehler_beim_zurueckstellen"]
