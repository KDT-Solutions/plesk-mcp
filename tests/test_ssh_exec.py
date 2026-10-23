"""SSH-Ausführung gegen einen lokalen Mini-SSH-Server (paramiko-Serverseite):
Host-Key-Pinning, Ausgabe-Obergrenze, Inaktivitäts-Timeout und harte Deadline
von _ssh_exec/_client_exec. Kein echter Plesk-Server nötig."""

import os
import socket
import subprocess
import sys
import threading

import paramiko
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="benötigt /bin/sh und coreutils")


class _Server(paramiko.ServerInterface):
    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if password == "geheim" else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        threading.Thread(target=_run, args=(channel, command.decode()), daemon=True).start()
        return True


def _run(chan, command):
    """Führt das Kommando lokal aus und pumpt stdout/stderr in den Kanal."""
    p = subprocess.Popen(command, shell=True, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def feed_stdin():
        try:
            while True:
                data = chan.recv(65536)
                if not data:
                    break
                p.stdin.write(data)
                p.stdin.flush()
        except (OSError, EOFError, socket.error):
            pass
        try:
            p.stdin.close()
        except OSError:
            pass

    threading.Thread(target=feed_stdin, daemon=True).start()

    def pump(stream, send):
        while True:
            data = stream.read(65536)
            if not data:
                break
            try:
                send(data)
            except (OSError, EOFError, socket.error):
                break

    threads = [threading.Thread(target=pump, args=(p.stdout, chan.sendall)),
               threading.Thread(target=pump, args=(p.stderr, chan.sendall_stderr))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if p.poll() is None:
        p.kill()
    p.wait()
    try:
        chan.send_exit_status(p.returncode)
        chan.close()
    except (OSError, EOFError):
        pass


@pytest.fixture(scope="module")
def sshd():
    key = paramiko.ECDSAKey.generate()
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
            t.start_server(server=_Server())
            transports.append(t)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield {"port": sock.getsockname()[1], "key": key}
    stop.set()
    thread.join()
    for t in transports:
        t.close()
    sock.close()


@pytest.fixture
def ssh(sshd, monkeypatch):
    monkeypatch.setattr(server, "_SSH_HOST", "127.0.0.1")
    monkeypatch.setattr(server, "_SSH_PORT", sshd["port"])
    monkeypatch.setattr(server, "_SSH_USER", "test")
    monkeypatch.setattr(server, "_SSH_PASSWORD", "geheim")
    monkeypatch.setattr(server, "_SSH_KEY_PATH", None)
    monkeypatch.setattr(server, "_SSH_KNOWN_HOSTS", "")
    monkeypatch.setattr(server, "_SSH_HOST_KEY", f"{sshd['key'].get_name()} {sshd['key'].get_base64()}")
    monkeypatch.setattr(server, "_SSH_TIMEOUT", 5)
    return sshd


def test_exec_roundtrip(ssh):
    out, err, rc = server._ssh_exec("echo hallo; echo fehler >&2; exit 3")
    assert (out, err, rc) == ("hallo\n", "fehler\n", 3)
    assert server.ssh_run("printf abc") == "abc"


def test_hostkey_required_and_pinned(ssh, monkeypatch):
    monkeypatch.setattr(server, "_SSH_HOST_KEY", "")
    with pytest.raises(server.SSHError, match="PLESK_SSH_HOST_KEY"):
        server._ssh_exec("true")
    other = paramiko.ECDSAKey.generate()
    monkeypatch.setattr(server, "_SSH_HOST_KEY", f"{other.get_name()} {other.get_base64()}")
    with pytest.raises(server.SSHError, match="passt nicht"):
        server._ssh_exec("true")


def test_output_truncated_at_limit(ssh, monkeypatch):
    monkeypatch.setattr(server, "_SSH_MAX_OUTPUT", 200_000)
    out, err, rc = server._ssh_exec("head -c 5000000 /dev/zero | tr '\\0' x")
    assert out.endswith(server._TRUNCATED_NOTE)
    assert len(out) <= 200_000 + len(server._TRUNCATED_NOTE)
    assert "gekürzt" in server.ssh_run("head -c 5000000 /dev/zero | tr '\\0' x")


def test_stderr_truncated_too(ssh, monkeypatch):
    monkeypatch.setattr(server, "_SSH_MAX_OUTPUT", 100_000)
    out, err, rc = server._ssh_exec("head -c 3000000 /dev/zero | tr '\\0' y >&2")
    assert len(err) <= 100_000 and "gekürzt" in out


def test_hard_deadline_with_continuous_output(ssh, monkeypatch):
    monkeypatch.setattr(server, "_SSH_MAX_OUTPUT", 500 * 1024 * 1024)
    with pytest.raises(server.SSHError, match="Deadline"):
        server._ssh_exec("yes", timeout=5, max_runtime=1)


def test_inactivity_timeout(ssh):
    with pytest.raises(socket.timeout):
        server._ssh_exec("sleep 5", timeout=1, max_runtime=30)


def test_client_exec_stdin_and_limit(ssh, monkeypatch):
    monkeypatch.setattr(server, "_SSH_MAX_OUTPUT", 50_000)
    client = server._ssh_connect()
    try:
        out, err, rc = server._client_exec(client, "cat", stdin_data=b"eingabe")
        assert (out, rc) == ("eingabe", 0)
        out, _, _ = server._client_exec(client, "head -c 1000000 /dev/zero | tr '\\0' z")
        assert out.endswith(server._TRUNCATED_NOTE)
    finally:
        client.close()


def test_run_diagnostic_uses_timeout_wrapper(ssh, monkeypatch):
    monkeypatch.setattr(server, "_DIAG_TIMEOUT", 1)
    assert server.run_diagnostic("uptime") != ""
    out = server.run_diagnostic("cat /var/log/gibt-es-nicht | wc -l")
    assert "0" in out
    # Pipeline laeuft unter "timeout <s>": ein haengendes Kommando wird beendet
    monkeypatch.setattr(server, "ALLOWED_COMMAND_PREFIXES", server.ALLOWED_COMMAND_PREFIXES | {"sleep"})
    out = server.run_diagnostic("sleep 10")
    assert "exit code: 124" in out
