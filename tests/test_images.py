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
    assert not (vhost / ".plesk-mcp").exists()


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


def test_max_files_limit(vhost):
    for i in range(3):
        make_jpeg(vhost / f"httpdocs/uploads/{i}.jpg")
    res = opt(max_files=2)
    assert res["bilder_geprueft"] == 2 and res["uebersprungen"]["limit_max_files"] == 1 and "hinweis" in res


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
