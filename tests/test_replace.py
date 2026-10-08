"""Tests für replace_in_vhost_file/replace_in_vhost_files (ohne Plesk-Server).

Wie test_vhost_files.py: Der Datei-Helper läuft lokal als Subprozess gegen ein
temporäres Vhost-Verzeichnis. Benötigt Linux und root (chown).
"""

import json
import os
import pwd
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402
from test_vhost_files import DOMAIN, LocalSession  # noqa: E402,F401

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or os.geteuid() != 0,
    reason="benötigt Linux und root (chown im Helper)",
)


@pytest.fixture
def vhost(tmp_path, monkeypatch):
    user = pwd.getpwnam("nobody")
    root = tmp_path / "vhosts"
    base = root / DOMAIN
    for d in ("httpdocs", "httpdocs/inc", "conf"):
        (base / d).mkdir(parents=True)
    for d in (base, base / "httpdocs", base / "httpdocs/inc"):
        os.chown(d, user.pw_uid, user.pw_gid)
    files = {
        "httpdocs/config.php": "<?php\ndefine('WP_DEBUG', false);\n$host = 'db.example.com';\n",
        "httpdocs/multi.txt": "foo bar\nfoo baz\nfoo\n",
        "httpdocs/inc/a.php": "<?php echo 'alt'; ?>\n",
        "httpdocs/inc/b.php": "<?php echo 'alt'; ?>\n",
        "httpdocs/inc/c.php": "<?php echo 'anders'; ?>\n",
    }
    for rel, content in files.items():
        (base / rel).write_text(content)
        os.chown(base / rel, user.pw_uid, user.pw_gid)
    os.chmod(base / "httpdocs/config.php", 0o600)
    monkeypatch.setattr(server, "_helper_session_factory", LocalSession)
    monkeypatch.setattr(server, "_VHOST_BASE", str(root))
    monkeypatch.setattr(server, "_UPLOAD_MIN_FREE", 0)
    return base


def rep(path, old, new, **kw):
    return json.loads(server.replace_in_vhost_file(DOMAIN, path, old, new, confirm=True, **kw))


def baks(d, name):
    return sorted(p for p in d.iterdir() if p.name.startswith(name + ".bak-"))


def tmp_leftovers(d):
    return [p.name for p in d.rglob("*") if ".mcp-" in p.name]


def test_exact_match(vhost):
    res = rep("httpdocs/config.php", "'WP_DEBUG', false", "'WP_DEBUG', true")
    text = (vhost / "httpdocs/config.php").read_text()
    assert "define('WP_DEBUG', true);" in text and "false" not in text
    assert res["ersetzungen"] == 1 and res["groesse_neu"] == len(text.encode())
    assert res["kontext"] == [{"vorher": "define('WP_DEBUG', false);", "nachher": "define('WP_DEBUG', true);"}]


def test_backup_created(vhost):
    old = (vhost / "httpdocs/config.php").read_text()
    res = rep("httpdocs/config.php", "db.example.com", "db2.example.com")
    b = baks(vhost / "httpdocs", "config.php")
    assert len(b) == 1 and b[0].read_text() == old
    assert server._BACKUP_SUFFIX_RE.search(b[0].name)
    assert res["backup"] == str(b[0])
    assert tmp_leftovers(vhost) == []
    # Zweite Ersetzung in derselben Sekunde: eigenes Backup des Zwischenstands
    mid = (vhost / "httpdocs/config.php").read_text()
    rep("httpdocs/config.php", "db2.example.com", "db3.example.com")
    b = baks(vhost / "httpdocs", "config.php")
    assert len(b) == 2 and b[0].read_text() == old and b[1].read_text() == mid


def test_permissions_unchanged(vhost):
    f = vhost / "httpdocs/config.php"
    before = os.stat(f)
    res = rep("httpdocs/config.php", "false", "true")
    after = os.stat(f)
    assert (after.st_uid, after.st_gid, after.st_mode) == (before.st_uid, before.st_gid, before.st_mode)
    assert res["rechte"] == "0600"
    bst = os.stat(baks(vhost / "httpdocs", "config.php")[0])
    assert (bst.st_uid, bst.st_gid, bst.st_mode) == (before.st_uid, before.st_gid, before.st_mode)


