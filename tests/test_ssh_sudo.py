"""Betrieb ohne root-Login (PLESK_SSH_SUDO): Kommandos und SFTP gehen als
"sudo -n -H -- ..." an den Server. Getestet gegen einen lokalen paramiko-
Mini-SSH-Server, der das sudo-Präfix prüft, abschneidet und den Rest lokal
ausführt - es braucht also weder sudo noch root auf dem Testrechner."""

import json
import os
import socket
import stat
import subprocess
import sys
import threading

import paramiko
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import server  # noqa: E402
from test_ssh_exec import _run  # noqa: E402

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="benötigt /bin/bash und coreutils")

SUDO = "sudo -n -H -- "
SFTP_SERVER = "/usr/libexec/openssh/sftp-server"


class _SFTP(paramiko.SFTPServerInterface):
    """Kleinster lesender SFTP-Server auf dem lokalen Dateisystem."""

    def list_folder(self, path):
        out = []
        for name in os.listdir(path):
            attr = paramiko.SFTPAttributes.from_stat(os.lstat(os.path.join(path, name)))
            attr.filename = name
            out.append(attr)
        return out

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(path))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    lstat = stat

    def open(self, path, flags, attr):
        try:
            f = open(path, "rb")
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        handle = paramiko.SFTPHandle(flags)
        handle.readfile = f
        return handle


class _Server(paramiko.ServerInterface):
    def __init__(self, state):
        self.state = state

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if password == "geheim" else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        raw = command.decode()
        self.state["commands"].append(raw)
        if self.state["refuse"]:
            # sudo ohne passenden sudoers-Eintrag
            cmd = "echo 'sudo: a password is required' >&2; exit 1"
        elif not raw.startswith(SUDO):
            cmd = "echo 'ohne sudo aufgerufen' >&2; exit 97"
        elif raw == SUDO + SFTP_SERVER:
            paramiko.SFTPServer(channel, "sftp", self, _SFTP).start()
            return True
        else:
            cmd = raw[len(SUDO):]
        threading.Thread(target=_run, args=(channel, cmd), daemon=True).start()
        return True


@pytest.fixture(scope="module")
def sshd():
    key = paramiko.ECDSAKey.generate()
    state = {"commands": [], "refuse": False}
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    sock.settimeout(0.5)
    transports = []
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            t = paramiko.Transport(conn)
            t.add_server_key(key)
            t.start_server(server=_Server(state))
            transports.append(t)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield {"port": sock.getsockname()[1], "key": key, "state": state}
    stop.set()
    thread.join()
    for t in transports:
        t.close()
    sock.close()


@pytest.fixture
def sudo_ssh(sshd, monkeypatch):
    monkeypatch.setattr(server, "_SSH_HOST", "127.0.0.1")
    monkeypatch.setattr(server, "_SSH_PORT", sshd["port"])
    monkeypatch.setattr(server, "_SSH_USER", "plesk-mcp")
    monkeypatch.setattr(server, "_SSH_PASSWORD", "geheim")
    monkeypatch.setattr(server, "_SSH_KEY_PATH", None)
    monkeypatch.setattr(server, "_SSH_KNOWN_HOSTS", "")
    monkeypatch.setattr(server, "_SSH_HOST_KEY", f"{sshd['key'].get_name()} {sshd['key'].get_base64()}")
    monkeypatch.setattr(server, "_SSH_TIMEOUT", 5)
    monkeypatch.setattr(server, "_SSH_SUDO", True)
    monkeypatch.setattr(server, "_SSH_SUDO_SHELL", "/bin/bash")
    monkeypatch.setattr(server, "_SSH_SUDO_SFTP_SERVER", SFTP_SERVER)
    sshd["state"]["commands"].clear()
    sshd["state"]["refuse"] = False
    return sshd["state"]


def test_wrap_format():
    assert server._sudo_wrap("plesk bin domain -i example.com") == (
        "sudo -n -H -- /bin/bash -c 'plesk bin domain -i example.com'"
    )


def test_exec_runs_through_sudo(sudo_ssh):
    out, err, rc = server._ssh_exec("echo hallo; echo fehler >&2; exit 3")
    assert (out, err, rc) == ("hallo\n", "fehler\n", 3)
    assert sudo_ssh["commands"] == ["sudo -n -H -- /bin/bash -c 'echo hallo; echo fehler >&2; exit 3'"]
    assert server.ssh_run("printf abc") == "abc"


@pytest.mark.parametrize("command,expected", [
    ("printf '%s' \"a'b\"", "a'b"),
    ("x='zwei worte'; printf '%s|%s' \"$x\" '$x'", "zwei worte|$x"),
    ("printf 'eins\\nzwei' | wc -l; printf '%s' \"$(echo drin)\"", "1\ndrin"),
    ("if [[ abc == a* ]]; then printf ja; fi", "ja"),
    ("printf '%s' 'zeile1\nzeile2'", "zeile1\nzeile2"),
])
def test_quoting_survives_the_extra_shell(sudo_ssh, command, expected):
    out, err, rc = server._ssh_exec(command)
    assert (out, err, rc) == (expected, "", 0)
    assert sudo_ssh["commands"][-1].startswith(SUDO + "/bin/bash -c ")


