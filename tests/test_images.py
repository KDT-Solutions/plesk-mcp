"""Tests für optimize_images/image_backups (ohne Plesk-Server).

Der Worker (_IMG_WORKER_SRC) läuft lokal als Subprozess gegen ein temporäres
Vhost-Verzeichnis. jpegoptim/optipng werden durch kleine verlustfreie
Ersatzskripte ersetzt (Bildinhalt bleibt, Kommentare werden entfernt).
Benötigt Pillow für Testbilder und Pixelvergleich, sonst übersprungen.
"""

import io
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

PIL = pytest.importorskip("PIL.Image")
from PIL import Image, ImageCms  # noqa: E402

DOMAIN = "example.com"

FAKE_JPEGOPTIM = r'''#!{py}
import struct, sys
p = sys.argv[-1]
d = open(p, "rb").read()
out, i = [d[:2]], 2
while i < len(d):
    m = d[i + 1]
    if m in (0xDA, 0xD9):
        out.append(d[i:]); break
    n = struct.unpack(">H", d[i + 2:i + 4])[0]
    if m != 0xFE:
        out.append(d[i:i + 2 + n])
    i += 2 + n
open(p, "wb").write(b"".join(out))
'''

FAKE_OPTIPNG = r'''#!{py}
import struct, sys, zlib
p = sys.argv[-1]
d = open(p, "rb").read()
out, i = [d[:8]], 8
while i < len(d):
    n = struct.unpack(">I", d[i:i + 4])[0]
    if d[i + 4:i + 8] != b"tEXt":
        out.append(d[i:i + 12 + n])
    i += 12 + n
open(p, "wb").write(b"".join(out))
'''


def make_jpeg(path, gps=True):
    img = Image.new("RGB", (64, 48), (200, 30, 30))
    for x in range(64):
        img.putpixel((x, x % 48), (x * 4, 255 - x * 4, 7))
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation
    exif[0x010F] = "Kamera AG"
    if gps:
        exif.get_ifd(0x8825).update({1: "N", 2: (47.0, 33.0, 12.5), 3: "E", 4: (7.0, 35.0, 1.0)})
    exif.get_ifd(0x8769).update({0xA431: "SN-123456", 0xA430: "Max Muster"})
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90, exif=exif.tobytes(), icc_profile=icc, comment=b"x" * 2000)
    data = buf.getvalue()
    xmp = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description exif:GPSLatitude="47,33.2N" '
           b'aux:SerialNumber="999" dc:creator="Fotograf"><exif:GPSLongitude>7,35E</exif:GPSLongitude>'
           b'</rdf:Description></rdf:RDF></x:xmpmeta>')
    seg = b"http://ns.adobe.com/xap/1.0/\x00" + xmp
    data = data[:2] + b"\xff\xe1" + (len(seg) + 2).to_bytes(2, "big") + seg + data[2:]
    path.write_bytes(data)


def make_png(path):
    img = Image.new("RGBA", (40, 40), (10, 20, 30, 128))
    from PIL import PngImagePlugin
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "y" * 3000)
    img.save(path, "PNG", pnginfo=info)


@pytest.fixture
def vhost(tmp_path, monkeypatch):
    base = tmp_path / "vhosts" / DOMAIN
    up = base / "httpdocs" / "uploads"
    up.mkdir(parents=True)
    (base / "conf").mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, src in (("jpegoptim", FAKE_JPEGOPTIM), ("optipng", FAKE_OPTIPNG)):
        f = bindir / name
        f.write_text(src.replace("{py}", sys.executable))
        f.chmod(0o755)
    ctx = {"domain": DOMAIN, "base": str(base), "login": "nobody", "uid": 0, "gid": 0,
           "docroots": [["httpdocs"], ["sub.example.com"]]}
    monkeypatch.setattr(server, "_vhost_ctx", lambda d: dict(ctx))
    monkeypatch.setattr(server, "_IMG_JPEGOPTIM", str(bindir / "jpegoptim"))
    monkeypatch.setattr(server, "_IMG_OPTIPNG", str(bindir / "optipng"))

    def run_local(c, args):
        p = subprocess.run([sys.executable, "-I", "-c", server._IMG_WORKER_SRC],
                           input=json.dumps({**args, "base": c["base"]}).encode(), capture_output=True)
        last = p.stdout.decode().strip().splitlines()[-1]
        res = json.loads(last)
        if not res["ok"]:
            raise ValueError(f"Bildoptimierung abgebrochen: {res.get('error')}")
        return res

    monkeypatch.setattr(server, "_img_run", run_local)
    return base


def opt(**kw):
    return json.loads(server.optimize_images(DOMAIN, **kw))


