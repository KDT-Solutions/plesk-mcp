"""run_diagnostic: Positiv-Listen je Utility (ohne Serverzugriff). Die
Umgehungen aus dem Security-Review müssen abgelehnt werden, die üblichen
Diagnose-Kommandos bleiben erlaubt."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


@pytest.fixture
def run(monkeypatch):
    calls = []

    def fake_ssh_run(cmd, timeout=None, max_runtime=None):
        calls.append((cmd, timeout, max_runtime))
        return "ok"

    monkeypatch.setattr(server, "ssh_run", fake_ssh_run)
    monkeypatch.setattr(server, "_DIAG_TIMEOUT", 60)

    def _run(cmd):
        return server.run_diagnostic(cmd), calls

    _run.calls = calls
    return _run


REJECTED = [
    # plesk bin: Kurzoptionen = schreibende Aktionen
    "plesk bin domain -r example.com",
    "plesk bin subscription -r example.com",
    "plesk bin customer -r kunde",
    "plesk bin domain -c evil.example -owner admin -hosting true",
    "plesk bin domain -u example.com -status disabled",
    "plesk bin extension -i /tmp/ext.zip",
    "plesk bin extension --install /tmp/ext.zip",
    "plesk bin domain --create x",
    "plesk bin domain --info x -c",
    "plesk bin domain --info x --remove",
    "plesk bin dns --add example.com -a www -ip 203.0.113.1",
    "plesk bin admin --show-password",
    "plesk bin domain --xml-info x --update",
    "plesk bin",
    "plesk bin domain",
    "plesk bin 'dom ain' -i x",
    "plesk version extra",
    # WP-CLI komplett gesperrt
    "plesk ext wp-toolkit --wp-cli -instance-id 1 -- plugin install x",
    "plesk ext wp-toolkit --wp-cli -instance-id 1 -- db query 'DROP TABLE wp_users'",
    "plesk ext wp-toolkit --list -wp-cli",
    "plesk ext wp-toolkit --clear-cache",
    "plesk ext wp-toolkit --update -instance-id 1",
    # plesk db/login/repair/sbin
    "plesk db",
    "plesk db -e 'select 1'",
    "plesk login",
    "plesk repair all",
    "plesk sbin httpdmng --reconfigure-all",
    # journalctl schreibend
    "journalctl --vacuum-time=1s",
    "journalctl --vacuum-size 10M",
    "journalctl --rotate",
    "journalctl --flush",
    "journalctl --sync",
    "journalctl --relinquish-var",
    "journalctl --setup-keys",
    "journalctl --cursor-file=/root/c -n 1",
    "journalctl -f -u nginx",
    "journalctl -fu nginx",
    "journalctl --follow",
    # dmesg
    "dmesg -C",
    "dmesg -c",
    "dmesg --clear",
    "dmesg --read-clear",
    "dmesg -n 1",
    "dmesg -w",
    # hostname
    "hostname neuername",
    "hostname -F /etc/hostname",
    "hostname -f",
    # systemctl nicht lesend
    "systemctl restart nginx",
    "systemctl mask nginx",
    "systemctl isolate rescue",
    "systemctl list-timers",
    "systemctl",
    # endlose/riesige Quellen
    "cat /dev/zero",
    "cat /dev/urandom",
    "cat /dev/random",
    "head -c 10 /dev/./urandom",
    "cat /proc/kcore",
    "grep x /dev/zero",
    "grep --file=/dev/zero /var/log/messages",
    "tail -f /var/log/nginx/error.log",
    "tail -F /var/log/nginx/error.log",
    "tail -fn 10 /var/log/nginx/error.log",
    "tail --follow=name /var/log/nginx/error.log",
    "tail --pid=1 /var/log/nginx/error.log",
    # find/du/grep -r nur in erlaubten Basen
    "find / -name x",
    "find",
    "find -name x",
    "find /etc -name x",
    "find /var/log/../.. -name x",
    "find /var/www/vhosts/../../etc -name x",
    "find -L /var/log -name x",
    "find -H /var/log -name x",
    "find /var/log -follow -name x",
    "find /var/log -delete",
    "find /var/log -name x -exec cat {} +",
    "find /var/log -name x -execdir cat {} +",
    "find /var/log -name x -ok cat {} +",
    "find /var/log -fprint /tmp/x",
    "find /var/log -fprintf /tmp/x %p",
    "find /var/log -fls /tmp/x",
    "find /var/log -fprint0 /tmp/x",
    "find /var/log/${X:-..}/.. -name x",
    "find ~ -name x",
    "find /var/log /root -name x",
    "du -a /",
    "du",
    "du -s /root",
    "du -L /var/log",
    "du --dereference /var/log",
    "du --files0-from=/tmp/x",
    "grep -r x /",
    "grep -r x /etc",
    "grep -rn x /etc/shadow",
    "grep -r x",
    "grep -r -efoo / /var/log",
    "grep -r -e foo /var/log /root",
    "grep --recursive x /",
    "grep -R x /var/log",
    "grep --dereference-recursive x /var/log",
    "zgrep -r x /",
    # Pipe ohne Leerzeichen: zweites Programm muss geprueft werden
    "cat /var/log/messages|plesk bin domain -r example.com",
    "cat /var/log/messages|bash",
    # Shell-Konstrukte
    "ls; rm -rf /",
    "ls && reboot",
    "ls > /tmp/x",
    "ls $(id)",
    "ls `id`",
    "ls\nid",
    # Programme ausserhalb der Whitelist
    "imunify360-agent malware ignore list",
    "mysql -e 'select 1'",
    "bash -c id",
    "python3 -c 'import os'",
]


@pytest.mark.parametrize("cmd", REJECTED)
def test_rejected(run, cmd):
    with pytest.raises(server.SSHError):
        run(cmd)
    # Nichts wurde ausgeführt
    assert run.calls == []


ALLOWED = [
    "plesk version",
    "plesk bin domain -i example.com",
    "plesk bin domain --info example.com",
    "plesk bin subscription -i example.com",
    "plesk bin customer -l",
    "plesk bin domain --list",
    "plesk bin dns --info example.com",
    "plesk bin mail -i info@example.com",
    "plesk bin site --show-php-settings example.com",
    "plesk bin domain --show-web-server-settings example.com",
    "plesk bin php_handler --list",
    "plesk bin extension --list",
    "plesk bin extension -l",
    "plesk bin server_pref -s",
    "plesk bin service_plan -i 'Default Domain'",
    "plesk ext wp-toolkit --list",
    "plesk ext wp-toolkit --info -instance-id 3",
    "plesk ext wp-toolkit --list -format json",
    "journalctl -k --since '2026-09-17 21:00'",
    "journalctl -u nginx --no-pager -n 100 --since -1h",
    "journalctl -k --no-pager | grep -iE 'oom|killed process'",
    "journalctl -p err -b --no-pager",
    "dmesg -T",
    "dmesg --level=err",
    "dmesg -T | tail -n 50",
    "hostname",
    "uptime",
    "free -m",
    "df -h",
    "top -b -n 1 | head -n 20",
    "ps aux --sort=-%mem | head -n 10",
    "ps aux | wc -l",
    "systemctl status nginx",
    "systemctl is-active php-fpm",
    "systemctl list-units --type=service --state=failed",
    "systemctl show nginx -p ActiveState",
    "systemctl cat nginx",
    "ls -la /var/www/vhosts/example.com/httpdocs",
    "ls -laR /var/www/vhosts/example.com/httpdocs/wp-content/uploads | head -n 100",
    "cat /var/www/vhosts/example.com/httpdocs/wp-config.php",
    "cat /var/www/vhosts/system/example.com/conf/vhost_nginx.conf",
    "head -n 50 /var/log/nginx/error.log",
    "tail -n 200 /var/log/nginx/error.log",
    "tail -n -1 /var/log/messages",
    "tail -c 1000 /var/log/messages",
    "grep -E 'err|warn' /var/log/nginx/error.log | tail -n 20",
    "grep -c 'POST /xmlrpc.php' /var/www/vhosts/example.com/logs/access_log",
    "grep -i 'oom' /var/log/messages",
    "grep -r 'client_max_body_size' /etc/nginx",
    "grep -rn --include='*.conf' server_name /etc/nginx/conf.d /etc/httpd",
    "grep -r -e 'Fatal error' /var/www/vhosts/example.com/logs",
    "grep -rl x /var/www/vhosts/example.com/httpdocs/wp-content/plugins",
    "grep -r x -- /var/log/nginx",
    "zcat /var/log/nginx/error.log-20260101.gz | grep -c err",
    "zgrep -E 'x' /var/log/nginx/error.log-20260101.gz",
    "zgrep -r x /var/log/nginx",
    "find /var/www/vhosts/example.com/httpdocs -name '*.php' -mtime -1",
    "find /var/www/vhosts/example.com/httpdocs -type f -size +50M -printf '%s %p\\n'",
    "find /var/log/nginx /var/log/httpd -name '*.log' -newer /var/log/messages",
    "find -P /var/log -maxdepth 1 -type f",
    "find /var/www/vhosts/example.com/httpdocs \\( -name '*.php' -o -name '*.phtml' \\) -mtime -1",
    "find /var/www/vhosts/example.com/httpdocs ! -user kunde",
    "find /usr/local/psa/admin/conf -name '*.conf'",
    "du -s /var/www/vhosts/example.com/httpdocs",
    "du -d 1 /var/www/vhosts/example.com",
    "du --max-depth=1 -h /var/log",
    "du -ah /var/www/vhosts/example.com/httpdocs/wp-content/uploads | tail -n 20",
    "lveinfo --period=1h --show-all",
    "lveps -p",
    "lve_readall",
    "wc -l /var/www/vhosts/example.com/logs/access_log",
]


@pytest.mark.parametrize("cmd", ALLOWED)
def test_allowed(run, cmd):
    out, calls = run(cmd)
    assert out == "ok" and len(calls) == 1


def test_remote_command_runs_under_timeout(run):
    out, calls = run("tail -n 5 /var/log/messages")
    remote, timeout, max_runtime = calls[0]
    assert remote.startswith("timeout -k 5 60 sh -c ")
    assert "tail -n 5 /var/log/messages" in remote
    assert timeout == 70 and max_runtime == 80


def test_quoted_pipe_in_pattern_stays_pattern(run):
    out, calls = run("grep -E 'error|warn' /var/log/nginx/error.log")
    assert "'error|warn'" in calls[0][0]


def test_split_options():
    opts, pos = server._split_options(["-rn", "-e", "foo", "-efoo2", "--include=*.c", "--exclude", "x", "a", "--", "-b"],
                                      set("e"), {"exclude"})
    assert opts == [("r", ""), ("n", ""), ("e", "foo"), ("e", "foo2"), ("include", "*.c"), ("exclude", "x")]
    assert pos == ["a", "-b"]


def test_search_log_lines_clamped(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "ssh_run", lambda cmd, timeout=None, max_runtime=None: calls.append(cmd) or "")
    server.search_log("example.com", lines=-1)
    server.search_log("example.com", lines=10 ** 9)
    server.search_log("example.com", lines=0, pattern="x")
    server.search_main_nginx_log("x", lines=99999)
    assert calls[0].startswith("tail -n 1 ") and " -n 5000 " in calls[1]
    assert calls[2].endswith("| tail -n 1") and calls[3].endswith("| tail -n 5000")
    with pytest.raises(ValueError):
        server.search_log("example.com", lines="viele")


def test_mysql_exec_keeps_password_on_server(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "ssh_run", lambda cmd, timeout=None, max_runtime=None: calls.append(cmd) or "1\n")
    assert server._mysql_exec("SELECT 1", database="kunde_db", timeout=5) == "1\n"
    cmd = calls[0]
    assert cmd.startswith('MYSQL_PWD="$(cat /etc/psa/.psa.shadow)" mysql -uadmin ')
    assert "--binary-mode" in cmd and "kunde_db" in cmd and "-p" not in cmd.replace("--", "")
    monkeypatch.setattr(server, "ssh_run", lambda cmd, timeout=None, max_runtime=None:
                        "ERROR 1045 (28000): Access denied for user 'admin'@'localhost'")
    with pytest.raises(RuntimeError, match="Anmeldung"):
        server._mysql_exec("SELECT 1")


@pytest.mark.parametrize("path", ["../server", "domains/../../login", "domains?x=1", "domains#a", "domains/1 status",
                                  "", "/", "domains;x", "dom%2Fains", "domäins"])
def test_plesk_api_get_rejects_bad_paths(monkeypatch, path):
    monkeypatch.setattr(server, "_API_HOST", "plesk.example.com")
    monkeypatch.setattr(server, "_API_KEY", "k")
    monkeypatch.setattr(server.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("kein Request")))
    with pytest.raises(ValueError, match="API-Pfad"):
        server.plesk_api_get(path)


def test_plesk_api_get_ok_path(monkeypatch):
    monkeypatch.setattr(server, "_API_HOST", "plesk.example.com")
    monkeypatch.setattr(server, "_API_KEY", "k")
    seen = {}

    class R:
        text = "[]"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(server.httpx, "get", lambda url, **k: seen.update(url=url, params=k["params"]) or R())
    assert server.plesk_api_get("/domains/1/status/", "name=example.com") == "[]"
    assert seen == {"url": "https://plesk.example.com:8443/api/v2/domains/1/status", "params": {"name": "example.com"}}
