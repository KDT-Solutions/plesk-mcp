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


def _tool(tmp_path, name, body):
    f = tmp_path / name
    f.write_text("#!/bin/sh\n" + body)
    f.chmod(0o755)
    return str(f)


def _inject(monkeypatch, code):
    """Code im Worker vor main() ausführen (z.B. os-Funktionen ersetzen)."""
    src = server._IMG_WORKER_SRC
    assert src.endswith("\nmain()\n")
    monkeypatch.setattr(server, "_IMG_WORKER_SRC", src[:-len("main()\n")] + code + "\nmain()\n")


def _worker_ns():
    ns = {}
    exec(compile(server._IMG_WORKER_SRC.replace("\nmain()\n", "\n"), "w", "exec"), ns)
    return ns


def _snapshot(root):
    """Alle Einträge unter root mit Typ, Grösse und mtime (inkl. Ordner)."""
    out = {}
    for r, ds, fs in os.walk(root):
        for n in ds + fs:
            st = os.lstat(os.path.join(r, n))
            out[os.path.relpath(os.path.join(r, n), root)] = (st.st_mode, st.st_size, st.st_mtime_ns)
    st = os.lstat(root)
    out["."] = (st.st_mode, st.st_size, st.st_mtime_ns)
    return out


# --- Punkt 1: Werkzeug lehnt Datei ab / leere Fehlermeldung ---------------

def test_tool_rejection_is_skipped_with_message(vhost, monkeypatch, tmp_path):
    # 0.18: optipng -quiet / jpegoptim -q schreiben bei Ablehnung nichts -> "werkzeug: "
    monkeypatch.setattr(server, "_IMG_JPEGOPTIM", _tool(tmp_path, "jpegoptim", "exit 1\n"))
    make_jpeg(vhost / "httpdocs/uploads/a.jpg")
    res = opt()
    assert "fehler" not in res and res["uebersprungen"]["werkzeug_abgelehnt"] == 1
    assert res["uebersprungen_details"] == [{"datei": "httpdocs/uploads/a.jpg", "grund": "werkzeug_abgelehnt",
                                             "meldung": "jpegoptim Exit-Code 1: keine Ausgabe"}]
    monkeypatch.setattr(server, "_IMG_JPEGOPTIM", _tool(
        tmp_path, "jpegoptim", "echo '** Processing: x' >&2\necho 'Error: IHDR: CRC error' >&2\nexit 2\n"))
    res = opt()
    assert res["uebersprungen_details"][0]["meldung"] == "jpegoptim Exit-Code 2: Error: IHDR: CRC error"


def test_tools_called_without_quiet(vhost, monkeypatch, tmp_path):
    log = tmp_path / "args.log"
    for name in ("jpegoptim", "optipng"):
        monkeypatch.setattr(server, "_IMG_" + name.upper(), _tool(tmp_path, name, f'echo "$@" >> {log}\n'))
    make_jpeg(vhost / "httpdocs/uploads/a.jpg")
    make_png(vhost / "httpdocs/uploads/b.png")
    opt()
    args = log.read_text().split()
    assert "-q" not in args and "-quiet" not in args and "-p" in args and "-preserve" in args


def test_wrong_extension_is_skipped_not_error(vhost):
    up = vhost / "httpdocs/uploads"
    make_png(up / "logo.png")
    os.rename(up / "logo.png", up / "png-als.jpg")
    make_jpeg(up / "jpeg-als.png")
    (up / "icon.png").write_bytes(b"\x00\x00\x01\x00\x06\x00" + b"\x00" * 60)  # ICO
    (up / "leer.png").write_bytes(b"")
    res = opt()
    assert "fehler" not in res and res["uebersprungen"]["format_passt_nicht_zur_endung"] == 4
    meld = {d["datei"].rsplit("/", 1)[-1]: d["meldung"] for d in res["uebersprungen_details"]}
    assert meld == {"icon.png": "Inhalt ist ico, Endung .png",
                    "jpeg-als.png": "Inhalt ist jpeg, Endung .png",
                    "leer.png": "Inhalt ist kein bekanntes Bildformat, Endung .png",
                    "png-als.jpg": "Inhalt ist png, Endung .jpg"}


def test_exception_message_has_type_and_text(vhost, monkeypatch, tmp_path):
    # Werkzeug löscht seine Eingabe -> FileNotFoundError im Worker
    monkeypatch.setattr(server, "_IMG_JPEGOPTIM", _tool(tmp_path, "jpegoptim", 'for a; do f="$a"; done; rm "$f"\n'))
    make_jpeg(vhost / "httpdocs/uploads/a.jpg")
    err = opt()["fehler"][0]["fehler"]
    assert err.startswith("FileNotFoundError: ") and len(err) > len("FileNotFoundError: ")
    ns = _worker_ns()
    assert ns["exc_text"](OSError()) == "OSError: OSError()"
    assert ns["exc_text"](ns["Bad"]("")) == "unbekannter Fehler"