def pixels(path):
    with Image.open(path) as im:
        return im.convert("RGBA").tobytes()


def test_dry_run_changes_nothing(vhost):
    f = vhost / "httpdocs/uploads/a.jpg"
    make_jpeg(f)
    before = f.read_bytes()
    res = opt(path="httpdocs/uploads")
    assert res["modus"].startswith("Probelauf") and res["bilder_optimiert"] == 1 and res["ersparnis_bytes"] > 0
    assert f.read_bytes() == before and "backup_run_id" not in res
    assert not (vhost / ".plesk-mcp/image-backups").exists()
    assert not any((vhost / ".plesk-mcp/tmp").iterdir())


def test_requires_confirm(vhost):
    with pytest.raises(ValueError, match="confirm"):
        opt(dry_run=False)


def test_jpeg_lossless_privacy_removed_metadata_kept(vhost):
    f = vhost / "httpdocs/uploads/a.jpg"
    make_jpeg(f)
    px = pixels(f)
    old_size = f.stat().st_size
    os.utime(f, (1_600_000_000, 1_600_000_000))
    res = opt(path="httpdocs/uploads", dry_run=False, confirm=True)
    assert res["bilder_optimiert"] == 1 and res["ersparnis_bytes"] > 0
    assert set(res["groesste_ersparnisse"][0]["entfernt"]) == {"gps", "seriennummern", "xmp"}
    assert f.stat().st_size < old_size and f.stat().st_mtime == 1_600_000_000
    assert pixels(f) == px
    with Image.open(f) as im:
        exif = im.getexif()
        assert exif[0x0112] == 6 and exif[0x010F] == "Kamera AG"
        assert not dict(exif.get_ifd(0x8825))
        assert all(not str(v).strip("\x00") for k, v in exif.get_ifd(0x8769).items() if k in (0xA430, 0xA431))
        assert im.info.get("icc_profile")
    data = f.read_bytes()
    assert b"SN-123456" not in data and b"Max Muster" not in data
    assert b"GPSLatitude" not in data and b"GPSLongitude" not in data and b"SerialNumber" not in data
    assert b'dc:creator="Fotograf"' in data
    # Backup = Original (Hardlink), ausserhalb des Docroots
    backup = vhost / ".plesk-mcp/image-backups" / res["backup_run_id"] / "httpdocs/uploads/a.jpg"
    assert backup.stat().st_size == old_size and b"SN-123456" in backup.read_bytes()


def test_png_lossless(vhost):
    f = vhost / "httpdocs/uploads/b.png"
    make_png(f)
    px = pixels(f)
    res = opt(dry_run=False, confirm=True)
    assert res["bilder_optimiert"] == 1 and pixels(f) == px
    assert f.stat().st_size < 3000


def test_not_smaller_and_no_privacy_is_skipped(vhost):
    f = vhost / "httpdocs/uploads/c.png"
    Image.new("RGB", (8, 8)).save(f, "PNG")
    before = f.read_bytes()
    res = opt(dry_run=False, confirm=True)
    assert res["bilder_optimiert"] == 0 and res["uebersprungen"].get("nicht_kleiner") == 1
    assert f.read_bytes() == before


def test_symlinks_hardlinks_and_paths(vhost, tmp_path):
    outside = tmp_path / "outside.jpg"
    make_jpeg(outside)
    os.symlink(outside, vhost / "httpdocs/uploads/link.jpg")
    os.symlink(tmp_path, vhost / "httpdocs/uploads/linkdir")
    make_jpeg(vhost / "httpdocs/uploads/h.jpg")
    os.link(vhost / "httpdocs/uploads/h.jpg", vhost / "httpdocs/uploads/h2.jpg")
    before = outside.read_bytes()
    res = opt(dry_run=False, confirm=True)
    assert res["bilder_optimiert"] == 0
    assert res["uebersprungen"].get("hardlink") == 2 and res["uebersprungen"].get("symlink_oder_kein_file") == 1
    assert outside.read_bytes() == before
    for p in ("conf", "../x", "/etc", "httpdocs/../conf", ".plesk-mcp"):
        with pytest.raises(ValueError):
            opt(path=p)


def test_max_files_limit_and_continue(vhost):
    for i in range(3):
        make_jpeg(vhost / f"httpdocs/uploads/{i}.jpg")
    res = opt(max_files=2, dry_run=False, confirm=True)
    assert res["bilder_geprueft"] == 2 and res["uebersprungen"]["limit_max_files"] == 1
    assert res["unvollstaendig"] is True and res["weiter_ab"] == "httpdocs/uploads/1.jpg"
    assert "weiter_ab=" in res["hinweis"]
    run1 = res["backup_run_id"]
    res = opt(max_files=2, dry_run=False, confirm=True, weiter_ab=res["weiter_ab"])
    assert res["bilder_geprueft"] == 1 and res["groesste_ersparnisse"][0]["datei"] == "httpdocs/uploads/2.jpg"
    assert res["unvollstaendig"] is False and "weiter_ab" not in res and res["backup_run_id"] != run1


