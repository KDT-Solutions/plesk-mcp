"""Tests für memory_limit und Warnungen beim WP-CLI-Aufruf (ohne Serverzugriff)."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


@pytest.mark.parametrize("raw,expected", [
    ("512M", ("512M", True)),
    ("128M", ("256M", True)),
    ("-1", ("2048M", True)),
    ("4G", ("2048M", True)),
    ("1G", ("1024M", True)),
    ("1536m", ("1536M", True)),
    ("786432K", ("768M", True)),
    ("536870912", ("512M", True)),
    (" 512M ", ("512M", True)),
])
def test_memory_limit_values(raw, expected):
    assert server._wp_memory_limit(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "abc", "512MB", "5 12M", "512M;id", "$(id)", "-2", "1.5G", "512T"])
def test_memory_limit_invalid_falls_back(raw):
    assert server._wp_memory_limit(raw) == ("512M", False)


SHOW = """General settings:
display_errors = off
Performance settings:
memory_limit = {perf}
max_execution_time = 30
PHP-FPM settings:
pm = ondemand
Additional directives:
{add}"""


def _fake_exec(text, rc=0):
    def fake(client, cmd, timeout=None, stdin_data=None):
        assert cmd.startswith("plesk bin site --show-php-settings ")
        return text, "", rc
    return fake


def test_domain_memory_limit_from_show(monkeypatch):
    monkeypatch.setattr(server, "_client_exec", _fake_exec(SHOW.format(perf="512M", add="")))
    assert server._wp_domain_memory_limit(object(), "example.com") == "512M"


def test_domain_memory_limit_additional_wins(monkeypatch):
    monkeypatch.setattr(server, "_client_exec",
                        _fake_exec(SHOW.format(perf="256M", add="memory_limit = 1G")))
    assert server._wp_domain_memory_limit(object(), "example.com") == "1G"


def test_domain_memory_limit_unreadable(monkeypatch):
    monkeypatch.setattr(server, "_client_exec", _fake_exec("error", rc=1))
    assert server._wp_domain_memory_limit(object(), "example.com") is None
    assert server._wp_memory_limit(None) == ("512M", False)


def _ctx(mem="512M"):
    return {"domain": "example.com", "login": "kunde", "home": "/var/www/vhosts/example.com",
            "php": "/opt/plesk/php/8.2/bin/php", "memory_limit": mem, "warnings": []}


def test_argv_has_memory_limit_before_phar():
    argv = server._wp_cli_argv(_ctx("768M"), "/var/www/vhosts/example.com/httpdocs", ["plugin", "list"])
    assert argv[:5] == ["/opt/plesk/php/8.2/bin/php", "-d", "memory_limit=768M", "-d", "display_errors=stderr"]
    assert argv[5] == server._WP_CLI_PATH
    assert argv[-2:] == ["plugin", "list"]


@pytest.mark.parametrize("mem", ["512M; id", "-1", "512", "$(id)", ""])
def test_argv_rejects_bad_memory(mem):
    ctx = _ctx(mem)
    if mem == "":
        assert "memory_limit=512M" in server._wp_cli_argv(ctx, "/x", ["a"])
        return
    with pytest.raises(ValueError):
        server._wp_cli_argv(ctx, "/x", ["a"])


def test_warnings_do_not_fail_and_are_collected(monkeypatch):
    out = ('PHP Deprecated:  Creation of dynamic property X::$y is deprecated in /x.php on line 3\n'
           '[{"name": "akismet", "status": "active"}]\n')
    err = ("PHP Warning:  Undefined array key \"a\" in /y.php on line 9\n"
           "Deprecated: strlen(): Passing null in /z.php on line 1\n")
    seen = {}

    def fake_run(client, ctx, argv, cwd, stdin_data=None, timeout=None):
        seen["argv"] = argv
        return out, err, 0

    monkeypatch.setattr(server, "_wp_run_as_user", fake_run)
    ctx = _ctx()
    text = server._wp_cli(object(), ctx, "/var/www/vhosts/example.com/httpdocs", ["plugin", "list"])
    assert server._wp_json(text) == [{"name": "akismet", "status": "active"}]
    assert len(ctx["warnings"]) == 3
    assert "memory_limit=512M" in seen["argv"]


def test_exit_code_decides_error_without_warnings(monkeypatch):
    err = ("PHP Deprecated:  something in /x.php on line 1\n"
           "PHP Fatal error:  Allowed memory size of 134217728 bytes exhausted in /a.php\n")
    monkeypatch.setattr(server, "_wp_run_as_user", lambda *a, **k: ("", err, 255))
    with pytest.raises(server.SSHError) as e:
        server._wp_cli(object(), _ctx(), "/x", ["plugin", "list"])
    assert "Fatal error" in str(e.value) and "Deprecated" not in str(e.value)


def test_reporting_adds_memory_and_warnings():
    @server._wp_reporting
    def tool():
        rep = server._wp_report.get()
        rep["memory_limit"], rep["from_domain"] = "512M", False
        rep["warnings"].append("PHP Deprecated: x")
        return json.dumps({"ergebnis": "ok"})

    res = json.loads(tool())
    assert res["php_memory_limit"] == "512M"
    assert "nicht ermittelbar" in res["hinweis_memory_limit"]
    assert res["warnungen"] == ["PHP Deprecated: x"]
    assert server._wp_report.get() is None


def test_reporting_without_context_unchanged():
    @server._wp_reporting
    def tool():
        return '{"a": 1}'

    assert tool() == '{"a": 1}'
