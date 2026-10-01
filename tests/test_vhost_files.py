"""Tests für Upload, Fetch und Dateiverwaltung (ohne Plesk-Server).

Der Datei-Helper (_HELPER_SRC) läuft hier lokal als Subprozess gegen ein
temporäres Vhost-Verzeichnis - dieselbe Logik wie per SSH auf dem Server.
Nur das SSH-Transportmittel und die Plesk-DB-Abfrage werden ersetzt.
Benötigt Linux und root (chown); sonst werden die Tests übersprungen.

Ausführen: pip install -r requirements.txt pytest && pytest
"""

import base64
import hashlib
import json
import os
import pwd
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or os.geteuid() != 0,
    reason="benötigt Linux und root (chown im Helper)",
)

DOMAIN = "example.com"


class LocalSession:
    def __init__(self, op, args):
        payload = base64.b64encode(json.dumps(args).encode()).decode()
        self.p = subprocess.Popen([sys.executable, "-I", "-c", server._HELPER_SRC, op, payload],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def send(self, data):
        self.p.stdin.write(data)
        self.p.stdin.flush()

    def finish(self):
        self.p.stdin.close()
        out = self.p.stdout.read()
        self.p.wait()
        return out

    def abort(self):
        self.p.kill()
        self.p.wait()


@pytest.fixture
def vhost(tmp_path, monkeypatch):
    user = pwd.getpwnam("nobody")
    root = tmp_path / "vhosts"
    base = root / DOMAIN
    for d in ("httpdocs", "httpdocs/img", "sub.example.com", "conf", "logs"):
        (base / d).mkdir(parents=True)
    for d in (base, base / "httpdocs", base / "httpdocs/img", base / "sub.example.com"):
        os.chown(d, user.pw_uid, user.pw_gid)
    (base / "httpdocs/old.txt").write_text("old")
    os.chown(base / "httpdocs/old.txt", user.pw_uid, user.pw_gid)
    ctx = {"domain": DOMAIN, "base": str(base), "login": "nobody", "uid": user.pw_uid,
           "gid": user.pw_gid, "docroots": [["httpdocs"], ["sub.example.com"]]}
    monkeypatch.setattr(server, "_helper_session_factory", LocalSession)
    monkeypatch.setattr(server, "_vhost_ctx", lambda d: dict(ctx))
    monkeypatch.setattr(server, "_VHOST_BASE", str(root))
    monkeypatch.setattr(server, "_HTTP_MODE", True)
    monkeypatch.setattr(server, "_PUBLIC_BASE_URL", "https://upload.example.com")
    monkeypatch.setattr(server, "_UPLOAD_MIN_FREE", 0)
    server._uploads.clear()
    server._upload_rate.clear()
    return base


@pytest.fixture
def client(vhost):
    from starlette.testclient import TestClient
    return TestClient(server._build_http_app())


def begin(path, **kw):
    return json.loads(server.upload_begin(DOMAIN, path, confirm=True, **kw))


def put(client, info, data, token=None, upload_id=None):
    return client.put(f"/upload/{upload_id or info['upload_id']}", content=data,
                      headers={"Authorization": f"Bearer {token or info['token']}"})


# --- Teil A: Pfade und Upload ------------------------------------------------


@pytest.mark.parametrize("path", ["../x", "/etc/passwd", "httpdocs/../../x", "httpdocs/*.php",
                                  "httpdocs/a?b", "httpdocs/./a", "", "httpdocs//a", "httpdocs\\a"])
def test_path_traversal_and_globs_rejected(path):
    with pytest.raises(ValueError):
        server._strict_parts(path)


def test_upload_roundtrip_and_single_use(client, vhost):
    info = begin("httpdocs/img/logo.png")
    data = os.urandom(4096)
    r = put(client, info, data)
    assert r.status_code == 200, r.text
    assert r.json()["sha256"] == hashlib.sha256(data).hexdigest()
    assert (vhost / "httpdocs/img/logo.png").read_bytes() == data
    st = os.stat(vhost / "httpdocs/img/logo.png")
    assert (st.st_uid, oct(st.st_mode & 0o777)) == (pwd.getpwnam("nobody").pw_uid, "0o644")
    assert put(client, info, data).status_code == 401
    status = json.loads(server.upload_status(info["upload_id"]))
    assert status["status"] == "completed" and status["size"] == 4096


def test_new_dirs_get_owner_and_0755(client, vhost):
    info = begin("httpdocs/neu/tief/a.bin")
    assert put(client, info, b"x").status_code == 200
    st = os.stat(vhost / "httpdocs/neu")
    assert st.st_uid == pwd.getpwnam("nobody").pw_uid and oct(st.st_mode & 0o777) == "0o755"


def test_upload_token_expired(client, vhost):
    info = begin("httpdocs/a.bin")
    server._uploads[info["upload_id"]]["expires"] = time.time() - 1
    assert put(client, info, b"x").status_code == 401
    assert json.loads(server.upload_status(info["upload_id"]))["status"] == "expired"
    assert not (vhost / "httpdocs/a.bin").exists()


def test_upload_token_bound_to_upload(client, vhost):
    a = begin("httpdocs/a.bin")
    b = begin("httpdocs/b.bin")
    r1 = put(client, a, b"x", upload_id=b["upload_id"])
    r2 = put(client, a, b"x", upload_id="unbekannt-unbekannt-1")
    r3 = put(client, a, b"x", token="falsch")
    assert r1.status_code == r2.status_code == r3.status_code == 401
    assert r1.json() == r2.json() == r3.json() == {"error": "unauthorized"}
    assert not (vhost / "httpdocs/b.bin").exists()
    assert put(client, a, b"x").status_code == 200


def test_upload_size_limit(client, vhost):
    info = begin("httpdocs/gross.bin", max_bytes=10)
    assert put(client, info, b"x" * 20).status_code == 413
    assert not (vhost / "httpdocs/gross.bin").exists()
    assert [p.name for p in (vhost / "httpdocs").iterdir() if ".mcp-" in p.name] == []


def test_upload_size_limit_streaming(vhost):
    args = server._helper_args(str(vhost), parts=["httpdocs", "s.bin"], max_bytes=10)
    with pytest.raises(server._HelperError) as e:
        server._helper_put(args, [b"x" * 6, b"x" * 6])
    assert e.value.code == "too_large"
    assert not (vhost / "httpdocs/s.bin").exists()


def test_upload_sha256_mismatch_keeps_old(client, vhost):
    info = begin("httpdocs/old.txt", sha256="0" * 64)
    assert put(client, info, b"neu").status_code == 422
    assert (vhost / "httpdocs/old.txt").read_text() == "old"
    assert [p.name for p in (vhost / "httpdocs").iterdir()] == ["img", "old.txt"]


def test_upload_executable_needs_flag(vhost):
    for p in ("httpdocs/x.php", "httpdocs/.htaccess", "httpdocs/.user.ini", "httpdocs/a.php.jpg"):
        with pytest.raises(ValueError):
            begin(p)
    assert begin("httpdocs/x.php", allow_executable=True)["upload_id"]


def test_upload_requires_confirm(vhost):
    with pytest.raises(ValueError):
        server.upload_begin(DOMAIN, "httpdocs/a.bin")


def test_upload_backup_on_overwrite(client, vhost):
    info = begin("httpdocs/old.txt")
    r = put(client, info, b"neu")
    assert r.status_code == 200
    assert (vhost / "httpdocs/old.txt").read_text() == "neu"
    baks = [p for p in (vhost / "httpdocs").iterdir() if p.name.startswith("old.txt.bak-")]
    assert len(baks) == 1 and baks[0].read_text() == "old"
    assert server._BACKUP_SUFFIX_RE.search(baks[0].name)


def test_symlink_target_and_parent_rejected(vhost, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, vhost / "httpdocs/linkdir")
    os.symlink(outside / "f", vhost / "httpdocs/link.txt")
    with pytest.raises(ValueError, match="Symlink"):
        begin("httpdocs/linkdir/x.bin")
    with pytest.raises(ValueError, match="Symlink"):
        begin("httpdocs/link.txt")
    with pytest.raises(ValueError, match="Symlink"):
        server.write_vhost_file(DOMAIN, "httpdocs/linkdir/x.txt", "x", confirm=True)
    assert list(outside.iterdir()) == []


def test_upload_rate_limit(client, vhost, monkeypatch):
    monkeypatch.setattr(server, "_UPLOAD_RATE_LIMIT", 2)
    codes = [client.put("/upload/x" * 3, content=b"").status_code for _ in range(3)]
    assert codes == [401, 401, 429]


def test_write_vhost_file_uses_helper(vhost):
    out = server.write_vhost_file(DOMAIN, "httpdocs/neu/a.txt", "hallo", confirm=True)
    assert "Neu erstellt" in out and "Rechte: 0644" in out and "Neu angelegte Ordner" in out
    out = server.write_vhost_file(DOMAIN, "httpdocs/neu/a.txt", "zwei", confirm=True)
    assert "Überschrieben" in out and "Backup der alten Version" in out
    (vhost / "conf/root.conf").write_text("x")
    with pytest.raises(ValueError, match="root"):
        server.write_vhost_file(DOMAIN, "conf/root.conf", "y", confirm=True)


# --- Teil A: SSRF ------------------------------------------------------------


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254",
                                "100.64.0.1", "0.0.0.0", "::1", "fe80::1", "fc00::1", "fd00:ec2::254",
                                "::ffff:10.0.0.1", "::ffff:127.0.0.1", "224.0.0.1", "203.0.113.5"])