def test_continuation_covers_every_image_exactly_once(vhost):
    names = [f"httpdocs/uploads/{d}/{i}.jpg" for d in ("a", "b") for i in range(4)]
    for n in names:
        (vhost / n).parent.mkdir(parents=True, exist_ok=True)
        make_jpeg(vhost / n)
    seen, weiter, calls = [], "", 0
    while True:
        res = opt(max_files=3, weiter_ab=weiter)
        seen += sorted(e["datei"] for e in res["groesste_ersparnisse"])
        calls += 1
        if not res["unvollstaendig"]:
            break
        weiter = res["weiter_ab"]
    assert calls == 3 and sorted(seen) == sorted(names) and len(seen) == len(set(seen))


def test_time_budget(vhost, monkeypatch):
    for i in range(3):
        make_jpeg(vhost / f"httpdocs/uploads/{i}.jpg")
    monkeypatch.setattr(server, "_IMG_TIME_BUDGET", -1)
    res = opt()
    assert res["bilder_geprueft"] == 0 and res["uebersprungen"]["zeitbudget_rest"] == 3 and "Unterordner" in res["hinweis"]
    assert res["unvollstaendig"] is True


def test_tool_timeout_counts_as_skipped(vhost, monkeypatch, tmp_path):
    slow = tmp_path / "slow"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(0o755)
    monkeypatch.setattr(server, "_IMG_JPEGOPTIM", str(slow))
    monkeypatch.setattr(server, "_IMG_TOOL_TIMEOUT", 1)
    f = vhost / "httpdocs/uploads/a.jpg"
    make_jpeg(f)
    before = f.read_bytes()
    res = opt(dry_run=False, confirm=True)
    assert res["uebersprungen"]["timeout"] == 1 and f.read_bytes() == before
    assert [p.name for p in f.parent.iterdir()] == ["a.jpg"]


def test_empty_path_uses_main_docroot(vhost, monkeypatch):
    make_jpeg(vhost / "httpdocs/uploads/a.jpg")
    (vhost / "sub.example.com").mkdir()
    make_jpeg(vhost / "sub.example.com/b.jpg")
    res = opt()
    assert res["pfad"] == "httpdocs" and res["bilder_geprueft"] == 1
    ctx = server._vhost_ctx(DOMAIN)
    monkeypatch.setattr(server, "_vhost_ctx", lambda d: {**ctx, "main_docroot": ["sub.example.com"]})
    res = opt()
    assert res["pfad"] == "sub.example.com" and res["bilder_geprueft"] == 1


def test_invalid_path_lists_all_docroots(vhost):
    with pytest.raises(ValueError, match="Gültige Docroots: httpdocs, sub.example.com"):
        opt(path="conf")


def test_errors_reach_client_and_lock_text(vhost):
    import asyncio
    from mcp.server.mcpserver.exceptions import ToolError
    with pytest.raises(ToolError, match="liegt in keinem Docroot"):
        asyncio.run(server.mcp.call_tool("optimize_images", {"domain": DOMAIN, "path": "conf"}))
    lock = server._phps_lock(f"img:{DOMAIN}", DOMAIN, "eine Bildoptimierung")
    try:
        with pytest.raises(ToolError, match="Für example.com läuft bereits eine Bildoptimierung - bitte warten"):
            asyncio.run(server.mcp.call_tool("optimize_images", {"domain": DOMAIN}))
        with pytest.raises(ToolError, match="läuft bereits eine Bildoptimierung"):
            asyncio.run(server.mcp.call_tool("image_backups", {"domain": DOMAIN, "action": "delete",
                                                                "run_id": "20260101120000-1a2b3c4d"}))
    finally:
        lock.release()


def test_backups_list_restore_delete(vhost):
    f = vhost / "httpdocs/uploads/a.jpg"
    make_jpeg(f)
    original = f.read_bytes()
    run_id = opt(dry_run=False, confirm=True)["backup_run_id"]
    lst = json.loads(server.image_backups(DOMAIN))
    assert lst["backups"][0]["run_id"] == run_id and lst["backups"][0]["dateien"] == 1
    prev = json.loads(server.image_backups(DOMAIN, "restore", run_id))
    assert prev["modus"].startswith("Vorschau") and f.read_bytes() != original
    json.loads(server.image_backups(DOMAIN, "restore", run_id, confirm=True))
    assert f.read_bytes() == original
    assert not (vhost / ".plesk-mcp/image-backups" / run_id).exists()
    run2 = opt(dry_run=False, confirm=True)["backup_run_id"]
    json.loads(server.image_backups(DOMAIN, "delete", run2, confirm=True))
    assert not (vhost / ".plesk-mcp/image-backups" / run2).exists() and f.read_bytes() != original
    for bad in ("../../etc", "x", ""):
        with pytest.raises(ValueError):
            server.image_backups(DOMAIN, "delete", bad, confirm=True)