@pytest.mark.skipif(not (os.path.exists("/usr/bin/optipng") and os.path.exists("/usr/bin/jpegoptim")),
                    reason="echte jpegoptim/optipng nicht installiert")
def test_real_tools_reject_like_on_server(vhost, monkeypatch):
    """Nachgestellte Fälle vom Server mit den echten Werkzeugen."""
    import struct
    import zlib

    monkeypatch.setattr(server, "_IMG_JPEGOPTIM", "/usr/bin/jpegoptim")
    monkeypatch.setattr(server, "_IMG_OPTIPNG", "/usr/bin/optipng")

    def chunk(t, b):
        return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)

    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 20, 12, 8, 6, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(b"".join(b"\x00" + bytes(range(80)) for _ in range(12))))
    up = vhost / "httpdocs/uploads"
    # bKGD mit 1 Byte bei RGBA: optipng bricht ohne -fix ab
    (up / "EH.png").write_bytes(b"\x89PNG\r\n\x1a\n" + ihdr + chunk(b"bKGD", b"\x0d") + idat + chunk(b"IEND", b""))
    # abgeschnittenes PNG: kein IEND
    make_png(up / "full.png")
    data = (up / "full.png").read_bytes()
    (up / "full.png").unlink()
    (up / "cut-1920x1920.png").write_bytes(data[:len(data) // 2])
    res = opt()
    assert "fehler" not in res and res["uebersprungen"]["werkzeug_abgelehnt"] == 2
    for d in res["uebersprungen_details"]:
        assert d["meldung"].startswith("optipng Exit-Code 1: ") and "Error" in d["meldung"]


# --- Punkt 2: nichts im Webroot ---------------------------------------------

# wie das echte optipng: legt beim Bearbeiten "<datei>.bak" neben die Eingabe
FAKE_OPTIPNG_BAK = FAKE_OPTIPNG + "\n".join([
    "import os, time",
    "open(p + '.bak', 'wb').write(d)",
    "if b'slow' in d:",
    "    time.sleep(5)",
    "os.unlink(p + '.bak')",
    "",
])


def test_dry_run_creates_nothing_in_webroot(vhost, monkeypatch, tmp_path):
    f = tmp_path / "optipng"
    f.write_text(FAKE_OPTIPNG_BAK.replace("{py}", sys.executable))
    f.chmod(0o755)
    monkeypatch.setattr(server, "_IMG_OPTIPNG", str(f))
    monkeypatch.setattr(server, "_IMG_TOOL_TIMEOUT", 1)
    up = vhost / "httpdocs/uploads"
    sub = up / "ast-shipping-providers"
    sub.mkdir()
    make_png(sub / "a.png")
    make_jpeg(sub / "b.png")  # falsche Endung
    from PIL import PngImagePlugin
    info = PngImagePlugin.PngInfo()
    info.add_text("x", "slow")
    Image.new("RGB", (8, 8)).save(sub / "slow.png", "PNG", pnginfo=info)  # läuft in den Timeout
    make_jpeg(up / "c.jpg")
    old = up / ".alt.png.mcp-deadbeef.png.bak"
    old.write_bytes(b"x")
    os.utime(old, (0, 0))
    for d in (sub, up, vhost / "httpdocs"):
        os.utime(d, ns=(1_000_000_000, 1_000_000_000))
    before = _snapshot(vhost / "httpdocs")
    res = opt()
    assert res["uebersprungen"]["timeout"] == 1 and res["bilder_optimiert"] == 2
    assert _snapshot(vhost / "httpdocs") == before
    assert res["verwaiste_temp_dateien"][0]["aktion"] == "wird im echten Lauf gelöscht"


EXDEV_PRELUDE = r"""
_orig_replace = os.replace
def _replace(src, dst):
    if "/.plesk-mcp/tmp/" in src:
        raise OSError(errno.EXDEV, "Invalid cross-device link")
    if ".mcp-" in src and FAIL_LOCAL:
        raise OSError(errno.EIO, "simulierter Fehler")
    return _orig_replace(src, dst)
os.replace = _replace
"""


def test_exdev_copy_in_target_dir_removed_on_failure(vhost, monkeypatch):
    # Arbeitskopie auf anderem Dateisystem: Kopie im Zielordner, Ersetzen schlägt fehl
    _inject(monkeypatch, "FAIL_LOCAL = True\n" + EXDEV_PRELUDE)
    f = vhost / "httpdocs/uploads/a.jpg"
    make_jpeg(f)
    before = f.read_bytes()
    res = opt(dry_run=False, confirm=True)
    assert res["fehler"][0]["fehler"].startswith("OSError: ") and f.read_bytes() == before
    assert sorted(p.name for p in f.parent.iterdir()) == ["a.jpg"]


def test_exdev_replace_still_works(vhost, monkeypatch):
    _inject(monkeypatch, "FAIL_LOCAL = False\n" + EXDEV_PRELUDE)
    f = vhost / "httpdocs/uploads/a.jpg"
    make_jpeg(f)
    px = pixels(f)
    res = opt(dry_run=False, confirm=True)
    assert res["bilder_optimiert"] == 1 and "fehler" not in res and pixels(f) == px
    assert sorted(p.name for p in f.parent.iterdir()) == ["a.jpg"]


def test_orphaned_temp_files(vhost):
    up = vhost / "httpdocs/uploads"
    make_jpeg(up / "a.jpg")
    names = {
        "alt": ".IMG-1.png.mcp-c5aabcb5.png.bak",       # optipng-Sicherung aus 0.18, alt
        "alt_jpg": ".foto.JPG.mcp-0123abcd.JPG",        # Arbeitskopie aus 0.18, alt
        "jung": ".neu.png.mcp-1a87d294.png",            # jünger als 1 Stunde
        "fremd": ".index.php.mcp-1a2b3c4d.tmp",         # anderes Tool
    }
    for k, n in names.items():
        (up / n).write_bytes(b"x")
        if k != "jung":
            os.utime(up / n, (0, 0))
    os.utime(up, ns=(1_000_000_000, 1_000_000_000))
    before = _snapshot(up)
    res = opt()
    assert _snapshot(up) == before
    akt = {e["datei"].rsplit("/", 1)[-1]: e["aktion"] for e in res["verwaiste_temp_dateien"]}
    assert akt == {names["alt"]: "wird im echten Lauf gelöscht", names["alt_jpg"]: "wird im echten Lauf gelöscht",
                   names["jung"]: "behalten: jünger als 1 Stunde",
                   names["fremd"]: "behalten: nicht das Namensschema der Bildoptimierung"}
    res = opt(dry_run=False, confirm=True)
    akt = {e["datei"].rsplit("/", 1)[-1]: e["aktion"] for e in res["verwaiste_temp_dateien"]}
    assert akt[names["alt"]] == akt[names["alt_jpg"]] == "gelöscht"
    assert sorted(p.name for p in up.iterdir()) == sorted(["a.jpg", names["jung"], names["fremd"]])
    assert res["bilder_optimiert"] == 1


# --- Punkt 3: Ordner ohne Schreibrecht -------------------------------------

def test_no_write_permission_skipped_with_owner(vhost, monkeypatch):
    import grp
    import pwd

    up = vhost / "httpdocs/uploads"
    ig = up / "instagram"
    ig.mkdir()
    make_png(ig / "post03_badge_bluetooth.png")
    make_png(ig / "post04.png")
    make_jpeg(up / "a.jpg")
    st = os.lstat(ig)
    owner = f"{pwd.getpwuid(st.st_uid).pw_name}:{grp.getgrgid(st.st_gid).gr_name}"
    # Tests laufen evtl. als root: fehlendes Schreibrecht im Worker simulieren
    _inject(monkeypatch, r"""
_orig_access = os.access
os.access = lambda p, m: False if p.endswith("/instagram") and m & os.W_OK else _orig_access(p, m)
""")
    for kw in ({}, {"dry_run": False, "confirm": True}):
        before = _snapshot(ig)
        res = opt(**kw)
        assert "fehler" not in res and res["uebersprungen"]["keine_schreibrechte"] == 2
        assert res["ohne_schreibrechte"] == [{"ordner": "httpdocs/uploads/instagram", "besitzer": owner,
                                              "dateien": 2}]
        assert ("auch im echten Lauf" in res["schreibrechte_hinweis"]) == (not kw)
        assert res["bilder_optimiert"] == 1 and _snapshot(ig) == before


def test_large_png_uses_o1(vhost, monkeypatch, tmp_path):
    log = tmp_path / "args.log"
    monkeypatch.setattr(server, "_IMG_OPTIPNG", _tool(tmp_path, "optipng", f'echo "$@" >> {log}\n'))
    up = vhost / "httpdocs/uploads"
    make_png(up / "klein.png")
    big = Image.frombytes("RGB", (700, 700), os.urandom(700 * 700 * 3))
    big.save(up / "gross.png", "PNG", compress_level=0)
    assert (up / "gross.png").stat().st_size >= 1024 * 1024
    opt()
    lines = log.read_text().splitlines()
    assert any("-o1" in l and "-o2" not in l for l in lines) and any("-o2" in l for l in lines)
    assert len(lines) == 2


# --- Hintergrundmodus (optimize_images hintergrund=true, image_job) -------

def run_local_bg(c, args, job):
    """Wie server._img_run_bg, aber lokal: Worker per Pipe, stdin bleibt als
    Abbruchkanal offen, stdout wird zeilenweise konsumiert."""
    p = subprocess.Popen([sys.executable, "-I", "-c", server._IMG_WORKER_SRC], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.stdin.write(json.dumps({**args, "base": c["base"]}).encode() + b"\n")
    p.stdin.flush()

    def cancel():
        try:
            p.stdin.write(b"cancel\n")
            p.stdin.flush()
        except OSError:
            pass

    job["_cancel"] = cancel
    if job["_cancel_requested"]:
        cancel()
    res = server._img_consume(job, p.stdout)
    p.wait(timeout=30)
    if res is None:
        raise server.SSHError("keine Antwort: " + p.stderr.read().decode()[-300:])
    if not res["ok"]:
        raise ValueError(f"Bildoptimierung abgebrochen: {res.get('error')}")
    return res


@pytest.fixture
def bg(vhost, monkeypatch):
    audit = []
    monkeypatch.setattr(server, "_img_run_bg", run_local_bg)
    monkeypatch.setattr(server, "_audit", lambda action, **f: audit.append((action, f)))
    server._img_jobs.clear()
    yield audit
    for j in list(server._img_jobs.values()):
        if j["_cancel_requested"] is False and j["ended"] is None:
            j["_cancel_requested"] = True
            if j["_cancel"]:
                j["_cancel"]()
        j["_done"].wait(40)
    server._img_jobs.clear()


def start(**kw):
    return json.loads(server.optimize_images(DOMAIN, hintergrund=True, **kw))


def status(job_id, **kw):
    return json.loads(server.image_job(DOMAIN, "status", job_id, **kw))


def wait_job(job_id, timeout=40):
    import time
    end = time.time() + timeout
    while time.time() < end:
        s = status(job_id)
        if s["status"] != "laeuft":
            return s
        time.sleep(0.1)
    raise AssertionError("Job nicht fertig: " + json.dumps(s))


def wait_for_file(job_id, name, timeout=20):
    import time
    end = time.time() + timeout
    while time.time() < end:
        s = status(job_id)
        if s["status"] == "laeuft" and (s["fortschritt"].get("aktuelle_datei") or "").endswith(name):
            return s
        time.sleep(0.05)
    raise AssertionError("Datei nicht erreicht: " + json.dumps(s))


def test_bg_start_status_finish(vhost, bg):
    up = vhost / "httpdocs/uploads"
    for n in ("a.jpg", "b.jpg", "c.png"):
        (make_png if n.endswith("png") else make_jpeg)(up / n)
    res = start(dry_run=False, confirm=True)
    assert server._IMG_RUN_ID_RE.match(res["job_id"]) and res["status"] == "laeuft" and res["pfad"] == "httpdocs"
    assert "image_job" in res["hinweis"] and "backup_run_id = job_id" in res["hinweis"]
    s = wait_job(res["job_id"])
    assert s["status"] == "fertig" and s["job_id"] == res["job_id"] and s["modus"] == "ausgeführt"
    assert s["bilder_geprueft"] == 3 and s["bilder_optimiert"] == 3 and s["unvollstaendig"] is False
    assert s["backup_run_id"] == res["job_id"] and (vhost / ".plesk-mcp/image-backups" / res["job_id"] / "httpdocs/uploads/a.jpg").exists()
    assert s["listen_gesamt"] == {"groesste_ersparnisse": 3, "uebersprungen_details": 0, "fehler": 0}
    assert s["gekuerzt"] is False and "weiter_ab" not in s and "timeout_hinweis" not in s
    assert s["prioritaet"]["nice"] == 10 and isinstance(s["prioritaet"]["ionice"], bool)
    assert s["laufzeit_s"] >= 0 and s["gestartet"]
    assert not any((vhost / ".plesk-mcp/tmp").iterdir())
    assert sorted(p.name for p in up.iterdir()) == ["a.jpg", "b.jpg", "c.png"]
    acts = [a for a, _ in bg]
    assert acts == ["image_job_start", "image_job_done"]
    assert bg[1][1]["status"] == "fertig" and bg[1][1]["optimized"] == 3 and bg[1][1]["job_id"] == res["job_id"]
    # Sperre wieder frei
    assert opt()["bilder_geprueft"] == 3


def test_bg_dry_run_changes_nothing(vhost, bg):
    f = vhost / "httpdocs/uploads/a.jpg"
    make_jpeg(f)
    before = f.read_bytes()
    res = start()
    assert res["modus"].startswith("Hintergrund - Probelauf")
    s = wait_job(res["job_id"])
    assert s["status"] == "fertig" and s["modus"].startswith("Probelauf") and f.read_bytes() == before
    assert "backup_run_id" not in s and not (vhost / ".plesk-mcp/image-backups").exists()
    assert [a for a, _ in bg] == ["image_job_start"]  # Probelauf: kein done-Eintrag im Audit


def test_bg_progress_during_run(vhost, bg, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_IMG_JPEGOPTIM", _tool(tmp_path, "jpegoptim", "sleep 1\n"))
    make_jpeg(vhost / "httpdocs/uploads/a.jpg")
    make_jpeg(vhost / "httpdocs/uploads/b.jpg")
    res = start()
    s = wait_for_file(res["job_id"], "b.jpg")
    assert s["fortschritt"]["gesamt"] == 2 and s["fortschritt"]["bearbeitet"] == 1
    assert s["fortschritt"]["aktuelle_datei"] == "httpdocs/uploads/b.jpg" and s["laufzeit_s"] >= 1
    s = wait_job(res["job_id"])
    assert s["status"] == "fertig" and s["bilder_geprueft"] == 2


def test_bg_cancel_leaves_nothing_behind(vhost, bg, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_IMG_OPTIPNG", _tool(tmp_path, "optipng", "exec sleep 30\n"))
    monkeypatch.setattr(server, "_IMG_BG_TOOL_TIMEOUT", 120)
    up = vhost / "httpdocs/uploads"
    make_jpeg(up / "a.jpg")
    make_png(up / "b.png")
    make_png(up / "c.png")
    orig_a, orig_b, orig_c = (up / "a.jpg").read_bytes(), (up / "b.png").read_bytes(), (up / "c.png").read_bytes()
    res = start(dry_run=False, confirm=True)
    jid = res["job_id"]
    wait_for_file(jid, "b.png")
    assert list((vhost / ".plesk-mcp/tmp" / jid).iterdir())  # Arbeitskopie liegt an
    prev = json.loads(server.image_job(DOMAIN, "cancel", jid))
    assert prev["modus"] == "Vorschau - Job läuft weiter" and prev["status"] == "laeuft" and "confirm=true" in prev["hinweis"]
    assert status(jid)["status"] == "laeuft"
    out = json.loads(server.image_job(DOMAIN, "cancel", jid, confirm=True))
    assert out["modus"] == "Abbruch ausgeführt" and out["status"] == "abgebrochen" and out["abgebrochen"] == "cancel"
    assert out["bilder_geprueft"] == 1 and out["bilder_optimiert"] == 1 and out["unvollstaendig"] is True
    assert out["uebersprungen"]["abgebrochen_rest"] == 2 and "2 Dateien nicht mehr bearbeitet" in out["hinweis"]
    assert out["backup_run_id"] == jid
    assert (up / "a.jpg").read_bytes() != orig_a  # fertig ersetzt
    assert (up / "b.png").read_bytes() == orig_b and (up / "c.png").read_bytes() == orig_c
    assert sorted(p.name for p in up.iterdir()) == ["a.jpg", "b.png", "c.png"]
    assert not (vhost / ".plesk-mcp/tmp" / jid).exists()
    assert [a for a, _ in bg] == ["image_job_start", "image_job_cancel", "image_job_done"]
    assert bg[2][1]["status"] == "abgebrochen" and bg[2][1]["aborted"] == "cancel"
    with pytest.raises(ValueError, match="läuft nicht mehr"):
        server.image_job(DOMAIN, "cancel", jid, confirm=True)
    assert status(jid)["status"] == "abgebrochen"
    assert opt()["bilder_geprueft"] == 3  # Sperre frei


def test_bg_lock_blocks_other_runs(vhost, bg, monkeypatch, tmp_path):
    import asyncio
    from mcp.server.mcpserver.exceptions import ToolError
    monkeypatch.setattr(server, "_IMG_JPEGOPTIM", _tool(tmp_path, "jpegoptim", "exec sleep 30\n"))
    monkeypatch.setattr(server, "_IMG_BG_TOOL_TIMEOUT", 120)
    make_jpeg(vhost / "httpdocs/uploads/a.jpg")
    jid = start()["job_id"]
    wait_for_file(jid, "a.jpg")
    msg = "Für example.com läuft bereits eine Bildoptimierung - bitte warten"
    with pytest.raises(ToolError, match=msg):
        asyncio.run(server.mcp.call_tool("optimize_images", {"domain": DOMAIN}))
    with pytest.raises(ToolError, match=msg):
        asyncio.run(server.mcp.call_tool("optimize_images", {"domain": DOMAIN, "hintergrund": True}))
    with pytest.raises(ToolError, match=msg):
        asyncio.run(server.mcp.call_tool("image_backups", {"domain": DOMAIN, "action": "delete",
                                                            "run_id": "20260101120000-1a2b3c4d", "confirm": True}))
    assert len(server._img_jobs) == 1
    out = json.loads(server.image_job(DOMAIN, "cancel", jid, confirm=True))
    assert out["status"] == "abgebrochen"
    assert opt()["bilder_geprueft"] == 1


def test_bg_max_jobs(vhost, bg, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_IMG_JPEGOPTIM", _tool(tmp_path, "jpegoptim", "exec sleep 30\n"))
    monkeypatch.setattr(server, "_IMG_BG_TOOL_TIMEOUT", 120)
    monkeypatch.setattr(server, "_IMG_MAX_JOBS", 1)
    ctx = server._vhost_ctx(DOMAIN)
    monkeypatch.setattr(server, "_vhost_ctx", lambda d: {**ctx, "domain": d})
    make_jpeg(vhost / "httpdocs/uploads/a.jpg")
    jid = start()["job_id"]
    with pytest.raises(ValueError, match="laufen bereits 1 Hintergrundjobs"):
        server.optimize_images("other.example.com", hintergrund=True)
    json.loads(server.image_job(DOMAIN, "cancel", jid, confirm=True))


def test_bg_rejects_weiter_ab(vhost, bg):
    with pytest.raises(ValueError, match="weiter_ab gilt nur im Vordergrund"):
        start(weiter_ab="httpdocs/x.jpg")


def test_bg_unknown_job(vhost, bg, monkeypatch):
    make_jpeg(vhost / "httpdocs/uploads/a.jpg")
    jid = start()["job_id"]
    wait_job(jid)
    ctx = server._vhost_ctx(DOMAIN)
    monkeypatch.setattr(server, "_vhost_ctx", lambda d: {**ctx, "domain": d})
    with pytest.raises(ValueError, match="Unbekannte job_id .* für other.example.com"):
        server.image_job("other.example.com", "status", jid)  # Job gehört einer anderen Domain
    assert status(jid)["status"] == "fertig"
    server._img_jobs.clear()  # Neustart des MCP-Servers
    with pytest.raises(ValueError, match="neu gestartet.*image_backups"):
        status(jid)
    for bad in ("", "x", "../x", "20260101120000-1a2b3c4d; rm -rf /"):
        with pytest.raises(ValueError, match="Ungültige job_id"):
            server.image_job(DOMAIN, "status", bad)
    with pytest.raises(ValueError, match="action muss"):
        server.image_job(DOMAIN, "restart", "20260101120000-1a2b3c4d")


def test_bg_worker_error_becomes_status(vhost, bg):
    res = start(path="httpdocs/fehlt")
    s = wait_job(res["job_id"])
    assert s["status"] == "fehler" and "Bildoptimierung abgebrochen" in s["fehler_meldung"]
    assert "image_backups" in s["hinweis"]
    assert [a for a, _ in bg] == ["image_job_start", "image_job_failed"]
    assert opt()["bilder_geprueft"] == 0  # Sperre frei


def test_bg_paging(vhost, bg, monkeypatch):
    monkeypatch.setattr(server, "_IMG_JOB_PAGE", 3)
    up = vhost / "httpdocs/uploads"
    for i in range(7):
        make_jpeg(up / f"{i}.jpg")
    jid = start()["job_id"]
    s = wait_job(jid)
    assert s["gekuerzt"] is True and len(s["groesste_ersparnisse"]) == 3
    assert s["listen_gesamt"]["groesste_ersparnisse"] == 7 and "groesste_ersparnisse: 7" in s["gekuerzt_hinweis"]
    assert len(server._img_jobs[jid]["result"]["groesste_ersparnisse"]) == 7  # intern vollständig
    p1 = status(jid, liste="groesste_ersparnisse", ab=3, limit=3)
    assert p1["gesamt"] == 7 and len(p1["eintraege"]) == 3 and p1["weitere"] is True and "ab=6" in p1["hinweis"]
    p2 = status(jid, liste="groesste_ersparnisse", ab=6, limit=3)
    assert len(p2["eintraege"]) == 1 and p2["weitere"] is False and "hinweis" not in p2
    alle = status(jid, liste="groesste_ersparnisse", ab=0, limit=500)["eintraege"]
    assert sorted(e["datei"] for e in alle) == sorted(f"httpdocs/uploads/{i}.jpg" for i in range(7))
    assert status(jid, liste="fehler")["eintraege"] == []
    with pytest.raises(ValueError, match="liste muss"):
        status(jid, liste="dateien")
    with pytest.raises(ValueError, match="limit muss"):
        status(jid, liste="fehler", limit=0)
    with pytest.raises(ValueError, match="ab muss"):
        status(jid, liste="fehler", ab=-1)


def test_fg_lists_report_truncation(vhost, monkeypatch):
    monkeypatch.setattr(server, "_IMG_LIST_LIMIT", 2)
    for i in range(3):
        make_jpeg(vhost / f"httpdocs/uploads/{i}.jpg")
    res = opt()
    assert len(res["groesste_ersparnisse"]) == 2 and res["gekuerzt"] is True
    assert res["listen_gesamt"]["groesste_ersparnisse"] == 3 and "hintergrund=true" in res["gekuerzt_hinweis"]


def test_fg_timeout_hint_points_to_background(vhost, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_IMG_JPEGOPTIM", _tool(tmp_path, "jpegoptim", "sleep 5\n"))
    monkeypatch.setattr(server, "_IMG_TOOL_TIMEOUT", 1)
    make_jpeg(vhost / "httpdocs/uploads/a.jpg")
    res = opt()
    assert res["timeout_hinweis"].startswith("1 Datei(en) im Timeout (1 s")
    assert 'hintergrund=true und dateien=["httpdocs/uploads/a.jpg"]' in res["timeout_hinweis"]


def test_dateien_param(vhost):
    up = vhost / "httpdocs/uploads"
    make_jpeg(up / "a.jpg")
    make_png(up / "b.png")
    (up / "sub").mkdir()
    make_jpeg(up / "sub/c.jpg")
    (up / "text.txt").write_text("x")
    os.symlink(up / "a.jpg", up / "link.jpg")
    (up / "sublink").symlink_to(up / "sub")
    orig_b = (up / "b.png").read_bytes()
    res = opt(dateien=["httpdocs/uploads/a.jpg", "httpdocs/uploads/sub/c.jpg", "httpdocs/uploads/sub/c.jpg",
                       "httpdocs/uploads/text.txt", "httpdocs/uploads/fehlt.jpg", "httpdocs/uploads/link.jpg",
                       "httpdocs/uploads/sublink/c.jpg"], dry_run=False, confirm=True)
    assert res["pfad"] == "dateien (6 Einträge)" and res["bilder_geprueft"] == 2 and res["bilder_optimiert"] == 2
    assert (up / "b.png").read_bytes() == orig_b  # nicht in der Liste
    assert res["uebersprungen"] == {"kein_bild": 1, "nicht_gefunden": 1, "symlink": 2}
    gr = {d["datei"]: d["grund"] for d in res["uebersprungen_details"]}
    assert gr == {"httpdocs/uploads/text.txt": "kein_bild", "httpdocs/uploads/fehlt.jpg": "nicht_gefunden",
                  "httpdocs/uploads/link.jpg": "symlink", "httpdocs/uploads/sublink/c.jpg": "symlink"}
    assert "fehler" not in res and "verwaiste_temp_dateien" not in res
    assert sorted(e["datei"] for e in res["groesste_ersparnisse"]) == ["httpdocs/uploads/a.jpg", "httpdocs/uploads/sub/c.jpg"]
    for bad, msg in (("../etc/x.jpg", "'\\.\\.'"), ("/etc/x.jpg", "nur relative Pfade"),
                     ("httpdocs/../conf/x.jpg", "'\\.\\.'"), ("conf/x.jpg", "keinem Docroot"),
                     ("httpdocs", "keinem Docroot"), ("httpdocs/*.jpg", "Wildcards")):
        with pytest.raises(ValueError, match=msg):
            opt(dateien=["httpdocs/uploads/a.jpg", bad])
    with pytest.raises(ValueError, match="höchstens 200"):
        opt(dateien=[f"httpdocs/{i}.jpg" for i in range(201)])
    with pytest.raises(ValueError, match="nicht beides"):
        opt(path="httpdocs", dateien=["httpdocs/uploads/a.jpg"])
    with pytest.raises(ValueError, match="dateien ist leer"):
        opt(dateien=[])
    with pytest.raises(ValueError, match="Liste relativer Pfade"):
        opt(dateien=["httpdocs/uploads/a.jpg", 5])


def test_bg_dateien(vhost, bg):
    up = vhost / "httpdocs/uploads"
    make_jpeg(up / "a.jpg")
    make_jpeg(up / "b.jpg")
    res = start(dateien=["httpdocs/uploads/b.jpg", "httpdocs/uploads/nein.jpg"], dry_run=False, confirm=True)
    assert res["pfad"] == "dateien (2 Einträge)"
    s = wait_job(res["job_id"])
    assert s["bilder_optimiert"] == 1 and s["groesste_ersparnisse"][0]["datei"] == "httpdocs/uploads/b.jpg"
    assert s["uebersprungen_details"] == [{"datei": "httpdocs/uploads/nein.jpg", "grund": "nicht_gefunden",
                                          "meldung": "Datei nicht gefunden"}]
    assert bg[0][1]["path"] == "dateien (2 Einträge)"


def _bg_worker(vhost, tmp_path, tool_body):
    """Worker direkt im Hintergrundmodus starten (ohne MCP-Schicht)."""
    args = {"op": "optimize", "parts": ["httpdocs"], "dateien": [], "dry_run": False, "recursive": True,
            "keep_backup": True, "max_bytes": 50 * 1024 * 1024, "run_id": "20260101120000-1a2b3c4d",
            "jpegoptim": _tool(tmp_path, "jpegoptim", tool_body), "optipng": "/nonexistent",
            "background": True, "max_files": 0, "start_after": "", "time_budget": None, "tool_timeout": 120,
            "list_limit": 0, "base": str(vhost)}
    p = subprocess.Popen([sys.executable, "-I", "-c", server._IMG_WORKER_SRC], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.stdin.write(json.dumps(args).encode() + b"\n")
    p.stdin.flush()
    return p


@pytest.mark.parametrize("how,reason", [("eof", "verbindung_verloren"), ("sigterm", "signal"), ("sighup", "signal")])
def test_bg_worker_aborts_cleanly_on_connection_loss(vhost, tmp_path, how, reason):
    import signal
    up = vhost / "httpdocs/uploads"
    make_jpeg(up / "a.jpg")
    make_jpeg(up / "b.jpg")
    before = {n: (up / n).read_bytes() for n in ("a.jpg", "b.jpg")}
    p = _bg_worker(vhost, tmp_path, "exec sleep 30\n")
    first = json.loads(p.stdout.readline())
    assert first["fortschritt"]["datei"] == "httpdocs/uploads/a.jpg" and first["fortschritt"]["gesamt"] == 2
    tmp = vhost / ".plesk-mcp/tmp/20260101120000-1a2b3c4d"
    deadline = __import__("time").time() + 10
    while not (tmp.exists() and list(tmp.iterdir())) and __import__("time").time() < deadline:
        __import__("time").sleep(0.05)
    assert list(tmp.iterdir())
    if how == "eof":
        p.stdin.close()
    else:
        # stdin bleibt offen, bis der Worker fertig ist - sonst käme das EOF dem
        # Signal zuvor und der Grund wäre "verbindung_verloren"
        p.send_signal(signal.SIGTERM if how == "sigterm" else signal.SIGHUP)
    out = p.stdout.read()
    p.wait(timeout=20)
    p.stdin.close()
    p.stderr.close()
    res = json.loads(out.strip().splitlines()[-1])
    assert res["ok"] and res["abgebrochen"] == reason and res["geprueft"] == 0 and res["optimiert"] == 0
    assert res["prioritaet"] == {"nice": 10, "ionice": os.path.exists("/usr/bin/ionice")}
    assert res["uebersprungen"] == {"abgebrochen_rest": 2}
    assert {n: (up / n).read_bytes() for n in before} == before
    assert sorted(x.name for x in up.iterdir()) == ["a.jpg", "b.jpg"]
    assert not tmp.exists() and not any((vhost / ".plesk-mcp/tmp").iterdir())
    assert not (vhost / ".plesk-mcp/image-backups").exists()


def test_progress_values_are_sanitised():
    job = {"fortschritt": {"bearbeitet": 1, "gesamt": 5, "aktuelle_datei": None}}
    server._img_progress(job, {"bearbeitet": "7", "gesamt": True, "optimiert": -1, "fehler": 10 ** 13,
                               "datei": "x" * 500, "uebersprungen": 3})
    assert job["fortschritt"] == {"bearbeitet": 1, "gesamt": 5, "aktuelle_datei": "x" * 300, "uebersprungen": 3}
    server._img_progress(job, "kein dict")
    server._img_progress(job, {"datei": ["liste"]})
    assert job["fortschritt"]["aktuelle_datei"] == "x" * 300
    assert server._img_consume(job, [b"kein json\n", b"{\"fortschritt\": {\"gesamt\": 9}}\n", b"[1]\n",
                                     b"{\"ok\": true, \"x\": 1}\n"]) == {"ok": True, "x": 1}
    assert job["fortschritt"]["gesamt"] == 9
    assert server._img_consume(job, [b".\n"]) is None