def test_client_exec_passes_stdin_through(sudo_ssh):
    client = server._ssh_connect()
    try:
        assert isinstance(client, server._SudoSSHClient)
        out, err, rc = server._client_exec(client, "cat", stdin_data=b"eingabe")
        assert (out, rc) == ("eingabe", 0)
    finally:
        client.close()
    assert sudo_ssh["commands"] == ["sudo -n -H -- /bin/bash -c cat"]


def test_helper_session_uses_sudo(sudo_ssh):
    session = server._SSHHelperSession("gibt-es-nicht", {})
    session.finish()
    assert len(sudo_ssh["commands"]) == 1
    assert sudo_ssh["commands"][0].startswith(SUDO + "/bin/bash -c ")
    assert " -I -c " in sudo_ssh["commands"][0]


def test_sftp_runs_through_sudo(sudo_ssh, tmp_path):
    (tmp_path / "datei.txt").write_bytes(b"inhalt")
    client = server._ssh_connect()
    try:
        sftp = client.open_sftp()
        try:
            assert sftp.listdir(str(tmp_path)) == ["datei.txt"]
            assert stat.S_ISREG(sftp.stat(str(tmp_path / "datei.txt")).st_mode)
            with sftp.open(str(tmp_path / "datei.txt"), "rb") as f:
                assert f.read() == b"inhalt"
        finally:
            sftp.close()
    finally:
        client.close()
    assert sudo_ssh["commands"] == [SUDO + SFTP_SERVER]


def test_sudo_refusal_is_a_clear_error(sudo_ssh):
    sudo_ssh["refuse"] = True
    with pytest.raises(server.SSHError, match="sudo auf dem Plesk-Server hat das Kommando abgelehnt"):
        server._ssh_exec("plesk bin domain -l")
    client = server._ssh_connect()
    try:
        with pytest.raises(server.SSHError, match="sudo auf dem Plesk-Server"):
            server._client_exec(client, "true")
        with pytest.raises(server.SSHError, match="SFTP ueber sudo konnte nicht gestartet werden"):
            client.open_sftp()
    finally:
        client.close()


def test_exit_code_one_without_sudo_message_is_not_an_error(sudo_ssh):
    out, err, rc = server._ssh_exec("echo 'grep: nichts gefunden' >&2; exit 1")
    assert (out, err, rc) == ("", "grep: nichts gefunden\n", 1)


def test_without_sudo_nothing_changes(sudo_ssh, monkeypatch):
    monkeypatch.setattr(server, "_SSH_SUDO", False)
    client = server._ssh_connect()
    try:
        assert type(client) is paramiko.SSHClient
    finally:
        client.close()
    assert server._ssh_command("uptime") == "uptime"
    out, err, rc = server._ssh_exec("true")
    assert rc == 97 and "ohne sudo aufgerufen" in err
    assert sudo_ssh["commands"] == ["true"]


def _config(env: dict[str, str]) -> dict:
    code = (
        "import json, server\n"
        "print('RESULT ' + json.dumps({'sudo': server._SSH_SUDO, 'warnings': server._check_startup_config(),"
        "'version': json.loads(server.get_version())}))\n"
    )
    full = {**os.environ, "MCP_API_KEY": "test-mcp-api-key-0123456789abcdef",
            "PLESK_SSH_HOST_KEY": "ssh-ed25519 AAAA", **env}
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=full,
                          capture_output=True, text=True, timeout=120, check=True)
    line = [l for l in proc.stdout.splitlines() if l.startswith("RESULT ")][-1]
    return json.loads(line[len("RESULT "):])


def test_sudo_only_for_non_root_user():
    res = _config({"PLESK_SSH_USER": "plesk-mcp", "PLESK_SSH_SUDO": "true"})
    assert res["sudo"] is True and res["version"]["ssh_sudo"] is True
    assert not any("PLESK_SSH_SUDO" in w for w in res["warnings"])

    res = _config({"PLESK_SSH_USER": "root", "PLESK_SSH_SUDO": "true"})
    assert res["sudo"] is False
    assert any("sudo wird nicht" in w for w in res["warnings"])

    res = _config({"PLESK_SSH_USER": "plesk-mcp", "PLESK_SSH_SUDO": ""})
    assert res["sudo"] is False
    assert any("PLESK_SSH_SUDO=true setzen" in w for w in res["warnings"])

    res = _config({"PLESK_SSH_USER": "root", "PLESK_SSH_SUDO": ""})
    assert res["sudo"] is False
    assert not any("PLESK_SSH_SUDO" in w for w in res["warnings"])