def test_broken_metadata_still_safe(vhost):
    f = vhost / "httpdocs/uploads/bad.jpg"
    make_jpeg(f, gps=False)
    data = bytearray(f.read_bytes())
    i = data.find(b"Exif\x00\x00")
    data[i + 6:i + 8] = b"XX"  # ungültige Byte-Reihenfolge
    f.write_bytes(bytes(data))
    px = pixels(f)
    res = opt(dry_run=False, confirm=True)
    assert pixels(f) == px
    entfernt = res["groesste_ersparnisse"][0]["entfernt"] if res["groesste_ersparnisse"] else []
    assert "metadaten_nicht_lesbar" in entfernt or res["bilder_optimiert"] == 0


def test_xmp_scrub_unit():
    ns = {}
    exec(compile(server._IMG_WORKER_SRC.replace("\nmain()\n", "\n"), "w", "exec"), ns)
    new, changed = ns["scrub_xmp"](b'<a exif:GPSAltitude="5" b="1"/><exif:GPSTimeStamp>x</exif:GPSTimeStamp>'
                                   b'<exifEX:BodySerialNumber/>')
    assert changed and new == b'<a b="1"/>'


def test_vhost_ctx_main_docroot(monkeypatch):
    class C:
        def close(self):
            pass

    def exec_(client, cmd, timeout=None, stdin_data=None):
        if "SELECT d.id, d.webspace_id" in cmd:
            return f"42\t0\tkunde\t{server._VHOST_BASE}/example.com\n", "", 0
        if "SELECT d.id, h.www_root" in cmd:
            return (f"77\t{server._VHOST_BASE}/example.com/sub.example.com\n"
                    f"42\t{server._VHOST_BASE}/example.com/example.com\n"), "", 0
        if cmd.startswith("id -u"):
            return "1001\n1002\n", "", 0
        raise AssertionError(cmd)

    monkeypatch.setattr(server, "_ssh_connect", lambda: C())
    monkeypatch.setattr(server, "_client_exec", exec_)
    ctx = server._vhost_ctx("example.com")
    assert ctx["main_docroot"] == ["example.com"]
    assert ctx["docroots"] == [["sub.example.com"], ["example.com"]]


def test_work_copies_outside_webroot_and_cleaned(vhost, monkeypatch, tmp_path):
    log = tmp_path / "args.log"
    spy = tmp_path / "spy"
    spy.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n")
    spy.chmod(0o755)
    monkeypatch.setattr(server, "_IMG_JPEGOPTIM", str(spy))
    make_jpeg(vhost / "httpdocs/uploads/a.jpg")
    res = opt(dry_run=False, confirm=True)
    arg = log.read_text().split()[-1]
    assert arg.startswith(str(vhost / ".plesk-mcp/tmp/")) and "/httpdocs/" not in arg
    assert res["bilder_optimiert"] == 1  # Datenschutzdaten entfernt
    assert not any((vhost / ".plesk-mcp/tmp").iterdir())
    assert [p.name for p in (vhost / "httpdocs/uploads").iterdir()] == ["a.jpg"]


def test_stale_work_dirs_removed(vhost):
    old = vhost / ".plesk-mcp/tmp/20200101000000-deadbeef"
    old.mkdir(parents=True)
    (old / "x.jpg").write_text("x")
    os.utime(old, (0, 0))
    make_jpeg(vhost / "httpdocs/uploads/a.jpg")
    opt()
    assert not old.exists()


def test_tool_error_message_has_exit_code(vhost, monkeypatch, tmp_path):
    bad = tmp_path / "bad"
    bad.write_text("#!/bin/sh\nexit 3\n")
    bad.chmod(0o755)
    monkeypatch.setattr(server, "_IMG_JPEGOPTIM", str(bad))
    make_jpeg(vhost / "httpdocs/uploads/a.jpg")
    res = opt()
    assert res["fehler"][0]["fehler"] == "bad Exit-Code 3: keine Ausgabe"
    bad.write_text("#!/bin/sh\necho 'Not a JPEG file' >&2\nexit 2\n")
    res = opt()
    assert res["fehler"][0]["fehler"] == "bad Exit-Code 2: Not a JPEG file"