def test_no_match(vhost):
    with pytest.raises(ValueError, match="nicht vor"):
        rep("httpdocs/config.php", "gibt es nicht", "x")
    assert baks(vhost / "httpdocs", "config.php") == []


def test_multiple_matches_without_replace_all(vhost):
    with pytest.raises(ValueError, match="3-mal"):
        rep("httpdocs/multi.txt", "foo", "qux")
    assert (vhost / "httpdocs/multi.txt").read_text() == "foo bar\nfoo baz\nfoo\n"
    assert baks(vhost / "httpdocs", "multi.txt") == []


def test_replace_all_and_delete(vhost):
    res = rep("httpdocs/multi.txt", "foo", "qux", replace_all=True)
    assert res["ersetzungen"] == 3 and len(res["kontext"]) == 3
    assert (vhost / "httpdocs/multi.txt").read_text() == "qux bar\nqux baz\nqux\n"
    assert res["kontext"][1] == {"vorher": "foo baz", "nachher": "qux baz"}
    rep("httpdocs/multi.txt", " bar", "")
    assert (vhost / "httpdocs/multi.txt").read_text() == "qux\nqux baz\nqux\n"


def test_crlf_preserved(vhost):
    f = vhost / "httpdocs/crlf.txt"
    f.write_bytes(b"a\r\nb\r\nc\r\n")
    res = rep("httpdocs/crlf.txt", "a\nb", "x\ny")
    assert f.read_bytes() == b"x\r\ny\r\nc\r\n" and res["zeilenenden"]


@pytest.mark.parametrize("path", ["../other.example.com/httpdocs/x", "/etc/passwd", "httpdocs/../../x",
                                  "httpdocs/./config.php", "httpdocs/*.php", "", "."])
def test_path_traversal_rejected(vhost, path):
    with pytest.raises(ValueError):
        rep(path, "a", "b")


