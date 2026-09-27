"""Tests für die Pfadvalidierung der Imunify360-Tools (ohne Serverzugriff).

Ausführen: pip install -r requirements.txt pytest && pytest
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

validate = server._imunify_validate_path


@pytest.mark.parametrize(
    "path",
    [
        "/var/www/vhosts/example.com/logs/error_log.1.gz",
        "/var/www/vhosts/example.com/logs/access_log.processed.7.gz",
        "/var/www/vhosts/example.com/logs/proxy_error_log-20260919.gz",
        "/var/www/vhosts/example.com/httpdocs/wp-content/uploads/file.php",
        "/var/www/vhosts/system/example.com/logs/error_log.2.gz",
        "/var/www/vhosts/sub.example.com/logs/access_ssl_log.1.gz",
    ],
)
def test_allowed(path):
    assert validate(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "/var/www/vhosts/example.com/../../../etc/passwd",
        "/var/www/vhosts/example.com/logs/../../other.example.com/x",
        "/var/www/vhosts/../vhosts/example.com/logs/error_log.1.gz",
        "/var/www/vhosts/example.com/./logs/error_log.1.gz",
        "/var/www/vhosts/example.com//logs/error_log.1.gz",
        "/var/www/vhosts/example.com/logs/",
        "/var/www/vhosts/example.com/logs/..",
    ],
)
def test_traversal_and_unnormalized_rejected(path):
    with pytest.raises(ValueError):
        validate(path)


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "/var/www/html/index.php",
        "/var/www/vhostsX/example.com/logs/error_log",
        "/var/www/vhosts",
        "/var/www/vhosts/",
        "/var/www/vhosts/example.com",
        "/var/www/vhosts/system/example.com",
        "var/www/vhosts/example.com/logs/error_log.1.gz",
        "logs/error_log.1.gz",
        "",
    ],
)
def test_outside_or_too_broad_rejected(path):
    with pytest.raises(ValueError):
        validate(path)


@pytest.mark.parametrize(
    "path",
    [
        "/var/www/vhosts/example.com/logs/*.gz",
        "/var/www/vhosts/example.com/logs/error_log.?.gz",
        "/var/www/vhosts/example.com/logs/error_log.[1-7].gz",
        "/var/www/vhosts/example.com/logs/a b",
        "/var/www/vhosts/example.com/logs/x;rm -rf /",
        "/var/www/vhosts/example.com/logs/x$(id)",
        "/var/www/vhosts/example.com/logs/x`id`",
        "/var/www/vhosts/example.com/logs/x|id",
        "/var/www/vhosts/example.com/logs/x&id",
        "/var/www/vhosts/example.com/logs/x>y",
        "/var/www/vhosts/example.com/logs/x'y",
        '/var/www/vhosts/example.com/logs/x"y',
        "/var/www/vhosts/example.com/logs/x\ny",
        "/var/www/vhosts/example.com/logs/x\x00y",
        "/var/www/vhosts/example.com/logs/x\\y",
        "/var/www/vhosts/example.com/logs/fähre.log",
    ],
)
def test_special_characters_rejected(path):
    with pytest.raises(ValueError):
        validate(path)


@pytest.mark.parametrize("term", ["-x", "--json", "a b", "x;id", "*.gz"])
def test_search_term_rejected(term):
    with pytest.raises(ValueError):
        server._imunify_search_term(term, "search")


def test_write_tools_require_confirm():
    with pytest.raises(ValueError, match="confirm=true"):
        server.imunify_ignore_add("/var/www/vhosts/example.com/logs/error_log.1.gz")
    with pytest.raises(ValueError, match="confirm=true"):
        server.imunify_ignore_remove("/var/www/vhosts/example.com/logs/error_log.1.gz")


def test_run_builds_quoted_argv(monkeypatch):
    captured = {}

    def fake_exec(command, timeout=None):
        captured["cmd"] = command
        return '{"items": 1}', "", 0

    monkeypatch.setattr(server, "_ssh_exec", fake_exec)
    res = server._imunify_run(["malware", "ignore", "add", "/var/www/vhosts/example.com/logs/a.gz"])
    assert res == {"ok": True, "data": {"items": 1}}
    assert captured["cmd"] == (
        "/usr/bin/imunify360-agent malware ignore add "
        "/var/www/vhosts/example.com/logs/a.gz --json"
    )


def test_run_reports_cli_error(monkeypatch):
    monkeypatch.setattr(
        server, "_ssh_exec", lambda c, timeout=None: ('{"error": ["kaputt"]}', "", 11)
    )
    res = server._imunify_run(["malware", "ignore", "list"])
    assert res["ok"] is False
    assert res["error"] == ["kaputt"]
    assert res["exit_code"] == 11


def test_remove_deletes_only_exact_path_ids(monkeypatch):
    p = "/var/www/vhosts/example.com/logs/error_log.1.gz"
    calls = []

    def fake_exec(command, timeout=None):
        calls.append(command)
        if " ignore list " in command:
            return (
                '{"items": ['
                '{"id": 7, "path": "' + p + '", "added_date": 0, "resource_type": "file"},'
                '{"id": 8, "path": "' + p + '.bak", "added_date": 0, "resource_type": "file"}'
                "]}",
                "",
                0,
            )
        return '{"items": 1}', "", 0

    monkeypatch.setattr(server, "_ssh_exec", fake_exec)
    monkeypatch.setattr(server, "_imunify_check_on_server", lambda path, must_exist: None)
    out = server.imunify_ignore_remove(p, confirm=True)
    assert '"removed_ids": [\n    7\n  ]' in out
    assert calls[-1] == "/usr/bin/imunify360-agent malware ignore delete 7 --json"