def test_ssrf_blocklist(ip):
    assert server._ip_is_public(ip) is False


def test_fetch_rejects_private_and_http(vhost, monkeypatch):
    monkeypatch.setattr(server, "_resolve_host", lambda h, p: ["10.0.0.5"])
    with pytest.raises(ValueError, match="SSRF"):
        server.fetch_to_vhost(DOMAIN, "httpdocs/f.bin", "https://files.example.com/f.bin", confirm=True)
    with pytest.raises(ValueError, match="https"):
        server.fetch_to_vhost(DOMAIN, "httpdocs/f.bin", "http://files.example.com/f.bin", confirm=True)
    assert not (vhost / "httpdocs/f.bin").exists()


def test_fetch_redirect_to_private_blocked(vhost, monkeypatch):
    import httpx

    dns = {"files.example.com": ["203.0.113.10"], "internal.example.com": ["10.0.0.5"]}
    monkeypatch.setattr(server, "_resolve_host", lambda h, p: dns[h])
    monkeypatch.setattr(server, "_ip_is_public", lambda ip: ip == "203.0.113.10")
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers["host"], request.extensions.get("sni_hostname")))
        return httpx.Response(302, headers={"location": "https://internal.example.com/x"})

    monkeypatch.setattr(server, "_fetch_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False))
    with pytest.raises(ValueError, match="SSRF"):
        server.fetch_to_vhost(DOMAIN, "httpdocs/f.bin", "https://files.example.com/f.bin", confirm=True)
    # Verbindung an die geprüfte IP gepinnt, Host/SNI = Originalname, zweiter Hop nie verbunden
    assert seen == [("203.0.113.10", "files.example.com", "files.example.com")]
    assert not (vhost / "httpdocs/f.bin").exists()


def test_fetch_ok_and_redirect_limit(vhost, monkeypatch):
    import httpx

    monkeypatch.setattr(server, "_resolve_host", lambda h, p: ["203.0.113.10"])
    monkeypatch.setattr(server, "_ip_is_public", lambda ip: True)
    calls = {"n": 0}

    def ok(request):
        return httpx.Response(200, content=b"daten")

    def loop(request):
        calls["n"] += 1
        return httpx.Response(302, headers={"location": f"/r{calls['n']}"})

    monkeypatch.setattr(server, "_fetch_client", lambda: httpx.Client(transport=httpx.MockTransport(ok)))
    res = json.loads(server.fetch_to_vhost(DOMAIN, "httpdocs/f.bin", "https://files.example.com/f.bin",
                                           sha256=hashlib.sha256(b"daten").hexdigest(), confirm=True))
    assert res["size"] == 5 and (vhost / "httpdocs/f.bin").read_bytes() == b"daten"
    monkeypatch.setattr(server, "_fetch_client", lambda: httpx.Client(transport=httpx.MockTransport(loop)))
    with pytest.raises(ValueError, match="Redirects"):
        server.fetch_to_vhost(DOMAIN, "httpdocs/g.bin", "https://files.example.com/g.bin", confirm=True)
    assert calls["n"] == 4


# --- Teil B: Dateiverwaltung -------------------------------------------------


@pytest.mark.parametrize("path", ["httpdocs", "sub.example.com", "httpdocs/cgi-bin", "cgi-bin", "conf",
                                  "conf/x.conf", "logs/access_log", "statistics", ".ssh", ".mcp-trash",
                                  ".mcp-trash/20260101120000", ".bashrc", "mail", "etc/passwd"])
def test_delete_protected_rejected(vhost, path):
    with pytest.raises(ValueError, match="geschützt"):
        server.delete_vhost_file(DOMAIN, path, recursive=True, confirm=True)


@pytest.mark.parametrize("path", ["httpdocs/*", "httpdocs/../conf", "/var/www", "httpdocs/a*.txt", "."])
def test_delete_wildcards_and_dots_rejected(vhost, path):
    with pytest.raises(ValueError):
        server.delete_vhost_file(DOMAIN, path, confirm=True)


def test_delete_file_to_trash_outside_docroot(vhost):
    res = json.loads(server.delete_vhost_file(DOMAIN, "httpdocs/old.txt", confirm=True))
    entry = vhost / ".mcp-trash" / res["trash_entry"]
    assert (entry / "httpdocs/old.txt").read_text() == "old"
    assert not (vhost / "httpdocs/old.txt").exists()
    trash = vhost / ".mcp-trash"
    assert not str(trash).startswith(str(vhost / "httpdocs"))
    assert oct(os.stat(trash).st_mode & 0o777) == "0o700"
    assert os.stat(trash).st_uid == pwd.getpwnam("nobody").pw_uid
    meta = json.loads((entry / ".mcp-trash-meta.json").read_text())
    assert meta["original_path"] == "httpdocs/old.txt"


def test_delete_dir_needs_recursive_and_token(vhost):
    (vhost / "httpdocs/img/a.jpg").write_text("a")
    with pytest.raises(ValueError, match="recursive"):
        server.delete_vhost_file(DOMAIN, "httpdocs/img", confirm=True)
    with pytest.raises(ValueError, match="zweistufig"):
        server.delete_vhost_file(DOMAIN, "httpdocs/img", recursive=True, confirm=True)
    with pytest.raises(ValueError, match="ungültig"):
        server.delete_vhost_file(DOMAIN, "httpdocs/img", recursive=True, delete_token="1.abc.def", confirm=True)
    dry = json.loads(server.delete_vhost_file(DOMAIN, "httpdocs/img", recursive=True, dry_run=True))
    assert dry["dateien"] == 1 and "httpdocs/img/a.jpg" in dry["erste_pfade"]
    assert (vhost / "httpdocs/img/a.jpg").exists()
    # Token für Papierkorb gilt nicht für permanent
    with pytest.raises(ValueError, match="ungültig"):
        server.delete_vhost_file(DOMAIN, "httpdocs/img", recursive=True, permanent=True,
                                 delete_token=dry["delete_token"], confirm=True)
    res = json.loads(server.delete_vhost_file(DOMAIN, "httpdocs/img", recursive=True,
                                              delete_token=dry["delete_token"], confirm=True))
    assert res["trash_entry"] and not (vhost / "httpdocs/img").exists()


def test_delete_token_invalid_after_change(vhost):
    (vhost / "httpdocs/img/a.jpg").write_text("a")
    dry = json.loads(server.delete_vhost_file(DOMAIN, "httpdocs/img", recursive=True, dry_run=True))
    (vhost / "httpdocs/img/b.jpg").write_text("b")
    with pytest.raises(ValueError, match="geändert"):
        server.delete_vhost_file(DOMAIN, "httpdocs/img", recursive=True,
                                 delete_token=dry["delete_token"], confirm=True)
    assert (vhost / "httpdocs/img/a.jpg").exists()


def test_delete_executable_needs_flag(vhost):
    (vhost / "httpdocs/img/x.php").write_text("<?php")
    dry = json.loads(server.delete_vhost_file(DOMAIN, "httpdocs/img", recursive=True, dry_run=True))
    assert dry["enthaelt_ausfuehrbare"] is True
    with pytest.raises(ValueError, match="allow_executable"):
        server.delete_vhost_file(DOMAIN, "httpdocs/img", recursive=True,
                                 delete_token=dry["delete_token"], confirm=True)
    server.delete_vhost_file(DOMAIN, "httpdocs/img", recursive=True, delete_token=dry["delete_token"],
                             allow_executable=True, confirm=True)


def test_delete_trash_limit(vhost, monkeypatch):
    monkeypatch.setattr(server, "_TRASH_MAX_BYTES", 1)
    with pytest.raises(ValueError, match="permanent"):
        server.delete_vhost_file(DOMAIN, "httpdocs/old.txt", confirm=True)
    res = json.loads(server.delete_vhost_file(DOMAIN, "httpdocs/old.txt", permanent=True, confirm=True))
    assert res["ergebnis"] == "Endgültig gelöscht." and not (vhost / "httpdocs/old.txt").exists()


def test_symlink_only_link_removed(vhost):
    target = vhost / "httpdocs/img"
    (target / "keep.jpg").write_text("k")
    os.symlink(target, vhost / "httpdocs/link")
    os.symlink(target / "keep.jpg", vhost / "httpdocs/filelink")
    server.delete_vhost_file(DOMAIN, "httpdocs/link", permanent=True, confirm=True)
    server.delete_vhost_file(DOMAIN, "httpdocs/filelink", confirm=True)
    assert not os.path.lexists(vhost / "httpdocs/link")
    assert not os.path.lexists(vhost / "httpdocs/filelink")
    assert (target / "keep.jpg").read_text() == "k"


def test_restore_does_not_overwrite(vhost):
    res = json.loads(server.delete_vhost_file(DOMAIN, "httpdocs/old.txt", confirm=True))
    (vhost / "httpdocs/old.txt").write_text("neu")
    with pytest.raises(ValueError, match="existiert"):
        server.restore_vhost_trash(DOMAIN, res["trash_entry"], confirm=True)
    assert (vhost / "httpdocs/old.txt").read_text() == "neu"
    os.unlink(vhost / "httpdocs/old.txt")
    preview = json.loads(server.restore_vhost_trash(DOMAIN, res["trash_entry"]))
    assert preview["ziel"] == "httpdocs/old.txt" and not (vhost / "httpdocs/old.txt").exists()
    server.restore_vhost_trash(DOMAIN, res["trash_entry"], confirm=True)
    assert (vhost / "httpdocs/old.txt").read_text() == "old"
    assert not (vhost / ".mcp-trash" / res["trash_entry"]).exists()


def test_empty_trash(vhost):
    res = json.loads(server.delete_vhost_file(DOMAIN, "httpdocs/old.txt", confirm=True))
    prev = json.loads(server.empty_vhost_trash(DOMAIN, older_than_days=0))
    assert [e["entry"] for e in prev["eintraege"]] == [res["trash_entry"]]
    assert (vhost / ".mcp-trash" / res["trash_entry"]).exists()
    json.loads(server.empty_vhost_trash(DOMAIN, older_than_days=14, confirm=True))
    assert (vhost / ".mcp-trash" / res["trash_entry"]).exists()
    server.empty_vhost_trash(DOMAIN, older_than_days=0, confirm=True)
    assert not (vhost / ".mcp-trash" / res["trash_entry"]).exists()


def test_move_across_domains_and_docroots_rejected(vhost):
    with pytest.raises(ValueError):
        server.move_vhost_file(DOMAIN, "httpdocs/old.txt", "../other.example.com/httpdocs/old.txt", confirm=True)
    with pytest.raises(ValueError, match="Docroot"):
        server.move_vhost_file(DOMAIN, "httpdocs/old.txt", "sub.example.com/old.txt", confirm=True)
    with pytest.raises(ValueError, match="geschützt"):
        server.move_vhost_file(DOMAIN, "httpdocs/old.txt", "conf/old.txt", confirm=True)
    with pytest.raises(ValueError, match="geschützt"):
        server.move_vhost_file(DOMAIN, "httpdocs", "httpdocs/x", confirm=True)
    assert (vhost / "httpdocs/old.txt").exists()


def test_move_overwrite_rules(vhost):
    (vhost / "httpdocs/b.txt").write_text("b")
    with pytest.raises(ValueError, match="existiert"):
        server.move_vhost_file(DOMAIN, "httpdocs/old.txt", "httpdocs/b.txt", confirm=True)
    assert (vhost / "httpdocs/b.txt").read_text() == "b"
    res = json.loads(server.move_vhost_file(DOMAIN, "httpdocs/old.txt", "httpdocs/b.txt",
                                            overwrite=True, confirm=True))
    assert (vhost / "httpdocs/b.txt").read_text() == "old"
    assert (vhost / res["backup_ueberschriebenes_ziel"]).read_text() == "b"
    with pytest.raises(ValueError, match="Nicht gefunden"):
        server.move_vhost_file(DOMAIN, "httpdocs/b.txt", "httpdocs/neu/b.txt", confirm=True)
    server.move_vhost_file(DOMAIN, "httpdocs/b.txt", "httpdocs/neu/b.txt", create_parents=True, confirm=True)
    assert (vhost / "httpdocs/neu/b.txt").read_text() == "old"


def test_move_executable_and_symlink(vhost):
    with pytest.raises(ValueError, match="allow_executable"):
        server.move_vhost_file(DOMAIN, "httpdocs/old.txt", "httpdocs/old.php", confirm=True)
    os.symlink("/etc/hostname", vhost / "httpdocs/l")
    server.move_vhost_file(DOMAIN, "httpdocs/l", "httpdocs/l2", confirm=True)
    assert os.readlink(vhost / "httpdocs/l2") == "/etc/hostname"


def test_list_vhost_dir(vhost):
    res = json.loads(server.list_vhost_dir(DOMAIN, "httpdocs", depth=2))
    paths = {e["path"]: e for e in res["entries"]}
    assert paths["httpdocs/old.txt"]["type"] == "file" and paths["httpdocs/img"]["type"] == "dir"
    for i in range(510):
        (vhost / "httpdocs/img" / f"f{i:03d}").write_text("x")
    res = json.loads(server.list_vhost_dir(DOMAIN, "httpdocs", depth=3))
    assert len(res["entries"]) == 500 and "hinweis" in res