def test_symlink_rejected(vhost, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("geheim")
    os.symlink(outside / "f.txt", vhost / "httpdocs/link.txt")
    os.symlink(outside, vhost / "httpdocs/linkdir")
    with pytest.raises(ValueError, match="Symlink"):
        rep("httpdocs/link.txt", "geheim", "x")
    with pytest.raises(ValueError, match="Symlink"):
        rep("httpdocs/linkdir/f.txt", "geheim", "x")
    assert (outside / "f.txt").read_text() == "geheim"
    assert list(outside.iterdir()) == [outside / "f.txt"]


def test_binary_and_non_utf8_rejected(vhost):
    (vhost / "httpdocs/bild.bin").write_bytes(b"abc\x00def")
    (vhost / "httpdocs/latin1.txt").write_bytes("abc äöü".encode("latin-1"))
    with pytest.raises(ValueError, match="Nullbytes"):
        rep("httpdocs/bild.bin", "abc", "x")
    with pytest.raises(ValueError, match="UTF-8"):
        rep("httpdocs/latin1.txt", "abc", "x")
    assert (vhost / "httpdocs/bild.bin").read_bytes() == b"abc\x00def"


def test_size_limit(vhost, monkeypatch):
    monkeypatch.setattr(server, "_REPLACE_MAX_BYTES", 10)
    with pytest.raises(ValueError, match="Limit"):
        rep("httpdocs/config.php", "false", "true")


def test_root_owned_outside_httpdocs_rejected(vhost):
    (vhost / "conf/x.conf").write_text("a")
    with pytest.raises(ValueError, match="root"):
        rep("conf/x.conf", "a", "b")
    assert (vhost / "conf/x.conf").read_text() == "a"


def test_requires_confirm_and_old_str(vhost):
    with pytest.raises(ValueError, match="confirm"):
        server.replace_in_vhost_file(DOMAIN, "httpdocs/config.php", "false", "true")
    with pytest.raises(ValueError, match="leer"):
        rep("httpdocs/config.php", "", "x")


def test_multi_paths_and_skipped(vhost):
    res = json.loads(server.replace_in_vhost_files(
        DOMAIN, "'alt'", "'neu'", paths=["httpdocs/inc/a.php", "httpdocs/inc/b.php", "httpdocs/inc/c.php"],
        confirm=True))
    assert [f["datei"].rsplit("/", 1)[1] for f in res["geaendert"]] == ["a.php", "b.php"]
    assert res["ohne_treffer"] == ["httpdocs/inc/c.php"] and res["ersetzungen_gesamt"] == 2
    assert (vhost / "httpdocs/inc/a.php").read_text() == "<?php echo 'neu'; ?>\n"
    assert len(baks(vhost / "httpdocs/inc", "a.php")) == 1


def test_multi_glob(vhost):
    os.symlink(vhost / "httpdocs/inc/a.php", vhost / "httpdocs/inc/l.php")
    res = json.loads(server.replace_in_vhost_files(DOMAIN, "'alt'", "'neu'", glob="httpdocs/**/*.php",
                                                   confirm=True))
    assert res["ersetzungen_gesamt"] == 2
    assert sorted(res["ohne_treffer"]) == ["httpdocs/config.php", "httpdocs/inc/c.php"]
    assert res["symlinks_uebersprungen"] == ["httpdocs/inc/l.php"]
    # Backups werden von einem weiteren glob nicht erfasst
    res = json.loads(server.replace_in_vhost_files(DOMAIN, "'neu'", "'neuer'", glob="httpdocs/inc/*",
                                                   confirm=True))
    assert res["ersetzungen_gesamt"] == 2 and all(".bak-" not in f["datei"] for f in res["geaendert"])


def test_multi_glob_limits(vhost, monkeypatch):
    monkeypatch.setattr(server, "_REPLACE_MAX_FILES", 2)
    with pytest.raises(ValueError, match="mehr als 2"):
        server.replace_in_vhost_files(DOMAIN, "alt", "neu", glob="httpdocs/**/*", confirm=True)
    for g in ("conf/*", "/httpdocs/*", "httpdocs/../conf/*", "*"):
        with pytest.raises(ValueError, match="glob"):
            server.replace_in_vhost_files(DOMAIN, "alt", "neu", glob=g, confirm=True)
    with pytest.raises(ValueError, match="Entweder"):
        server.replace_in_vhost_files(DOMAIN, "alt", "neu", confirm=True)


def test_multi_all_or_nothing(vhost):
    (vhost / "httpdocs/inc/bin.php").write_bytes(b"'alt'\x00")
    with pytest.raises(ValueError, match="Nullbytes"):
        server.replace_in_vhost_files(DOMAIN, "'alt'", "'neu'",
                                      paths=["httpdocs/inc/a.php", "httpdocs/inc/bin.php"], confirm=True)
    with pytest.raises(ValueError, match="2-mal"):
        (vhost / "httpdocs/inc/b.php").write_text("'alt' 'alt'")
        server.replace_in_vhost_files(DOMAIN, "'alt'", "'neu'",
                                      paths=["httpdocs/inc/a.php", "httpdocs/inc/b.php"], confirm=True)
    assert (vhost / "httpdocs/inc/a.php").read_text() == "<?php echo 'alt'; ?>\n"
    assert baks(vhost / "httpdocs/inc", "a.php") == [] and tmp_leftovers(vhost) == []


def test_multi_rollback_on_write_error(vhost):
    # Alle Backup-Namen von b.php belegt -> Fehler in Phase 2, a.php bleibt unverändert
    import time
    for i in range(12):
        s = time.strftime("%Y%m%d%H%M%S", time.localtime(time.time() + i))
        (vhost / f"httpdocs/inc/b.php.bak-{s}").write_text("x")
    with pytest.raises(ValueError, match="existiert"):
        server.replace_in_vhost_files(DOMAIN, "'alt'", "'neu'",
                                      paths=["httpdocs/inc/a.php", "httpdocs/inc/b.php"], confirm=True)
    assert (vhost / "httpdocs/inc/a.php").read_text() == "<?php echo 'alt'; ?>\n"
    assert baks(vhost / "httpdocs/inc", "a.php") == [] and tmp_leftovers(vhost) == []
