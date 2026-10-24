"""
plesk-mcp

Version: wird automatisch gezählt (siehe _read_version unten). Abfragbar über
das get_version-Tool bzw. serverInfo im MCP-Handshake, um nach einem Redeploy
die ausgerollte Version zu prüfen.

Read-only MCP-Server für Diagnose auf einem Plesk-Server. Drei Datenquellen:

- SSH (paramiko) für alles, was nur auf Betriebssystem-Ebene existiert
  (PHP-FPM-Status, systemd-Journal, OOM-Kills, Disk-Nutzung, Serverlast) und
  für Plesk-CLI-Befehle (plesk bin ..., plesk ext ...).
- Die Plesk-REST-API (X-API-Key) für strukturierte Plesk-eigene Daten
  (Domains, Subscriptions etc.) über das generische plesk_api_get-Tool.
- MySQL/MariaDB direkt (über den Plesk-internen Admin-DB-Zugang aus
  /etc/psa/.psa.shadow, siehe db_list/db_query/db_search) für read-only
  Datenbank-Abfragen ohne eigene, zusätzlich zu konfigurierende
  DB-Zugangsdaten.

Gedacht für Fehleranalyse bei Support-Anfragen (z.B. "Website nicht erreichbar").
Fast alle Tools sind read-only: kein Neustart von Services, keine destruktiven
Kommandos (Whitelist + Blacklist weiter unten). Ausnahme: write_vhost_file,
replace_in_vhost_file, replace_in_vhost_files, set_php_handler, php_settings_update,
php_settings_rollback, web_server_settings_update, web_server_settings_rollback,
optimize_images, image_backups, delete_vhost_backup, delete_vhost_log,
dns_add_record, dns_delete_record, dns_update_record, imunify_ignore_add, imunify_ignore_remove, wp_option_update
wp_option_rollback sowie die Datei-Tools upload_begin, fetch_to_vhost,
move_vhost_file, delete_vhost_file, restore_vhost_trash und empty_vhost_trash
(siehe README "Datei-Upload"/"Dateiverwaltung") dürfen schreiben - die ersten beiden Dateien innerhalb von
/var/www/vhosts/<domain>/ anlegen/überschreiben/löschen (Backups),
delete_vhost_log Logdateien unter /var/www/vhosts/<domain>/logs/ löschen
(rotierte) bzw. leeren (aktive), die drei dns_*-Tools DNS-Resource-Records der
Domain-Zone anlegen/entfernen/ändern (plesk bin dns --add/--del;
dns_update_record kombiniert beides für einen bestehenden Record).
imunify_ignore_add/imunify_ignore_remove ändern ausschliesslich die
Imunify360-Malware-Ignore-Liste (nur Pfade unter /var/www/vhosts/),
wp_option_update/wp_option_rollback einzelne freigegebene WordPress-Optionen
(per WP-CLI als Subscription-User). Alle schreibenden Tools erfordern zwingend confirm=true pro Aufruf
(keine globale Freischaltung), write_vhost_file und die replace_*-Tools legen
vor dem Überschreiben automatisch ein Backup der alten Version an.

Läuft ausschliesslich als HTTP-Dienst (Streamable HTTP) im Docker-Container
hinter einem Reverse-Proxy und erfordert MCP_API_KEY. Ein lokaler Betrieb
(stdio/uv) ist nicht vorgesehen.

Alle Zugangsdaten (SSH, Plesk-API-Key, MCP_API_KEY) werden ausschliesslich
über Umgebungsvariablen gesetzt - es sind keine echten Zugangsdaten im Code
hinterlegt.
"""

from __future__ import annotations

import base64
import datetime
import functools
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import posixpath
import re
import shlex
import secrets
import socket
import stat
import struct
import sys
import threading
import time
import contextvars
from typing import Any

import paramiko
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError


_HTTP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
_HTTP_PORT = int(os.environ.get("MCP_PORT", "8000"))
_MCP_API_KEY = os.environ.get("MCP_API_KEY", "")

# Version = <Major.Minor>.<Patch>. Major.Minor wird hier von Hand gepflegt, der
# Patch-Teil zaehlt automatisch: Anzahl Commits seit dem Commit, der diese
# Zeile zuletzt geaendert hat, die eine der build-relevanten Dateien
# (server.py, Dockerfile, requirements.txt, Workflow) geaendert haben. Der
# Commit mit einer neuen Basis ergibt also <Basis>.0.
# Im Docker-Image setzt GitHub Actions die fertige Version als APP_VERSION,
# lokal (Git-Checkout) wird sie direkt aus der Git-Historie berechnet.
_VERSION_BASE = "0.31"
_VERSION_PATHS = ["server.py", "Dockerfile", "requirements.txt", ".github/workflows/docker-publish.yml"]


def _read_version() -> str:
    env_version = os.environ.get("APP_VERSION", "").strip()
    if env_version:
        return env_version
    try:
        import subprocess
        cwd = os.path.dirname(os.path.abspath(__file__))
        base_commit = subprocess.run(
            ["git", "log", "-1", "--format=%H", "-G", r'^_VERSION_BASE = "', "--", "server.py"],
            cwd=cwd, capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        rev = f"{base_commit}..HEAD" if base_commit else "HEAD"
        count = subprocess.run(
            ["git", "rev-list", "--count", rev, "--", *_VERSION_PATHS],
            cwd=cwd, capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        if count.isdigit():
            return f"{_VERSION_BASE}.{count}"
    except Exception:
        pass
    return f"{_VERSION_BASE}.0-dev"


__version__ = _read_version()

mcp = MCPServer("plesk-mcp", version=__version__)


class _UserError(ToolError, ValueError):
    """Ablehnung/Validierungsfehler mit Klartext-Meldung für den Client."""


_mcp_tool = mcp.tool


def _tool_with_messages(*args: Any, **kwargs: Any):
    """Wie mcp.tool(), aber ValueError (Ablehnungen mit Begründung) wird als
    ToolError gemeldet - sonst zeigt der Client nur "Error executing tool"
    ohne die Begründung. Registriert wird der Wrapper, das Modul behält die
    Originalfunktion (direkte Aufrufe, z.B. in Tests, bleiben unverändert)."""
    register = _mcp_tool(*args, **kwargs)

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*a: Any, **kw: Any):
            try:
                return fn(*a, **kw)
            except ToolError:
                raise
            except ValueError as e:
                raise _UserError(str(e)) from e

        register(wrapper)
        return fn

    return decorator


mcp.tool = _tool_with_messages

# ---------------------------------------------------------------------------
# SSH-Verbindung zum Plesk-Server
# ---------------------------------------------------------------------------

_SSH_HOST = os.environ.get("PLESK_SSH_HOST", "")
_SSH_PORT = int(os.environ.get("PLESK_SSH_PORT", "22"))
_SSH_USER = os.environ.get("PLESK_SSH_USER", "root")
_SSH_PASSWORD = os.environ.get("PLESK_SSH_PASSWORD")
_SSH_KEY_PATH = os.environ.get("PLESK_SSH_KEY_PATH")  # Alternative zu Passwort
_SSH_TIMEOUT = int(os.environ.get("PLESK_SSH_TIMEOUT", "20"))
# Host-Key-Verifikation (MITM-Schutz). Beide Werte kommen aus der Umgebung -
# im oeffentlichen Repo darf kein Host-Key hinterlegt sein.
#   PLESK_SSH_KNOWN_HOSTS: Pfad zu einer known_hosts-Datei (gemountet).
#   PLESK_SSH_HOST_KEY:    ein known_hosts-/authorized_keys-Zeilenrest, also
#                          "<typ> <base64>" (z.B. "ssh-ed25519 AAAAC3Nza...").
# Einer von beiden ist Pflicht: Der Host-Key wird immer strikt geprueft
# (RejectPolicy bei unbekanntem Key). Ohne gepinnten Key startet der Server
# nicht (siehe _check_startup_config) - ein automatisches Akzeptieren beim
# ersten Connect (AutoAddPolicy) gibt es nicht mehr, weil damit bei
# Passwort-Authentifizierung ein MITM das Root-Passwort abgreifen koennte.
_SSH_KNOWN_HOSTS = os.environ.get("PLESK_SSH_KNOWN_HOSTS", "").strip()
_SSH_HOST_KEY = os.environ.get("PLESK_SSH_HOST_KEY", "").strip()
# Mindestlaenge des statischen Bearer-Tokens (MCP_API_KEY): 32 Zeichen, also
# mindestens 128 Bit bei Hex bzw. ~190 Bit bei base64/urlsafe-Zufall.
_MCP_API_KEY_MIN_LEN = 32
# Ausgabe-Obergrenze pro SSH-Kommando (stdout und stderr je) - danach wird der
# Kanal geschlossen und die Ausgabe als gekuerzt markiert. Schuetzt den
# Container vor OOM durch z.B. "cat" auf riesige Dateien.
_SSH_MAX_OUTPUT = 4 * 1024 * 1024
# Harte Gesamtlaufzeit pro SSH-Kommando (Sekunden), unabhaengig davon, ob
# Ausgabe fliesst (der paramiko-Timeout ist nur ein Inaktivitaets-Timeout).
# Lang laufende Tools (Helper, WP-CLI, Bildoptimierung) uebergeben ihren
# eigenen Wert; der Standard ist nur ein Sicherheitsnetz.
_SSH_MAX_RUNTIME = int(os.environ.get("PLESK_SSH_MAX_RUNTIME", "1800"))
# Laufzeitlimit fuer run_diagnostic: wird dem Remote-Kommando als
# "timeout <s>" vorangestellt und gilt zusaetzlich als harte Deadline.
_DIAG_TIMEOUT = int(os.environ.get("DIAG_TIMEOUT", "60"))

# Kommandos, die der generische Diagnose-Tool (run_diagnostic) ausführen darf
# (read-only). Jeder Eintrag ist ein erlaubtes erstes Wort (Programm) des
# Befehls - "plesk" deckt damit auch "plesk bin dns ..." und
# "plesk ext wp-toolkit ..." ab.
ALLOWED_COMMAND_PREFIXES = {
    "plesk",
    "lveinfo",
    "lveps",
    "lve_readall",
    "systemctl",
    "journalctl",
    "grep",
    "tail",
    "head",
    "cat",
    "find",
    "ls",
    "du",
    "df",
    "dmesg",
    "hostname",
    "uptime",
    "top",
    "free",
    "ps",
    "wc",
    # Nur lesende Dekomprimierung von rotierten .gz-Logs - im Gegensatz zu
    # "gunzip" (löscht standardmässig die Originaldatei) bewusst NICHT
    # freigegeben, obwohl gunzip sonst ein plausibler Kandidat wäre.
    "zcat",
    "zgrep",
}

# Subcommands/Wörter, die trotz erlaubtem Programmnamen verboten bleiben
# (z.B. systemctl restart/stop). Als ganzes Wort geprüft (Regex \b...\b),
# damit z.B. "lve_kill_log" (legitimer Dateiname) nicht fälschlich wegen
# "kill" blockiert wird, "rm -rf" aber schon.
FORBIDDEN_WORDS = {
    "restart", "stop", "reload", "start", "kill", "killall", "rm", "delete",
    "remove", "passwd", "shutdown", "reboot", "mv", "dd", "chmod", "chown",
    "mkfs", "bash", "sh", "zsh", "python", "python3", "perl", "curl", "wget",
    "nc", "eval", "exec",
    # Generische Schreib-/Zustands-Subcommands, v.a. relevant fuer
    # "plesk bin dns/...": --add/-a, --del/-d, --set, --reset, --on/--off,
    # --update-soa etc. sind alle schreibend, auch wenn "plesk" selbst in
    # ALLOWED_COMMAND_PREFIXES steht - ohne diese Woerter waeren sie ueber
    # run_diagnostic trotz "read-only"-Anspruch ausfuehrbar gewesen (siehe
    # dns_add_record/dns_delete_record/dns_update_record fuer den
    # vorgesehenen, confirm=true-abgesicherten Weg, DNS-Records zu aendern).
    "add", "del", "set", "reset", "on", "off", "update",
}
_FORBIDDEN_WORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in FORBIDDEN_WORDS) + r")\b", re.IGNORECASE
)

# Shell-Konstrukte, die Command-Chaining/Injection ermöglichen - werden als
# reiner Substring-Check verboten, unabhängig vom Rest des Befehls.
# Newline/Carriage-Return sind bewusst dabei: das Kommando wird als String an
# eine Remote-Shell uebergeben, ein "\n" darin startet eine zweite Befehlszeile
# (z.B. "ls /tmp\nid") - der per-Segment-Programmcheck sieht davon nichts.
FORBIDDEN_SHELL_CONSTRUCTS = (";", "&&", "||", "`", "$(", ">", "<", "&", "\n", "\r")

# find-Primitive, die Kommandos ausfuehren oder Dateien schreiben - fuer find
# separat geprueft, weil der \b-Wortcheck sie nicht zuverlaessig fasst
# (z.B. "-execdir" enthaelt "exec" nicht als eigenes Wort). Vergleich erfolgt
# auf dem Token ohne fuehrende Bindestriche.
FORBIDDEN_FIND_PRIMARIES = {
    "exec", "execdir", "ok", "okdir", "delete",
    "fprint", "fprintf", "fls", "fprint0",
}

# Positive Allowlist der erlaubten Subcommands/ersten Argumente je Programm,
# das schreibende Unterbefehle kennt. Nur lesende Operationen. Eine
# Positiv-Liste ist hier noetig, weil eine Blacklist bei "plesk"/"systemctl"
# die Schreib-Subcommands nicht dicht bekommt (plesk db/login/bin extension
# --install-url, systemctl mask/disable/poweroff/isolate ...).
SAFE_SUBCOMMANDS = {
    "systemctl": {"status", "is-active", "list-units", "show", "cat"},
    # plesk: nur die lesenden CLI-Familien; innerhalb davon gilt zusaetzlich
    # die Positiv-Liste der Aktionen in _check_plesk (nur -i/--info, -l/--list,
    # --show-*), siehe dort.
    "plesk": {"version", "bin", "ext"},
}

# Verbotene erste Subcommands je Programm (haben Vorrang vor SAFE_SUBCOMMANDS).
# "plesk db" oeffnet eine SQL-Shell auf der Plesk-Datenbank, "plesk login"
# erzeugt einen Admin-Login-Link, "plesk repair" schreibt.
FORBIDDEN_SUBCOMMANDS = {
    "plesk": {"login", "db", "repair", "sbin", "daemon", "installer"},
}

# Schreibende Optionsflags, die unabhaengig vom Programm blockiert werden
# (v.a. "plesk bin/ext ..."): install/create/enable/disable etc. sind
# schreibend, tauchen aber als Langoptionen auf und werden von der reinen
# Wort-Blacklist nicht erfasst.
FORBIDDEN_SUBFLAGS = {
    "install", "install-url", "create", "enable", "disable", "repair",
    "exec", "execute", "uninstall", "upgrade", "activate", "deactivate",
    "wp-cli",
}

# --- Positiv-Listen je Utility fuer run_diagnostic -------------------------
#
# plesk bin <utility> <aktion> [optionen]: Die Aktion (erstes Argument nach dem
# Utility) muss lesend sein. Plesk-CLI-Konvention: Langaktionen "--xxx",
# Kurzaktionen "-x", Optionen danach als "-name wert". Kurzaktionen sind je
# Utility verschieden belegt ("-i" ist bei "domain" --info, bei "extension"
# aber --install), darum sind -c/-u/-r/-i/-e grundsaetzlich gesperrt und "-i"
# nur bei den Utilities erlaubt, bei denen es --info bedeutet.
_PLESK_UTIL_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
_PLESK_READ_ACTIONS = {"--info", "--list", "-l"}
# "-i" == "--info" bei diesen Utilities
_PLESK_INFO_SHORT_UTILS = {
    "domain", "subscription", "customer", "reseller", "site", "subdomain",
    "domain_alias", "dns", "mail", "database", "service_plan", "service_plan_addon",
}
# "-s" == "--show" (server_pref)
_PLESK_SHOW_SHORT_UTILS = {"server_pref"}
# Kurzoptionen, die bei plesk bin/ext nie erlaubt sind (-c=--create,
# -u=--update, -r=--remove, -i=--install bei extension, -e=--enable).
_PLESK_FORBIDDEN_SHORT = {"-c", "-u", "-r", "-i", "-e"}
# "--show-*" ist lesend - ausser --show-password (gibt das Admin-Passwort aus).
_PLESK_FORBIDDEN_SHOW = {"--show-password"}
_PLESK_EXT_READ_ACTIONS = {"--list", "-l", "--info"}

# journalctl: Optionen, die Journal-Dateien loeschen/rotieren/schreiben oder
# nie enden (-f/--follow). Vergleich auf dem Namen ohne Bindestriche, vor "=".
_JOURNALCTL_FORBIDDEN = {
    "vacuum-size", "vacuum-time", "vacuum-files", "rotate", "flush", "sync",
    "relinquish-var", "smart-relinquish-var", "setup-keys", "force",
    "cursor-file", "follow", "f",
}
_JOURNALCTL_SHORT_WITH_ARG = set("upSUnotDMgFc")

# dmesg: -C/-c/--clear/--read-clear loeschen den Ring-Buffer, -n/-D/-E aendern
# das Konsolen-Logging, -w/-W warten endlos.
_DMESG_FORBIDDEN = {
    "clear", "read-clear", "console-level", "console-off", "console-on",
    "follow", "follow-new", "C", "c", "n", "D", "E", "w", "W",
}
_DMESG_SHORT_WITH_ARG = set("Ffls")

# tail: -f/-F/--follow/--pid/--retry enden nie (bzw. nur mit --pid).
_TAIL_FORBIDDEN = {"follow", "pid", "retry", "f", "F"}
_TAIL_SHORT_WITH_ARG = set("ncs")

# du: Symlinks nie verfolgen (sonst fuehrt ein Link aus den erlaubten Pfaden
# heraus), keine Pfadlisten aus Dateien.
_DU_FORBIDDEN = {"dereference", "dereference-args", "files0-from", "L", "D", "H"}
_DU_SHORT_WITH_ARG = set("BdtX")
_DU_LONG_WITH_ARG = {"block-size", "max-depth", "threshold", "exclude-from", "exclude", "time-style"}

# find: -L/-H/-follow verfolgen Symlinks (siehe du), dazu die Aktions-Primitive
# aus FORBIDDEN_FIND_PRIMARIES.
_FIND_FORBIDDEN = {"L", "H", "follow"} | FORBIDDEN_FIND_PRIMARIES

# grep/zgrep: -r/--recursive nur mit explizitem Pfad in den erlaubten Basen;
# -R/--dereference-recursive folgt allen Symlinks und ist gesperrt.
_GREP_RECURSIVE = {"recursive", "r"}
_GREP_FORBIDDEN = {"dereference-recursive", "R"}
_GREP_SHORT_WITH_ARG = set("ABCmefdD")
_GREP_LONG_WITH_ARG = {
    "regexp", "file", "max-count", "context", "after-context", "before-context",
    "binary-files", "color", "colour", "devices", "directories", "exclude",
    "exclude-from", "exclude-dir", "include", "label",
}

# Basispfade, unterhalb derer find/du/grep -r suchen duerfen (absolut, ohne
# Shell-Expansionen). Alles andere waere ein Scan des ganzen Servers.
_DIAG_PATH_BASES = ("/var/www/vhosts", "/var/log", "/etc/nginx", "/etc/httpd", "/usr/local/psa")
# Zeichen in Pfadargumenten, die die Remote-Shell noch expandieren wuerde und
# damit den Prefix-Check aushebeln koennten ("/var/log/${X:-..}/..", "{..,x}").
_DIAG_PATH_BAD_CHARS = set("$`{}~")
# Geraete/Pseudodateien, die endlos liefern oder den Kernelspeicher ausgeben.
_DIAG_FORBIDDEN_PATHS = (
    "/dev/zero", "/dev/urandom", "/dev/random", "/dev/mem", "/dev/kmem", "/dev/port",
    "/proc/kcore", "/proc/kmsg",
)


class SSHError(ToolError, RuntimeError):
    """Erwarteter SSH-/Validierungsfehler. Als ToolError sieht der Client die
    Meldung selbst statt nur 'Error executing tool <name>'."""


def _configure_host_keys(client: paramiko.SSHClient) -> None:
    """Konfiguriert die Host-Key-Verifikation aus den Umgebungsvariablen
    PLESK_SSH_KNOWN_HOSTS/PLESK_SSH_HOST_KEY: immer strikte Pruefung
    (RejectPolicy). Ohne gepinnten Key wird keine Verbindung aufgebaut."""
    pinned = False

    if _SSH_KNOWN_HOSTS:
        client.load_host_keys(_SSH_KNOWN_HOSTS)  # wirft, wenn Pfad fehlt
        pinned = True

    if _SSH_HOST_KEY:
        import base64 as _b64
        parts = _SSH_HOST_KEY.split()
        # ssh-keyscan-Format "<host> <typ> <base64>" tolerieren
        if len(parts) >= 3 and not parts[0].startswith(("ssh-", "ecdsa-", "sk-")):
            parts = parts[1:]
        if len(parts) < 2:
            raise SSHError(
                "PLESK_SSH_HOST_KEY muss das Format '<typ> <base64>' haben "
                "(z.B. 'ssh-ed25519 AAAAC3Nza...')."
            )
        keytype, keyblob = parts[0], parts[1]
        try:
            key = paramiko.PKey.from_type_string(keytype, _b64.b64decode(keyblob))
        except Exception:
            # Fallback fuer aeltere paramiko-Versionen ohne from_type_string
            key_classes = {
                "ssh-ed25519": paramiko.Ed25519Key,
                "ssh-rsa": paramiko.RSAKey,
                "ecdsa-sha2-nistp256": paramiko.ECDSAKey,
                "ecdsa-sha2-nistp384": paramiko.ECDSAKey,
                "ecdsa-sha2-nistp521": paramiko.ECDSAKey,
            }
            cls = key_classes.get(keytype)
            if not cls:
                raise SSHError(f"Nicht unterstuetzter Host-Key-Typ: {keytype}")
            key = cls(data=_b64.b64decode(keyblob))
        # paramiko sucht den Key bei Nicht-Standard-Port unter "[host]:port"
        host_entry = _SSH_HOST if _SSH_PORT == 22 else f"[{_SSH_HOST}]:{_SSH_PORT}"
        client.get_host_keys().add(host_entry, keytype, key)
        pinned = True

    if not pinned:
        raise SSHError(
            "PLESK_SSH_HOST_KEY oder PLESK_SSH_KNOWN_HOSTS ist nicht gesetzt - ohne "
            "gepinnten Host-Key wird keine SSH-Verbindung aufgebaut (kein MITM-Schutz)."
        )
    client.set_missing_host_key_policy(paramiko.RejectPolicy())


def _key_fingerprint(key: paramiko.PKey) -> str:
    """SHA256-Fingerprint wie ssh-keygen -lf (zum Abgleich mit dem Server)."""
    import base64 as _b64
    import hashlib
    return "SHA256:" + _b64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


def _ssh_connect() -> paramiko.SSHClient:
    if not _SSH_HOST or not _SSH_USER:
        raise SSHError(
            "PLESK_SSH_HOST/PLESK_SSH_USER sind nicht gesetzt (Umgebungsvariablen fehlen)."
        )
    client = paramiko.SSHClient()
    try:
        _configure_host_keys(client)
    except SSHError:
        raise
    except (ValueError, paramiko.SSHException) as e:
        raise SSHError(
            "PLESK_SSH_HOST_KEY ist ungueltig - erwartet '<typ> <base64>', z.B. "
            "'ssh-ed25519 AAAAC3Nza...' (ohne Hostname davor)."
        ) from e
    except OSError as e:
        raise SSHError(f"PLESK_SSH_KNOWN_HOSTS nicht lesbar ({_SSH_KNOWN_HOSTS}): {e.strerror or e}") from e
    connect_kwargs: dict[str, Any] = dict(
        hostname=_SSH_HOST,
        port=_SSH_PORT,
        username=_SSH_USER,
        timeout=_SSH_TIMEOUT,
        banner_timeout=_SSH_TIMEOUT,
        auth_timeout=_SSH_TIMEOUT,
    )
    if _SSH_KEY_PATH:
        connect_kwargs["key_filename"] = _SSH_KEY_PATH
    elif _SSH_PASSWORD:
        connect_kwargs["password"] = _SSH_PASSWORD
    else:
        raise SSHError("Weder PLESK_SSH_PASSWORD noch PLESK_SSH_KEY_PATH gesetzt.")
    target = f"{_SSH_HOST}:{_SSH_PORT}"
    try:
        client.connect(**connect_kwargs)
    except paramiko.BadHostKeyException as e:
        raise SSHError(
            f"SSH-Host-Key von {target} passt nicht zum hinterlegten Key "
            f"(Server: {e.key.get_name()} {_key_fingerprint(e.key)}, "
            f"erwartet: {e.expected_key.get_name()} {_key_fingerprint(e.expected_key)}). "
            "PLESK_SSH_HOST_KEY bzw. PLESK_SSH_KNOWN_HOSTS pruefen."
        ) from e
    except paramiko.AuthenticationException as e:
        raise SSHError(f"SSH-Anmeldung an {target} als {_SSH_USER} fehlgeschlagen: {e}") from e
    except paramiko.SSHException as e:
        # u.a. RejectPolicy: Host-Key fuer diesen Host/Port nicht hinterlegt
        raise SSHError(f"SSH-Fehler bei {target}: {e}") from e
    except OSError as e:
        raise SSHError(f"SSH-Verbindung zu {target} fehlgeschlagen: {e.strerror or e}") from e
    return client


def _read_channel(
    chan: paramiko.Channel,
    timeout: int,
    max_runtime: int,
    limit: int | None = None,
) -> tuple[bytes, bytes, bool, int]:
    """Liest stdout/stderr eines Kanals chunkweise mit Inaktivitaets-Timeout,
    harter Gesamt-Deadline und Ausgabe-Obergrenze (je Strom). Bei Ueberschreitung
    der Obergrenze wird der Kanal geschlossen (Remote-Prozess bekommt SIGPIPE)
    und truncated=True gemeldet. Rueckgabe: (stdout, stderr, truncated, exit_code);
    exit_code ist -1, wenn keiner gemeldet wurde (z.B. nach dem Abbruch)."""
    import select

    limit = limit or _SSH_MAX_OUTPUT
    out, err = bytearray(), bytearray()
    truncated = False
    start = last = time.monotonic()
    while True:
        now = time.monotonic()
        if now - start > max_runtime:
            chan.close()
            raise SSHError(
                f"Kommando nach {max_runtime} s Gesamtlaufzeit abgebrochen (harte Deadline)."
            )
        if now - last > timeout:
            chan.close()
            raise socket.timeout(f"keine Ausgabe seit {timeout} s")
        got = False
        while chan.recv_ready():
            data = chan.recv(65536)
            if not data:
                break
            got = True
            out += data
            if len(out) > limit:
                truncated = True
                break
        while not truncated and chan.recv_stderr_ready():
            data = chan.recv_stderr(65536)
            if not data:
                break
            got = True
            err += data
            if len(err) > limit:
                truncated = True
                break
        if truncated:
            chan.close()
            break
        if got:
            last = now
            continue
        if chan.eof_received and not chan.recv_ready() and not chan.recv_stderr_ready():
            break
        if chan.closed:
            break
        # Nichts anliegend: auf Daten/EOF warten (max. 0.5 s, dann erneut
        # Deadline und Inaktivitaet pruefen).
        try:
            select.select([chan], [], [], 0.5)
        except (OSError, ValueError):
            time.sleep(0.05)
    exit_code = -1
    if chan.status_event.wait(0 if truncated else min(timeout, 10)):
        exit_code = chan.recv_exit_status()
    return bytes(out[:limit]), bytes(err[:limit]), truncated, exit_code


_TRUNCATED_NOTE = f"\n[Ausgabe gekürzt auf {_SSH_MAX_OUTPUT // (1024 * 1024)} MB - Kommando abgebrochen]"


def _ssh_exec(
    command: str, timeout: int | None = None, max_runtime: int | None = None
) -> tuple[str, str, int]:
    """Führt ein Kommando aus und liefert (stdout, stderr, exit_code) getrennt
    zurück - für Tools, die stdout selbst parsen (z.B. JSON der Imunify-CLI).
    timeout: Inaktivitaets-Timeout, max_runtime: harte Gesamtlaufzeit; die
    Ausgabe ist je Strom auf _SSH_MAX_OUTPUT begrenzt (Hinweis am Ende)."""
    client = _ssh_connect()
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout or _SSH_TIMEOUT)
        raw_out, raw_err, truncated, exit_code = _read_channel(
            stdout.channel, timeout or _SSH_TIMEOUT, max_runtime or _SSH_MAX_RUNTIME
        )
    finally:
        client.close()
    out = raw_out.decode("utf-8", errors="replace")
    err = raw_err.decode("utf-8", errors="replace")
    if truncated:
        out += _TRUNCATED_NOTE
    return out, err, exit_code


def ssh_run(command: str, timeout: int | None = None, max_runtime: int | None = None) -> str:
    """Führt ein Kommando ungeprüft aus (nur für die fest verdrahteten Tools,
    NICHT für Nutzereingaben ohne Whitelist-Check - dafür ssh_run_whitelisted).
    """
    out, err, exit_code = _ssh_exec(command, timeout=timeout, max_runtime=max_runtime)

    result = out
    if err.strip():
        result += f"\n[stderr]\n{err}"
    if exit_code != 0:
        result += f"\n[exit code: {exit_code}]"
    return result.strip() or "(keine Ausgabe)"


def _split_options(
    args: list[str], short_with_arg: set[str] = frozenset(), long_with_arg: set[str] = frozenset()
) -> tuple[list[tuple[str, str]], list[str]]:
    """Zerlegt eine Argumentliste GNU-artig in Optionen und positionale
    Argumente. Optionen kommen als (name, wert) ohne Bindestriche zurueck -
    Kurzoptionen einzeln aus Clustern ("-rn" -> r, n), wobei eine Kurzoption
    mit Argument den Rest des Clusters bzw. das naechste Token als Wert nimmt
    ("-efoo", "-e foo"). Nach "--" ist alles positional."""
    opts: list[tuple[str, str]] = []
    pos: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            pos.extend(args[i + 1:])
            break
        if tok.startswith("--"):
            name, eq, val = tok[2:].partition("=")
            if not eq and name in long_with_arg and i + 1 < len(args):
                i += 1
                val = args[i]
            opts.append((name, val))
        elif tok.startswith("-") and len(tok) > 1:
            letters = tok[1:]
            j = 0
            while j < len(letters):
                ch = letters[j]
                if ch in short_with_arg:
                    val = letters[j + 1:]
                    if not val and i + 1 < len(args):
                        i += 1
                        val = args[i]
                    opts.append((ch, val))
                    break
                opts.append((ch, ""))
                j += 1
        else:
            pos.append(tok)
        i += 1
    return opts, pos


def _reject_options(program: str, opts: list[tuple[str, str]], forbidden: set[str]) -> None:
    for name, _ in opts:
        if name in forbidden or name.lower() in forbidden:
            dash = "-" if len(name) == 1 else "--"
            raise SSHError(
                f"Option '{dash}{name}' ist bei '{program}' nicht erlaubt "
                "(schreibend, endlos oder folgt Symlinks). Dieses Tool ist read-only."
            )


def _check_diag_paths(program: str, paths: list[str]) -> None:
    """find/du/grep -r: mindestens ein Pfad, jeder absolut, ohne
    Shell-Expansionen und (normalisiert) unterhalb von _DIAG_PATH_BASES."""
    if not paths:
        raise SSHError(
            f"'{program}' braucht einen expliziten Pfad unterhalb von "
            f"{', '.join(_DIAG_PATH_BASES)} (kein Scan des ganzen Servers)."
        )
    for p in paths:
        if not p.startswith("/") or _DIAG_PATH_BAD_CHARS & set(p):
            raise SSHError(
                f"Pfad {p!r} ist nicht erlaubt - absoluter Pfad ohne Shell-Expansionen "
                f"($, ~, {{}}) unterhalb von {', '.join(_DIAG_PATH_BASES)} angeben."
            )
        n = posixpath.normpath(p)
        if not any(n == b or n.startswith(b + "/") for b in _DIAG_PATH_BASES):
            raise SSHError(
                f"Pfad {p!r} liegt ausserhalb der erlaubten Basispfade "
                f"({', '.join(_DIAG_PATH_BASES)})."
            )


def _check_plesk(args: list[str]) -> None:
    """plesk version | plesk bin <util> <lese-aktion> [optionen] |
    plesk ext <name> --list|--info [optionen]. Nur eine Aktion, keine
    weiteren Langoptionen, keine der gesperrten Kurzoptionen."""
    if not args:
        raise SSHError("'plesk' erfordert ein Subcommand. Erlaubt (read-only): bin, ext, version")
    sub = args[0].lower()
    if sub in ("version", "--version", "-v"):
        if len(args) > 1:
            raise SSHError("'plesk version' nimmt keine weiteren Argumente.")
        return
    if sub not in ("bin", "ext") or len(args) < 3:
        raise SSHError(
            "Erlaubt sind nur 'plesk version', 'plesk bin <utility> -i|--info|-l|--list|--show-*' "
            "und 'plesk ext <name> --list|--info' (read-only)."
        )
    util = args[1].lower()
    action = args[2]
    if not _PLESK_UTIL_RE.match(util):
        raise SSHError(f"Ungültiger Plesk-Utility-Name {args[1]!r}.")
    action_l = action.lower()
    if sub == "bin":
        ok = (
            action_l in _PLESK_READ_ACTIONS
            or (action_l.startswith("--show-") and action_l not in _PLESK_FORBIDDEN_SHOW)
            or (action_l == "-i" and util in _PLESK_INFO_SHORT_UTILS)
            or (action_l == "-s" and util in _PLESK_SHOW_SHORT_UTILS)
        )
        allowed = "-i/--info (nur bei Domain-/Kunden-Utilities), -l/--list, --show-*"
    else:
        ok = action_l in _PLESK_EXT_READ_ACTIONS
        allowed = "--list, -l, --info"
    if not ok:
        raise SSHError(
            f"Aktion '{action}' ist bei 'plesk {sub} {util}' nicht erlaubt. "
            f"Erlaubt (read-only): {allowed}. Schreibende Plesk-Aktionen laufen nur "
            "über die dafür vorgesehenen Tools (dns_*, php_settings_*, wp_* ...)."
        )
    for tok in args[3:]:
        if not tok.startswith("-"):
            continue
        low = tok.lower()
        if low in _PLESK_FORBIDDEN_SHORT or low.startswith("--"):
            raise SSHError(
                f"Option '{tok}' ist nach der Aktion '{action}' nicht erlaubt - pro Aufruf "
                "genau eine lesende Aktion, Optionen nur als '-name wert'."
            )
        if low.lstrip("-").split("=", 1)[0] in FORBIDDEN_SUBFLAGS:
            raise SSHError(f"Option '{tok}' ist schreibend und nicht erlaubt.")


def _check_segment(seg: list[str]) -> None:
    """Prueft ein einzelnes Pipe-Segment (Token-Liste) read-only-tauglich:
    erlaubtes Programm, erlaubtes Subcommand (Positiv-Liste, wo noetig),
    keine schreibenden Optionsflags, je Utility die Positiv-/Sperrlisten
    (plesk-Aktionen, journalctl/dmesg/tail/du/find/grep-Optionen, Pfade)."""
    if not seg:
        raise SSHError("Leeres Pipe-Segment im Kommando.")
    program = seg[0]
    if program not in ALLOWED_COMMAND_PREFIXES:
        raise SSHError(
            f"Programm '{program}' ist nicht erlaubt. "
            f"Erlaubt sind: {', '.join(sorted(ALLOWED_COMMAND_PREFIXES))}"
        )
    args = seg[1:]

    # 0) Geraete/Pseudodateien, die endlos liefern oder Kernelspeicher ausgeben
    #    (auch als Optionswert wie --file=/dev/zero; normalisiert gegen /dev/./zero).
    for tok in args:
        n = posixpath.normpath(tok)
        if any(f in n for f in _DIAG_FORBIDDEN_PATHS):
            raise SSHError(f"Pfad '{tok}' ist nicht erlaubt (Gerät/Pseudodatei).")

    # 1) Erstes echtes Subcommand (erstes Argument, das nicht mit '-' beginnt).
    first_sub = next((a for a in args if not a.startswith("-")), None)

    forbidden_subs = FORBIDDEN_SUBCOMMANDS.get(program)
    if forbidden_subs and first_sub and first_sub.lower() in forbidden_subs:
        raise SSHError(
            f"Subcommand '{program} {first_sub}' ist nicht erlaubt "
            "(schreibend/interaktiv). Dieses Tool ist read-only."
        )

    safe_subs = SAFE_SUBCOMMANDS.get(program)
    if safe_subs is not None:
        if first_sub is None:
            raise SSHError(
                f"'{program}' erfordert ein Subcommand. Erlaubt (read-only): "
                f"{', '.join(sorted(safe_subs))}"
            )
        if first_sub.lower() not in safe_subs:
            raise SSHError(
                f"Subcommand '{program} {first_sub}' ist nicht erlaubt. "
                f"Erlaubt (read-only): {', '.join(sorted(safe_subs))}"
            )

    # 2) Schreibende Optionsflags (Langoptionen wie --install-url, --create).
    for tok in args:
        if not tok.startswith("-"):
            continue
        flag = tok.lstrip("-").split("=", 1)[0].lower()
        if flag in FORBIDDEN_SUBFLAGS:
            raise SSHError(
                f"Optionsflag '{tok}' ist schreibend und nicht erlaubt. "
                "Dieses Tool ist read-only für Diagnosezwecke."
            )

    # 3) Positiv-/Sperrlisten je Utility.
    if program == "plesk":
        _check_plesk(args)
    elif program == "hostname":
        if args:
            raise SSHError("'hostname' ist nur ohne Argumente erlaubt (setzt sonst den Hostnamen).")
    elif program == "journalctl":
        opts, _ = _split_options(args, _JOURNALCTL_SHORT_WITH_ARG)
        _reject_options(program, opts, _JOURNALCTL_FORBIDDEN)
    elif program == "dmesg":
        opts, _ = _split_options(args, _DMESG_SHORT_WITH_ARG)
        _reject_options(program, opts, _DMESG_FORBIDDEN)
    elif program == "tail":
        opts, _ = _split_options(args, _TAIL_SHORT_WITH_ARG)
        _reject_options(program, opts, _TAIL_FORBIDDEN)
    elif program == "du":
        opts, paths = _split_options(args, _DU_SHORT_WITH_ARG, _DU_LONG_WITH_ARG)
        _reject_options(program, opts, _DU_FORBIDDEN)
        _check_diag_paths(program, paths)
    elif program == "find":
        # find [-H|-L|-P] [-D opts] [-O level] <pfad...> [ausdruck]: Startpunkte
        # sind die Tokens vor dem ersten Ausdrucks-Token (-name, "(", "!" ...).
        rest = list(args)
        while rest and (rest[0] in ("-P", "-D") or rest[0].startswith("-O")):
            rest = rest[2:] if rest[0] == "-D" else rest[1:]
        paths: list[str] = []
        while rest and not rest[0].startswith(("-", "(", "!", ",")):
            paths.append(rest.pop(0))
        for tok in args:
            name = tok.lstrip("-")
            if tok.startswith("-") and (name in _FIND_FORBIDDEN or name.lower() in FORBIDDEN_FIND_PRIMARIES):
                raise SSHError(
                    f"find-Option '{tok}' ist nicht erlaubt "
                    "(führt Kommandos aus, schreibt Dateien oder folgt Symlinks)."
                )
        _check_diag_paths(program, paths)
    elif program in ("grep", "zgrep"):
        opts, pos = _split_options(args, _GREP_SHORT_WITH_ARG, _GREP_LONG_WITH_ARG)
        _reject_options(program, opts, _GREP_FORBIDDEN)
        if any(name in _GREP_RECURSIVE for name, _ in opts):
            # Ohne -e/-f ist das erste positionale Argument das Muster.
            if not any(name in ("e", "f", "regexp", "file") for name, _ in opts):
                pos = pos[1:]
            _check_diag_paths(f"{program} -r", pos)


def ssh_run_whitelisted(command: str, timeout: int | None = None) -> str:
    """Führt ein Kommando aus, das direkt von Claude/dem Nutzer kommt. Prüft:
    1. Keine Shell-Chaining-Konstrukte (;, &&, ||, `, $(, >, <, &, Newline)
    2. Keine verbotenen Wörter (restart/stop/kill/rm/... als eigenständiges Wort)
    3. Jedes Pipe-Segment: erlaubtes Programm, erlaubtes Subcommand
       (Positiv-Liste für plesk/systemctl), keine schreibenden Flags, je
       Utility die Positiv-/Sperrlisten (siehe _check_segment).
    Das Kommando läuft auf dem Server unter "timeout <DIAG_TIMEOUT>" und mit
    harter Deadline; die Ausgabe ist auf _SSH_MAX_OUTPUT begrenzt.
    """
    stripped = command.strip()
    if not stripped:
        raise SSHError("Leeres Kommando.")

    # Zusaetzlich alle uebrigen Steuerzeichen ablehnen (nur normale Leerzeichen
    # und druckbare Zeichen erlaubt) - schuetzt vor Injection ueber exotische
    # Whitespace-/Steuerzeichen, die eine Remote-Shell interpretieren koennte.
    if any(ord(c) < 32 and c != " " for c in stripped):
        raise SSHError("Kommando enthält unerlaubte Steuerzeichen.")

    for construct in FORBIDDEN_SHELL_CONSTRUCTS:
        if construct in stripped:
            label = construct.encode("unicode_escape").decode("ascii")
            raise SSHError(
                f"Kommando enthält verbotenes Shell-Konstrukt '{label}'. "
                "Command-Chaining/Umleitung ist nicht erlaubt."
            )

    match = _FORBIDDEN_WORD_RE.search(stripped)
    if match:
        raise SSHError(
            f"Kommando enthält verbotenes Wort '{match.group(1)}'. "
            "Dieses Tool ist read-only für Diagnosezwecke."
        )

    try:
        # punctuation_chars="|": ein unquotiertes "|" wird auch ohne Leerzeichen
        # ("cat x|plesk ...") zum eigenen Token, ein quotiertes ('err|warn')
        # bleibt Teil des Musters - sonst wuerde das zweite Programm der Pipe
        # nicht als eigenes Segment geprueft.
        lex = shlex.shlex(stripped, posix=True, punctuation_chars="|")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError as e:
        raise SSHError(f"Kommando konnte nicht geparst werden: {e}")
    if not tokens:
        raise SSHError("Leeres Kommando.")

    # Whitelist pro Pipe-Segment prüfen (falls mehrere Programme verkettet sind)
    segment: list[str] = []
    segments: list[list[str]] = [segment]
    for tok in tokens:
        if tok == "|":
            segment = []
            segments.append(segment)
        else:
            segment.append(tok)

    for seg in segments:
        _check_segment(seg)

    # Gesamte Pipeline unter "timeout" (eigene Prozessgruppe, SIGTERM an alle
    # Kinder, nach 5 s SIGKILL) - dazu auf unserer Seite Inaktivitaets-Timeout
    # und harte Deadline etwas darueber als Backstop.
    limit = timeout or _DIAG_TIMEOUT
    remote = f"timeout -k 5 {int(limit)} sh -c {shlex.quote(stripped)}"
    return ssh_run(remote, timeout=limit + 10, max_runtime=limit + 20)


def _domain_arg(domain: str) -> str:
    """Validiert grob, dass domain wie ein Domainname aussieht, um
    Shell-Injection über die vorformulierten Kommandos zu vermeiden.
    """
    d = domain.strip()
    if not d or any(c in d for c in " ;|&`$(){}<>\"'\n\t"):
        raise ValueError(f"Ungültiger Domainname: {domain!r}")
    return d


_VHOST_BASE = "/var/www/vhosts"
_BACKUP_SUFFIX_RE = re.compile(r"\.bak-\d{14}$")


def _vhost_path(domain: str, path: str) -> str:
    """Löst domain+relativen Pfad zu einem absoluten Pfad auf, der zwingend
    innerhalb von /var/www/vhosts/<domain>/ liegen muss. Verhindert
    Path-Traversal (z.B. "../../../etc/passwd") über os.path.normpath +
    Prefix-Check.
    """
    d = _domain_arg(domain)
    base = f"{_VHOST_BASE}/{d}"
    rel = path.strip().lstrip("/")
    if not rel:
        raise ValueError("path darf nicht leer sein.")
    full = os.path.normpath(f"{base}/{rel}")
    if full != base and not full.startswith(base + "/"):
        raise ValueError(
            f"Pfad {path!r} verlässt das Vhost-Verzeichnis von '{d}' - nicht erlaubt."
        )
    return full


def _assert_real_within_vhost(sftp, full: str, domain: str) -> None:
    """Stellt sicher, dass der kanonische Pfad (alle Symlinks in
    Elternverzeichnissen aufgeloest) innerhalb von /var/www/vhosts/<domain>/
    bleibt. _vhost_path prueft nur den Pfad-String; ein symlinktes
    Zwischenverzeichnis (z.B. httpdocs/x -> /etc) wuerde sonst als root aus
    dem Vhost-Verzeichnis herausfuehren."""
    d = _domain_arg(domain)
    base = f"{_VHOST_BASE}/{d}"
    try:
        base_real = sftp.normalize(base)
    except IOError as e:
        raise ValueError(f"Vhost-Verzeichnis von '{d}' nicht auffindbar: {e}")

    # Tiefsten bereits existierenden Vorfahren von full kanonisieren; der
    # (noch) nicht existierende Rest kann keine Symlinks enthalten.
    probe = full
    tail = ""
    while True:
        try:
            probe_real = sftp.normalize(probe)
            break
        except IOError:
            parent, _, name = probe.rpartition("/")
            if not parent or parent == probe:
                raise ValueError(f"Pfad {full!r} nicht aufloesbar.")
            tail = "/" + name + tail
            probe = parent

    real_full = probe_real + tail
    if real_full != base_real and not real_full.startswith(base_real + "/"):
        raise ValueError(
            f"Pfad {full!r} zeigt (ueber einen Symlink) auf {real_full!r} "
            f"ausserhalb des Vhost-Verzeichnisses von '{d}' - nicht erlaubt."
        )


def _client_exec(
    client: paramiko.SSHClient,
    command: str,
    timeout: int | None = None,
    stdin_data: bytes | None = None,
    max_runtime: int | None = None,
) -> tuple[str, str, int]:
    """Wie _ssh_exec, aber auf einer bereits offenen SSH-Verbindung."""
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout or _SSH_TIMEOUT)
    if stdin_data is not None:
        stdin.write(stdin_data)
    stdin.channel.shutdown_write()
    raw_out, raw_err, truncated, exit_code = _read_channel(
        stdout.channel, timeout or _SSH_TIMEOUT, max_runtime or _SSH_MAX_RUNTIME
    )
    out = raw_out.decode("utf-8", errors="replace")
    if truncated:
        out += _TRUNCATED_NOTE
    return out, raw_err.decode("utf-8", errors="replace"), exit_code


# ---------------------------------------------------------------------------
# Plesk-REST-API (X-API-Key) - für strukturierte Plesk-eigene Daten
# ---------------------------------------------------------------------------

import httpx  # noqa: E402  (bewusst nach den Konstanten, analog zu bexio-mcp)

# httpx loggt jeden Request inkl. voller URL auf INFO - nur Warnungen/Fehler
# durchlassen, damit keine Query-Parameter im Container-Log landen
logging.getLogger("httpx").setLevel(logging.WARNING)

_API_HOST = os.environ.get("PLESK_API_HOST", _SSH_HOST)
_API_PORT = int(os.environ.get("PLESK_API_PORT", "8443"))
_API_KEY = os.environ.get("PLESK_API_KEY", "")
_API_VERIFY_SSL = os.environ.get("PLESK_API_VERIFY_SSL", "true").strip().lower() not in (
    "false", "0", "no",
)
_API_TIMEOUT = int(os.environ.get("PLESK_API_TIMEOUT", "20"))
# Pfad unterhalb von /api/v2/: nur [A-Za-z0-9_/-], kein "..", "?" oder "#" -
# httpx wuerde ".."-Segmente normalisieren und so Requests ausserhalb von
# /api/v2/ auf das Plesk-Panel erlauben.
_API_PATH_RE = re.compile(r"^[A-Za-z0-9_/-]{1,512}$")


def plesk_api_get_raw(path: str, params: dict[str, str] | None = None) -> str:
    """GET-Request gegen die Plesk-REST-API (https://<host>:8443/api/v2/<path>).
    Nur GET - schreibende Requests sind über dieses Tool bewusst nicht möglich.
    """
    if not _API_HOST:
        raise RuntimeError(
            "PLESK_API_HOST/PLESK_SSH_HOST ist nicht gesetzt (Umgebungsvariable fehlt)."
        )
    if not _API_KEY:
        raise RuntimeError("PLESK_API_KEY ist nicht gesetzt (Umgebungsvariable fehlt).")

    clean_path = path.strip().strip("/")
    if not clean_path or not _API_PATH_RE.match(clean_path) or ".." in clean_path.split("/"):
        raise ValueError(
            f"Ungültiger API-Pfad {path!r}: erlaubt sind nur Buchstaben, Ziffern, '_', '-' und '/' "
            "(z.B. 'domains' oder 'domains/1/status'), kein '..', '?' oder '#' - "
            "Query-Parameter über den Parameter query angeben."
        )
    url = f"https://{_API_HOST}:{_API_PORT}/api/v2/{clean_path}"
    resp = httpx.get(
        url,
        params=params or {},
        headers={"X-API-Key": _API_KEY, "Accept": "application/json"},
        timeout=_API_TIMEOUT,
        verify=_API_VERIFY_SSL,
    )
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------------
# MySQL/MariaDB-Datenbanken - read-only, via Plesk-internen Admin-DB-Zugang
# ---------------------------------------------------------------------------
#
# Plesk legt den Login und das Klartext-Passwort seines eigenen MySQL/MariaDB-
# Administrator-Accounts (Login "admin") in /etc/psa/.psa.shadow ab - damit
# verwaltet Plesk selbst alle Kunden-Datenbanken (z.B. für phpMyAdmin-
# Single-Sign-on, Backups, Migrationen). Da diese Tools ohnehin per SSH als
# root laufen (also die Datei bereits lesen könnten), wird dieser Account
# genutzt, um automatisch - ohne eigene, zusätzlich zu konfigurierende
# Datenbank-Zugangsdaten - auf beliebige Datenbanken auf dem Server zugreifen
# zu können. Das ist technisch ein sehr mächtiger Zugang (faktisch DB-root);
# die Beschränkung auf read-only passiert daher ausschliesslich in diesem
# Code (siehe _validate_select_sql) und nicht durch MySQL-Rechte selbst.

_DB_SHADOW_PATH = "/etc/psa/.psa.shadow"
_DB_ADMIN_USER = "admin"

# Schlüsselwörter, die in einer db_query/db_search-Query verboten sind - auch
# wenn das Statement mit SELECT/SHOW/EXPLAIN/DESCRIBE beginnt. Als ganzes Wort
# geprüft (Regex \b...\b), damit z.B. eine Spalte "start_date" nicht wegen
# "start" fälschlich blockiert wird (bekannte Einschränkung: eine Spalte, die
# GENAU "start" heisst, würde blockiert - siehe README).
_SQL_FORBIDDEN_WORDS = {
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "grant", "revoke", "rename", "replace", "call", "exec", "execute",
    "lock", "unlock", "commit", "rollback", "set", "load_file", "outfile",
    "dumpfile", "sleep", "benchmark", "start",
}
_SQL_FORBIDDEN_WORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _SQL_FORBIDDEN_WORDS) + r")\b", re.IGNORECASE
)

_DB_SYSTEM_SCHEMAS = {
    "information_schema", "mysql", "performance_schema", "sys",
    "psa", "phpmyadmin", "roundcube", "horde",
}


def _quote_ident(name: str) -> str:
    """MySQL-Identifier-Escaping (Tabellen-/Spaltennamen) - verdoppelt
    Backticks im Namen und umschliesst ihn mit Backticks."""
    return "`" + name.replace("`", "``") + "`"


def _sql_quote(value: str) -> str:
    """Escaped einen String-Wert für die Verwendung als SQL-Literal (einfache
    Anführungszeichen)."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _validate_select_sql(sql: str) -> str:
    """Stellt sicher, dass sql ein einzelnes, reines Lese-Statement ist:
    genau ein Statement (kein ';' zum Verketten, ein optionales
    Trailing-';' ist erlaubt), beginnt mit SELECT/SHOW/EXPLAIN/DESCRIBE/DESC,
    und enthält kein Wort aus _SQL_FORBIDDEN_WORDS (insert/update/delete/...,
    load_file, into outfile/dumpfile, sleep/benchmark als DoS-Schutz, etc.)."""
    s = sql.strip()
    if not s:
        raise ValueError("Leere Query.")
    # Backslashes sind in Lese-Queries nicht noetig und ermoeglichen sonst
    # Client-Kommandos des mysql-CLI (\!, \., \T ...). Sie werden zwar durch
    # --binary-mode in _mysql_exec neutralisiert, hier aber zusaetzlich
    # abgelehnt (Defense-in-Depth, verhindert Shell-/Datei-Zugriff wie
    # "SELECT 1 \\! id").
    if "\\" in s:
        raise ValueError(
            "Backslash ('\\') ist in Queries nicht erlaubt (verhindert "
            "mysql-Client-Kommandos wie \\! oder \\.)."
        )
    body = s[:-1].strip() if s.endswith(";") else s
    if ";" in body:
        raise ValueError(
            "Mehrere Statements ('; ...') sind nicht erlaubt - nur eine "
            "einzelne Lese-Query pro Aufruf."
        )
    if not body:
        raise ValueError("Leere Query.")
    first_word = body.split(None, 1)[0].lower()
    if first_word not in ("select", "show", "explain", "describe", "desc"):
        raise ValueError(
            f"Nur SELECT/SHOW/EXPLAIN/DESCRIBE-Queries sind erlaubt, nicht "
            f"'{first_word}'. Dieses Tool ist strikt read-only."
        )
    match = _SQL_FORBIDDEN_WORD_RE.search(body)
    if match:
        raise ValueError(
            f"Query enthält verbotenes Schlüsselwort '{match.group(1)}'. "
            "Dieses Tool ist strikt read-only (siehe README für die volle Blacklist)."
        )
    return body


def _validate_db_name(database: str) -> str:
    db = database.strip()
    if not db or any(c in db for c in " ;|&`$(){}<>\"'\n\t"):
        raise ValueError(f"Ungültiger Datenbankname: {database!r}")
    return db


def _mysql_exec(
    sql: str,
    database: str = "",
    timeout: int | None = None,
    skip_column_names: bool = False,
) -> str:
    """Führt sql via `mysql`-CLI als Plesk-Admin-User aus. Das Passwort wird
    erst auf dem Server aus /etc/psa/.psa.shadow in die Umgebungsvariable
    MYSQL_PWD des mysql-Prozesses gelesen ("$(cat ...)" im Prefix-Assignment):
    Es steht damit weder in der Kommandozeile der Remote-Shell
    (/proc/<pid>/cmdline, fuer alle lokalen User lesbar) noch im mysql-argv,
    und es verlaesst den Server nie. /proc/<pid>/environ ist nur fuer root
    lesbar."""
    db_part = f" {shlex.quote(database)}" if database else ""
    # --binary-mode: deaktiviert die mysql-CLI-eigenen Kommandos (\!, \., system,
    # source, tee ...) und behandelt Backslashes als literal. Ohne dieses Flag
    # fuehrt der Client z.B. "SELECT 1 \! id" als Shell-Kommando aus (RCE als
    # root), obwohl die SQL-Blacklist das Statement fuer harmlos haelt.
    flags = "--connect-timeout=10 --batch --raw --binary-mode"
    if skip_column_names:
        flags += " -N"
    cmd = (
        f'MYSQL_PWD="$(cat {shlex.quote(_DB_SHADOW_PATH)})" mysql -u{_DB_ADMIN_USER} {flags}'
        f"{db_part} -e {shlex.quote(sql)}"
    )
    out = ssh_run(cmd, timeout=timeout)
    if "Access denied" in out or _DB_SHADOW_PATH in out:
        raise RuntimeError(
            f"MySQL-Anmeldung als '{_DB_ADMIN_USER}' fehlgeschlagen - ist {_DB_SHADOW_PATH} "
            "vorhanden und ist dies ein Plesk-Server mit lokaler MySQL/MariaDB-Installation?"
        )
    return out


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def get_version() -> str:
    """Version des laufenden Plesk-MCP-Servers abfragen (Redeploy-Kontrolle)."""
    return json.dumps({"name": "plesk-mcp", "version": __version__, "commit": os.environ.get("GIT_SHA", "unbekannt")})


@mcp.tool()
def domain_info(domain: str) -> str:
    """Zeigt allgemeine Plesk-Domain-Infos: Status, Disk-Space (Limit/Nutzung),
    Traffic, Hosting-Typ, IP, SSL-Zertifikat, Subscription/Service-Plan.
    Entspricht `plesk bin domain -i <domain>`.
    """
    d = _domain_arg(domain)
    return ssh_run(f"plesk bin domain -i {shlex.quote(d)}")


@mcp.tool()
def dns_records(domain: str) -> str:
    """Zeigt die DNS-Resource-Records der Domain-Zone.
    Entspricht `plesk bin dns --info <domain>`.
    """
    d = _domain_arg(domain)
    return ssh_run(f"plesk bin dns --info {shlex.quote(d)}")


def _dns_build_cmd(
    action: str,
    domain: str,
    record_type: str,
    subdomain: str,
    value: str,
    priority: str,
    srv_service: str,
    srv_protocol: str,
    srv_port: str,
    srv_weight: str,
) -> str:
    """Baut das `plesk bin dns --add/--del`-Kommando für einen Record.
    Gemeinsame Logik für dns_add_record/dns_delete_record - Plesk erwartet
    beim Löschen exakt dieselben Parameter wie beim Anlegen (zur eindeutigen
    Identifikation des Records), daher identischer Aufbau für beide Aktionen.
    """
    d = _domain_arg(domain)
    rt = record_type.strip().lower()
    sub = subdomain.strip()
    if sub == "@":
        sub = ""
    val = value.strip()

    flag = "--add" if action == "add" else "--del"
    cmd = f"plesk bin dns {flag} {shlex.quote(d)}"

    if rt == "a":
        if not val:
            raise ValueError("value (IP-Adresse) ist für A-Records erforderlich.")
        cmd += f" -a {shlex.quote(sub)} -ip {shlex.quote(val)}"
    elif rt == "aaaa":
        if not val:
            raise ValueError("value (IPv6-Adresse) ist für AAAA-Records erforderlich.")
        cmd += f" -aaaa {shlex.quote(sub)} -ip {shlex.quote(val)}"
    elif rt == "cname":
        if not val:
            raise ValueError("value (Zieldomain) ist für CNAME-Records erforderlich.")
        cmd += f" -cname {shlex.quote(sub)} -canonical {shlex.quote(val)}"
    elif rt == "mx":
        if not val:
            raise ValueError("value (Mailserver) ist für MX-Records erforderlich.")
        if not priority.strip():
            raise ValueError("priority ist für MX-Records erforderlich.")
        cmd += (
            f" -mx {shlex.quote(sub)} -mailexchanger {shlex.quote(val)}"
            f" -priority {shlex.quote(priority.strip())}"
        )
    elif rt == "ns":
        if not val:
            raise ValueError("value (Nameserver) ist für NS-Records erforderlich.")
        cmd += f" -ns {shlex.quote(sub)} -nameserver {shlex.quote(val)}"
    elif rt == "txt":
        if not val:
            raise ValueError("value (Text-Inhalt) ist für TXT-Records erforderlich.")
        cmd += f" -txt {shlex.quote(val)} -domain {shlex.quote(sub)}"
    elif rt == "srv":
        missing = [
            name
            for name, v in (
                ("value (Ziel-Host)", val),
                ("priority", priority.strip()),
                ("srv_service", srv_service.strip()),
                ("srv_protocol", srv_protocol.strip()),
                ("srv_port", srv_port.strip()),
                ("srv_weight", srv_weight.strip()),
            )
            if not v
        ]
        if missing:
            raise ValueError(
                "Für SRV-Records sind alle folgenden Parameter erforderlich, "
                f"es fehlen: {', '.join(missing)}."
            )
        cmd += (
            f" -srv {shlex.quote(sub)} -srv-service {shlex.quote(srv_service.strip())}"
            f" -srv-target-host {shlex.quote(val)}"
            f" -srv-protocol {shlex.quote(srv_protocol.strip())}"
            f" -srv-port {shlex.quote(srv_port.strip())}"
            f" -srv-priority {shlex.quote(priority.strip())}"
            f" -srv-weight {shlex.quote(srv_weight.strip())}"
        )
    else:
        raise ValueError(
            f"Unbekannter record_type {record_type!r}. "
            "Unterstützt: a, aaaa, cname, mx, ns, txt, srv."
        )
    return cmd


@mcp.tool()
def dns_add_record(
    domain: str,
    record_type: str,
    subdomain: str = "",
    value: str = "",
    priority: str = "",
    srv_service: str = "",
    srv_protocol: str = "",
    srv_port: str = "",
    srv_weight: str = "",
    confirm: bool = False,
) -> str:
    """Fügt einen DNS-Resource-Record zur Zone einer Domain hinzu.
    Entspricht `plesk bin dns --add <domain> -<typ> ...`.

    ACHTUNG - schreibt auf einem Produktivserver (ändert live auflösbare
    DNS-Einträge): erfordert confirm=true als bewusste Bestätigung PRO
    Aufruf (keine globale Freischaltung), analog zu write_vhost_file.
    Existiert exakt derselbe Record bereits, meldet Plesk selbst einen
    Fehler zurück (kein stillschweigendes Duplizieren/Überschreiben) - zum
    Ändern eines bestehenden Records daher erst dns_delete_record, dann
    dns_add_record mit dem neuen Wert.

    record_type (Gross-/Kleinschreibung egal): a | aaaa | cname | mx | ns | txt | srv

    subdomain: Hostname-Teil relativ zur Zone, z.B. "www" oder "mail3".
               Leer ("") oder "@" steht für die Zone/Domain-Root selbst.
    value: Zielwert je nach Typ - a/aaaa: IP-Adresse; cname: kanonischer
           Name; mx: Mailserver; ns: Nameserver; txt: Text-Inhalt;
           srv: Ziel-Host (srv-target-host).
    priority: nur für mx (0-50) und srv (srv-priority) erforderlich.
    srv_service/srv_protocol/srv_port/srv_weight: nur für record_type="srv"
        erforderlich, zusätzlich zu subdomain, value und priority - z.B. für
        einen SIP-SRV-Record: subdomain="", value="sipserver.example.com",
        srv_service="sip", srv_protocol="tcp", srv_port="5060",
        srv_weight="5", priority="0".
    """
    if not confirm:
        raise ValueError(
            "confirm=true erforderlich - dieses Tool schreibt auf einem "
            "Produktivserver (DNS-Zone) und braucht eine explizite "
            "Bestätigung pro Aufruf."
        )
    cmd = _dns_build_cmd(
        "add", domain, record_type, subdomain, value, priority,
        srv_service, srv_protocol, srv_port, srv_weight,
    )
    return ssh_run(cmd)


@mcp.tool()
def dns_delete_record(
    domain: str,
    record_type: str,
    subdomain: str = "",
    value: str = "",
    priority: str = "",
    srv_service: str = "",
    srv_protocol: str = "",
    srv_port: str = "",
    srv_weight: str = "",
    confirm: bool = False,
) -> str:
    """Entfernt einen DNS-Resource-Record aus der Zone einer Domain.
    Entspricht `plesk bin dns --del <domain> -<typ> ...` - Plesk erfordert
    dieselben Parameter wie beim Anlegen (dns_add_record), um den zu
    löschenden Record eindeutig zu identifizieren; vorher am besten
    dns_records aufrufen, um die exakten aktuellen Werte zu bestätigen.

    ACHTUNG - schreibt auf einem Produktivserver (entfernt live auflösbare
    DNS-Einträge): erfordert confirm=true als bewusste Bestätigung PRO
    Aufruf (keine globale Freischaltung).

    Parameter identisch zu dns_add_record (record_type, subdomain, value,
    priority, srv_*) - siehe dort für die Bedeutung je Record-Typ.
    """
    if not confirm:
        raise ValueError(
            "confirm=true erforderlich - dieses Tool schreibt auf einem "
            "Produktivserver (DNS-Zone) und braucht eine explizite "
            "Bestätigung pro Aufruf."
        )
    cmd = _dns_build_cmd(
        "del", domain, record_type, subdomain, value, priority,
        srv_service, srv_protocol, srv_port, srv_weight,
    )
    return ssh_run(cmd)


@mcp.tool()
def dns_update_record(
    domain: str,
    record_type: str,
    old_subdomain: str,
    old_value: str,
    new_subdomain: str = "",
    new_value: str = "",
    old_priority: str = "",
    new_priority: str = "",
    old_srv_service: str = "",
    old_srv_protocol: str = "",
    old_srv_port: str = "",
    old_srv_weight: str = "",
    new_srv_service: str = "",
    new_srv_protocol: str = "",
    new_srv_port: str = "",
    new_srv_weight: str = "",
    confirm: bool = False,
) -> str:
    """Ändert einen bestehenden DNS-Record. Plesk kennt kein natives
    'Update' für einzelne Records (nur `--set` für die komplette Zone auf
    einmal) - dieses Tool bildet die Änderung als zusammenhängenden
    Delete+Add ab (`plesk bin dns --del` mit den old_*-Werten, direkt
    gefolgt von `--add` mit den new_*-Werten), damit dafür nur ein
    confirm=true-Aufruf nötig ist statt zwei einzelne (dns_delete_record +
    dns_add_record).

    old_subdomain/old_value/old_priority/old_srv_*: identifizieren den zu
        ändernden Record - müssen exakt den aktuellen Werten entsprechen
        (vorher am besten dns_records aufrufen, um sie zu bestätigen).
    new_subdomain/new_value/new_priority/new_srv_*: die neuen Werte. Wird
        einer davon leer gelassen, bleibt er wie im alten Record - z.B. nur
        die IP eines A-Records ändern: record_type="a", old_subdomain="www",
        old_value="<alte IP>", new_value="<neue IP>" (new_subdomain kann
        weggelassen werden, bleibt dann "www").
    record_type (Gross-/Kleinschreibung egal): a | aaaa | cname | mx | ns | txt | srv
        - gilt für den Record vor UND nach der Änderung; um einen Record in
        einen anderen Typ umzuwandeln, stattdessen dns_delete_record +
        dns_add_record einzeln verwenden.

    ACHTUNG - schreibt auf einem Produktivserver (ändert live auflösbare
    DNS-Einträge): erfordert confirm=true als bewusste Bestätigung PRO
    Aufruf (keine globale Freischaltung). Falls das nachträgliche --add
    fehlschlägt (z.B. Tippfehler im neuen Wert), ist der alte Record
    bereits gelöscht - die vollständige Fehlermeldung wird zurückgegeben,
    damit der alte Record bei Bedarf manuell per dns_add_record wieder
    angelegt werden kann. Scheitert bereits das --del (z.B. weil
    old_subdomain/old_value nicht exakt zum aktuellen Record passen), wird
    abgebrochen, BEVOR irgendetwas geändert wird.
    """
    if not confirm:
        raise ValueError(
            "confirm=true erforderlich - dieses Tool schreibt auf einem "
            "Produktivserver (DNS-Zone) und braucht eine explizite "
            "Bestätigung pro Aufruf."
        )

    eff_new_subdomain = new_subdomain.strip() or old_subdomain
    eff_new_value = new_value.strip() or old_value
    eff_new_priority = new_priority.strip() or old_priority
    eff_new_srv_service = new_srv_service.strip() or old_srv_service
    eff_new_srv_protocol = new_srv_protocol.strip() or old_srv_protocol
    eff_new_srv_port = new_srv_port.strip() or old_srv_port
    eff_new_srv_weight = new_srv_weight.strip() or old_srv_weight

    del_cmd = _dns_build_cmd(
        "del", domain, record_type, old_subdomain, old_value, old_priority,
        old_srv_service, old_srv_protocol, old_srv_port, old_srv_weight,
    )
    del_result = ssh_run(del_cmd)
    if "[exit code:" in del_result:
        return (
            "Abgebrochen - alter Record konnte nicht gelöscht werden, es "
            "wurde nichts geändert. Prüfe old_subdomain/old_value/"
            "old_priority (am besten zuerst dns_records aufrufen).\n\n"
            f"Ausgabe von --del:\n{del_result}"
        )

    add_cmd = _dns_build_cmd(
        "add", domain, record_type, eff_new_subdomain, eff_new_value, eff_new_priority,
        eff_new_srv_service, eff_new_srv_protocol, eff_new_srv_port, eff_new_srv_weight,
    )
    add_result = ssh_run(add_cmd)

    return (
        f"Alter Record gelöscht:\n{del_result}\n\n"
        f"Neuer Record angelegt:\n{add_result}"
    )


@mcp.tool()
def backup_list(domain: str = "") -> str:
    """Listet vorhandene lokale Backup-Dateien auf (Server- und
    Domain-/Subscription-Backups unter /var/lib/psa/dumps, dem Plesk-
    Standardpfad für lokale Backups). Ohne domain wird das gesamte
    Backup-Verzeichnis durchsucht, mit domain nur Treffer für diese Domain.
    Zeigt Dateiname, Grösse und Änderungsdatum - keine Backups werden erstellt
    oder gelöscht.
    """
    base = "/var/lib/psa/dumps"
    # ".discovered"/".run" sind interne Plesk-Metadaten-Verzeichnisse (ein
    # Eintrag pro Backup-Lauf, keine eigentlichen Backup-Dateien) - werden
    # ausgeblendet (-prune), sonst ist die Ausgabe bei vielen Domains
    # unlesbar lang.
    prune = r"\( -name '.discovered' -o -name '.run' \) -prune -o"
    if domain:
        d = _domain_arg(domain)
        cmd = f"find {base} {prune} -iname '*{d}*' -exec ls -lhd {{}} \\; 2>&1 | head -n 200"
    else:
        cmd = f"find {base} {prune} -maxdepth 4 -type d -print 2>&1 | head -n 200"
    out = ssh_run(cmd)
    if not out or out == "(keine Ausgabe)":
        return f"Keine Backups gefunden (Pfad {base})."
    return out


@mcp.tool()
def wp_toolkit_list(with_plugins: bool = False) -> str:
    """Listet alle vom WP Toolkit verwalteten WordPress-Installationen auf.
    Entspricht `plesk ext wp-toolkit --list` (optional mit -plugins für
    Plugin-Infos je Installation).
    """
    cmd = "plesk ext wp-toolkit --list"
    if with_plugins:
        cmd += " -plugins"
    return ssh_run(cmd)


@mcp.tool()
def wp_toolkit_info(instance_id: str) -> str:
    """Zeigt Detail-Infos zu einer einzelnen WordPress-Installation (Version,
    Pfad, Updates, Plugins/Themes). instance_id kommt aus wp_toolkit_list.
    Entspricht `plesk ext wp-toolkit --info -instance-id <id>`.
    """
    iid = instance_id.strip()
    if not iid or not iid.isdigit():
        raise ValueError(f"Ungültige instance_id: {instance_id!r} (muss numerisch sein).")
    return ssh_run(f"plesk ext wp-toolkit --info -instance-id {iid}")


@mcp.tool()
def plesk_api_get(path: str, query: str = "") -> str:
    """Read-only GET-Request gegen die Plesk-REST-API für strukturierte
    JSON-Daten (Domains, Subscriptions, Kunden etc.), als Ergänzung zu den
    SSH/CLI-Tools. Beispiel: path="domains" für GET /api/v2/domains.
    query: optionale Querystring-Parameter im Format "key1=val1&key2=val2".
    Erfordert PLESK_API_KEY (siehe README). Nur GET - keine schreibenden
    Requests möglich.
    """
    params: dict[str, str] = {}
    for pair in query.split("&"):
        if not pair or "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        params[k.strip()] = v.strip()
    return plesk_api_get_raw(path, params)


_LOGIN_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


@mcp.tool()
def customer_update_email(login: str, email: str, confirm: bool = False) -> str:
    """Setzt die Kontakt-E-Mail-Adresse eines Plesk-Kundenaccounts (nicht
    des Admins). Entspricht `plesk bin customer --update <login> -email <mail>`.
    Den Login liefert plesk_api_get("clients") (Feld "login").

    ACHTUNG - schreibt auf einem Produktivserver (Kundenstammdaten, an diese
    Adresse gehen Plesk-Benachrichtigungen): erfordert confirm=true als
    bewusste Bestätigung PRO Aufruf. Ohne confirm wird nur eine Vorschau
    (aktuelle vs. neue Adresse) zurückgegeben.
    """
    lg = login.strip()
    mail = email.strip()
    if not _LOGIN_RE.match(lg):
        raise ValueError(f"Ungültiger Plesk-Login: {login!r}")
    if lg == "admin":
        raise ValueError("Der Admin-Account wird über dieses Tool nicht geändert.")
    if not _EMAIL_RE.match(mail):
        raise ValueError(f"Ungültige E-Mail-Adresse: {email!r}")

    current = ssh_run(
        f"plesk bin customer --info {shlex.quote(lg)} | grep -i '^Email'"
    )
    if not confirm:
        return (
            f"Vorschau (nichts geändert) - Login {lg}: {current} -> neu: {mail}\n"
            "Zum Ausführen confirm=true setzen."
        )
    out = ssh_run(
        f"plesk bin customer --update {shlex.quote(lg)} -email {shlex.quote(mail)}"
    )
    return f"Vorher: {current}\n{out}"


_SUB_INFO_KEYS = ("Domain name", "Owner's contact name", "Domain status", "Size")
_SUB_DELETE_KEYS = ("Mailboxes", "Total size")


def _subscription_info(domain: str) -> tuple[str, dict[str, list[str]]]:
    """Liest `plesk bin subscription --info` und liefert den Domainnamen plus
    die Zeilen "Schlüssel: Wert" (mehrfach vorkommende Schlüssel als Liste).
    Wirft, wenn die Domain keine Subscription ist (z.B. Zusatzdomain)."""
    d = _wp_domain(domain)
    out, err, code = _ssh_exec(f"plesk bin subscription --info {shlex.quote(d)}")
    if code != 0:
        msg = (err.strip() or out.strip()).splitlines()
        raise ValueError(f"Keine Subscription {d!r} gefunden: {msg[0] if msg else f'exit {code}'}")
    fields: dict[str, list[str]] = {}
    for line in out.splitlines():
        key, sep, val = line.partition(":")
        if sep and val.strip():
            fields.setdefault(key.strip(), []).append(val.strip())
    return d, fields


def _subscription_summary(fields: dict[str, list[str]], keys: tuple[str, ...]) -> str:
    return "\n".join(f"{k}: {', '.join(fields[k])}" for k in keys if k in fields)


def _subscription_switch(domain: str, flag: str, label: str, confirm: bool) -> str:
    d, before = _subscription_info(domain)
    summary = _subscription_summary(before, _SUB_INFO_KEYS)
    if not confirm:
        return f"Vorschau (nichts geändert) - {label}:\n{summary}\nZum Ausführen confirm=true setzen."
    out = ssh_run(f"plesk bin subscription {flag} {shlex.quote(d)}")
    _, after = _subscription_info(d)
    status = ", ".join(after.get("Domain status", ["?"]))
    return f"Vorher:\n{summary}\n\n{out}\n\nStatus jetzt: {status}"


@mcp.tool()
def subscription_suspend(domain: str, confirm: bool = False) -> str:
    """Sperrt eine komplette Plesk-Subscription (Webseiten, Mail, FTP),
    z.B. nach Kündigung oder Zahlungsverzug. Entspricht
    `plesk bin subscription --webspace-off <domain>`. Daten bleiben
    erhalten, rückgängig mit subscription_activate. domain = Hauptdomain
    der Subscription.

    ACHTUNG - schreibt auf einem Produktivserver (Kundenseiten offline):
    erfordert confirm=true als bewusste Bestätigung PRO Aufruf. Ohne
    confirm nur Vorschau (Inhaber, aktueller Status).
    """
    return _subscription_switch(domain, "--webspace-off", "Subscription sperren", confirm)


@mcp.tool()
def subscription_activate(domain: str, confirm: bool = False) -> str:
    """Aktiviert eine gesperrte Plesk-Subscription wieder. Entspricht
    `plesk bin subscription --webspace-on <domain>`.

    Erfordert confirm=true PRO Aufruf, ohne confirm nur Vorschau.
    """
    return _subscription_switch(domain, "--webspace-on", "Subscription aktivieren", confirm)


@mcp.tool()
def subscription_delete(domain: str, confirm_domain: str = "", confirm: bool = False) -> str:
    """Löscht eine Plesk-Subscription ENDGÜLTIG mit allen Dateien,
    Datenbanken und Mailboxen. Entspricht
    `plesk bin subscription --remove <domain>`. Kein Papierkorb, kein
    Rollback - vorher ggf. backup_list prüfen.

    Zweistufig: ohne confirm nur Vorschau (Inhaber, Status, Grösse,
    Mailboxen). Ausgeführt wird nur mit confirm=true UND confirm_domain =
    exakt derselbe Domainname als Rückbestätigung. Vor dem Aufruf mit
    confirm=true muss der Nutzer im Chat ausdrücklich bestätigt haben.
    """
    d, info = _subscription_info(domain)
    summary = _subscription_summary(info, _SUB_INFO_KEYS + _SUB_DELETE_KEYS)
    if not confirm:
        return (
            f"Vorschau (nichts gelöscht) - Subscription ENDGÜLTIG löschen:\n{summary}\n"
            "Zum Ausführen confirm=true und confirm_domain=<Domainname> setzen."
        )
    if confirm_domain.strip().lower() != d:
        raise ValueError(
            f"Rückbestätigung fehlt: confirm_domain muss exakt {d!r} sein, nichts gelöscht."
        )
    out = ssh_run(f"plesk bin subscription --remove {shlex.quote(d)}")
    _, _, code = _ssh_exec(f"plesk bin subscription --info {shlex.quote(d)}")
    gone = "gelöscht" if code != 0 else "NOCH VORHANDEN - bitte prüfen"
    return f"Vorher:\n{summary}\n\n{out}\n\nKontrolle: Subscription {gone}"

@mcp.tool()
def customer_delete(login: str, confirm_login: str = "", confirm: bool = False) -> str:
    """Löscht einen Plesk-Kundenaccount, der KEINE Domains/Subscriptions mehr
    hat (z.B. alter Kontakt). Entspricht `plesk bin customer --remove <login>`.
    Kunden mit Domains werden abgelehnt - deren Subscriptions zuerst mit
    subscription_delete entfernen.

    Zweistufig: ohne confirm nur Vorschau. Ausgeführt wird nur mit
    confirm=true UND confirm_login = exakt derselbe Login als
    Rückbestätigung. Vor dem Aufruf mit confirm=true muss der Nutzer im Chat
    ausdrücklich bestätigt haben.
    """
    lg = login.strip()
    if not _LOGIN_RE.match(lg):
        raise ValueError(f"Ungültiger Plesk-Login: {login!r}")
    if lg == "admin":
        raise ValueError("Der Admin-Account wird über dieses Tool nicht gelöscht.")
    out, err, code = _ssh_exec(f"plesk bin customer --info {shlex.quote(lg)}")
    if code != 0:
        raise ValueError(f"Kein Kundenaccount {lg!r} gefunden.")
    fields = {}
    for line in out.splitlines():
        key, sep, val = line.partition(":")
        if sep:
            fields.setdefault(key.strip(), val.strip())
    domains = fields.get("Domains", "")
    if domains != "0":
        raise ValueError(
            f"Kundenaccount {lg!r} hat noch Domains ({domains or '?'}) - "
            "zuerst die Subscriptions entfernen, nichts gelöscht."
        )
    summary = "\n".join(
        f"{k}: {fields[k]}" for k in ("Personal name", "Company name", "Email", "Creation date", "Domains")
        if fields.get(k)
    )
    if not confirm:
        return (
            f"Vorschau (nichts gelöscht) - Kundenaccount {lg} ENDGÜLTIG löschen:\n{summary}\n"
            "Zum Ausführen confirm=true und confirm_login=<Login> setzen."
        )
    if confirm_login.strip() != lg:
        raise ValueError(f"Rückbestätigung fehlt: confirm_login muss exakt {lg!r} sein, nichts gelöscht.")
    res = ssh_run(f"plesk bin customer --remove {shlex.quote(lg)}")
    _, _, code = _ssh_exec(f"plesk bin customer --info {shlex.quote(lg)}")
    gone = "gelöscht" if code != 0 else "NOCH VORHANDEN - bitte prüfen"
    return f"Vorher:\n{summary}\n\n{res}\n\nKontrolle: Kundenaccount {gone}"

@mcp.tool()
def lve_stats(domain: str) -> str:
    """Sucht die aktuellen LVE-Ressourcenwerte (CPU/IO-Limits und -Faults)
    für die angegebene Domain, z.B. um kurzzeitige Nichterreichbarkeit durch
    Ressourcenlimits (CloudLinux LVE) zu diagnostizieren.
    """
    d = _domain_arg(domain)
    out = ssh_run(f"lveinfo --limit 500 2>&1 | grep -i -A3 -B3 {shlex.quote(d)}")
    if not out or out == "(keine Ausgabe)":
        # Fallback über lveps, falls lveinfo den Namen nicht direkt matcht
        out = ssh_run(f"lveps 2>&1 | grep -i {shlex.quote(d)}")
    return out or f"Keine LVE-Daten für '{d}' gefunden."


@mcp.tool()
def fpm_service_status(domain: str) -> str:
    """Findet den PHP-FPM-systemd-Service der Domain (Plesk erstellt pro Domain
    einen eigenen Pool-Service, z.B. plesk-php82-fpm_domain.ch_216.service)
    und zeigt dessen Status inkl. letzter Log-Zeilen (Start/Stop/Fehler).
    """
    d = _domain_arg(domain)
    units = ssh_run(f"systemctl list-units --type=service --all 2>&1 | grep -i {shlex.quote(d)}")
    if not units or units == "(keine Ausgabe)":
        return f"Kein FPM-Service für '{d}' gefunden."
    first_unit = units.strip().splitlines()[0].split()[0]
    status = ssh_run(f"systemctl status {shlex.quote(first_unit)} --no-pager -l")
    return f"Gefundene Units:\n{units}\n\nStatus von {first_unit}:\n{status}"


@mcp.tool()
def fpm_journal(domain: str, since: str = "-2h", until: str = "") -> str:
    """Zeigt systemd-Journal-Einträge (Start/Stop/Crash/Fehler) des
    FPM-Service der Domain im angegebenen Zeitfenster.
    since/until im journalctl-Format, z.B. "2026-09-17 21:00" oder "-2h".
    """
    d = _domain_arg(domain)
    units = ssh_run(f"systemctl list-units --type=service --all 2>&1 | grep -i {shlex.quote(d)}")
    if not units or units == "(keine Ausgabe)":
        return f"Kein FPM-Service für '{d}' gefunden."
    first_unit = units.strip().splitlines()[0].split()[0]

    cmd = f"journalctl -u {shlex.quote(first_unit)} --no-pager --since {shlex.quote(since)}"
    if until:
        cmd += f" --until {shlex.quote(until)}"
    return ssh_run(cmd)


_LOG_LINES_MAX = 5000


def _clamp_lines(lines: Any) -> int:
    """lines fuer tail -n auf 1..5000 klemmen (negativ/0 = ganze Datei bei
    tail, riesige Werte = unbegrenzte Ausgabe)."""
    try:
        n = int(lines)
    except (TypeError, ValueError):
        raise ValueError("lines muss eine Zahl sein.")
    return max(1, min(_LOG_LINES_MAX, n))


@mcp.tool()
def search_log(
    domain: str,
    log_type: str = "proxy_error_log",
    pattern: str = "",
    lines: int = 200,
    include_rotated: bool = False,
) -> str:
    """Durchsucht ein Vhost-Log der Domain nach einem optionalen Muster
    (regex, an grep -E übergeben) und gibt die letzten `lines` Treffer zurück
    (1..5000). log_type: proxy_error_log | error_log | access_log
    Pfad: /var/www/vhosts/<domain>/logs/<log_type>
    include_rotated: zusätzlich die rotierten, gzip-komprimierten Logs
    (<log_type>-YYYYMMDD.gz im selben Verzeichnis) mit durchsuchen - nötig
    für Vorfälle, die länger als die aktuelle Logrotation zurückliegen.
    """
    d = _domain_arg(domain)
    allowed_logs = {"proxy_error_log", "error_log", "access_log"}
    if log_type not in allowed_logs:
        raise ValueError(f"log_type muss einer von {allowed_logs} sein.")
    lines = _clamp_lines(lines)

    log_dir = f"/var/www/vhosts/{d}/logs"
    path = f"{log_dir}/{log_type}"

    if include_rotated:
        if not pattern:
            raise ValueError(
                "include_rotated benötigt ein pattern (sonst zu viele Treffer "
                "über mehrere komprimierte Dateien hinweg)."
            )
        cmd = (
            f"zgrep -E {shlex.quote(pattern)} "
            f"{shlex.quote(log_dir)}/{log_type}*.gz 2>&1 | tail -n {int(lines)}"
        )
        return ssh_run(cmd)

    if pattern:
        cmd = f"grep -E {shlex.quote(pattern)} {shlex.quote(path)} 2>&1 | tail -n {int(lines)}"
    else:
        cmd = f"tail -n {int(lines)} {shlex.quote(path)} 2>&1"
    return ssh_run(cmd)


@mcp.tool()
def search_main_nginx_log(
    pattern: str,
    log: str = "access",
    lines: int = 200,
    include_rotated: bool = False,
) -> str:
    """Durchsucht das serverweite nginx-Log (nicht pro-Vhost) unter
    /var/log/nginx/ - dort landen u.a. 408/523-Fehler, die CloudLinux
    Web-Monitoring-Reports zeigen, die einzelnen Vhost-Logs aber oft nicht
    erfassen (Fehler vor dem Routing zum Backend).
    log: "access" oder "error"
    include_rotated: auch die rotierten .gz-Dateien der letzten Tage durchsuchen.
    """
    if log not in {"access", "error"}:
        raise ValueError("log muss 'access' oder 'error' sein.")
    lines = _clamp_lines(lines)

    if include_rotated:
        cmd = (
            f"zgrep -E {shlex.quote(pattern)} "
            f"/var/log/nginx/{log}.log* 2>&1 | tail -n {int(lines)}"
        )
    else:
        cmd = (
            f"grep -E {shlex.quote(pattern)} "
            f"/var/log/nginx/{log}.log 2>&1 | tail -n {int(lines)}"
        )
    return ssh_run(cmd)


@mcp.tool()
def check_oom_kills(since: str = "-24h", until: str = "", domain: str = "") -> str:
    """Prüft im Kernel-Journal auf Out-Of-Memory-Kills im angegebenen
    Zeitfenster - klassischer Grund für "Seite bricht immer wieder weg".
    Optional nach Domain/Prozessname filtern.
    """
    cmd = f"journalctl -k --no-pager --since {shlex.quote(since)}"
    if until:
        cmd += f" --until {shlex.quote(until)}"
    cmd += " | grep -iE 'oom|killed process'"
    if domain:
        d = _domain_arg(domain)
        cmd += f" | grep -i {shlex.quote(d)}"
    out = ssh_run(cmd)
    if not out or out == "(keine Ausgabe)":
        return "Keine OOM-Kills im angegebenen Zeitfenster gefunden."
    return out


@mcp.tool()
def disk_usage(domain: str) -> str:
    """Zeigt die Disk-Nutzung des Vhost-Verzeichnisses der Domain
    (httpdocs, logs, etc. einzeln aufgeschlüsselt)."""
    d = _domain_arg(domain)
    return ssh_run(f"du -sh /var/www/vhosts/{shlex.quote(d)}/* 2>&1")


@mcp.tool()
def read_vhost_file(domain: str, path: str, encoding: str = "text") -> str:
    """Liest eine Datei aus dem Vhost-Verzeichnis der Domain
    (/var/www/vhosts/<domain>/<path>, path relativ, z.B. "httpdocs/index.php").
    encoding: "text" (UTF-8, Standard) oder "base64" (für Binärdateien wie
    Bilder). Rein lesend - kein confirm nötig.
    """
    if encoding not in {"text", "base64"}:
        raise ValueError("encoding muss 'text' oder 'base64' sein.")
    full = _vhost_path(domain, path)

    client = _ssh_connect()
    try:
        sftp = client.open_sftp()
        try:
            _assert_real_within_vhost(sftp, full, domain)
            with sftp.open(full, "rb") as f:
                data = f.read()
        finally:
            sftp.close()
    finally:
        client.close()

    if encoding == "base64":
        return base64.b64encode(data).decode("ascii")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(
            "Datei ist nicht UTF-8-dekodierbar (vermutlich Binärdatei) - "
            "mit encoding='base64' erneut versuchen."
        )


@mcp.tool()
def write_vhost_file(
    domain: str,
    path: str,
    content: str,
    confirm: bool = False,
    encoding: str = "text",
    allow_executable: bool = False,
) -> str:
    """Schreibt/überschreibt eine Datei im Vhost-Verzeichnis der Domain
    (/var/www/vhosts/<domain>/<path>) - legt auch fehlende Zwischenordner an.
    encoding: "text" (content ist Klartext, UTF-8 - Standard) oder "base64"
    (content ist base64-kodiert, für Binärdateien wie Bilder/ZIPs).

    Dieselben Regeln wie upload_begin/fetch_to_vhost: domain muss die
    Hauptdomain einer Subscription sein, path relativ ohne "..", geschützte
    Bereiche (conf, logs, .ssh, Dotfiles im Vhost-Root, Docroot selbst, ...)
    werden abgelehnt, ausführbare Dateien (.php, .htaccess, .user.ini, ...)
    nur mit allow_executable=true. Grössenlimit UPLOAD_MAX_BYTES.

    ACHTUNG - schreibt auf einem Produktivserver: erfordert confirm=true als
    bewusste Bestätigung PRO Aufruf (keine globale Freischaltung). Existiert
    die Zieldatei bereits, wird vorher automatisch ein Backup als
    "<path>.bak-<YYYYMMDDHHMMSS>" im selben Verzeichnis angelegt (siehe
    delete_vhost_backup zum späteren Aufräumen, sobald die Änderung verifiziert ist).
    Ist der Zielpfad bereits ein Symlink, wird aus Sicherheitsgründen
    abgebrochen (kein Überschreiben durch Symlinks hindurch).
    Besitzer/Rechte: Bestehende Dateien (und ihr Backup) behalten Besitzer,
    Gruppe und Rechte der alten Datei. Neue Dateien (0644) und neu angelegte
    Ordner (0755) erhalten als Besitzer den User des nächsten existierenden
    übergeordneten Ordners - gehört dieser root, den Besitzer von httpdocs
    (Subscription-User) - und als Gruppe dessen primäre Gruppe (psacln).
    Fallback-Reihenfolge für den User: Referenzordner, httpdocs,
    /var/www/vhosts/<domain>/. Nie root - sonst Fehler. Unterhalb von
    httpdocs werden zudem bestehende Dateien und übergeordnete Ordner, die
    root gehören, auf diesen Besitzer umgestellt (Rechte bleiben; httpdocs
    selbst auf <user>:psaserv). Eine root-eigene Datei ausserhalb von
    httpdocs wird nicht überschrieben.
    """
    if not confirm:
        raise ValueError(
            "confirm=true erforderlich - dieses Tool schreibt auf einem "
            "Produktivserver und braucht eine explizite Bestätigung pro Aufruf."
        )
    if encoding not in {"text", "base64"}:
        raise ValueError("encoding muss 'text' oder 'base64' sein.")

    if encoding == "base64":
        try:
            data = base64.b64decode(content, validate=True)
        except Exception as e:
            raise ValueError(f"content ist kein gültiges Base64: {e}")
    else:
        data = content.encode("utf-8")
    if len(data) > _UPLOAD_MAX_BYTES:
        raise ValueError(
            f"content ist {len(data)} Bytes gross - Limit {_UPLOAD_MAX_BYTES} Bytes (UPLOAD_MAX_BYTES); "
            "für grosse Dateien upload_begin oder fetch_to_vhost verwenden."
        )

    # Dieselbe Prüfkette wie upload_begin/fetch_to_vhost: Subscription, kein
    # geschützter Bereich, ausführbare Dateien nur mit allow_executable.
    ctx = _vhost_ctx(domain)
    parts = _strict_parts(path)
    _check_not_protected(parts, ctx, "Ziel")
    _check_executable(parts, allow_executable)
    base = ctx["base"]
    full = f"{base}/{'/'.join(parts)}"

    # Gemeinsame Schreiblogik mit Upload/Fetch (Helper auf dem Plesk-Server,
    # siehe _HELPER_SRC): Temp-Datei, Backup, atomarer rename, Besitzer.
    try:
        res = _helper_put(
            _helper_args(base, parts=parts, max_bytes=min(len(data), _UPLOAD_MAX_BYTES),
                         reserve=_UPLOAD_MIN_FREE),
            [data],
        )
    except _HelperError as e:
        if e.code == "target_symlink":
            raise ValueError(f"'{full}' ist ein Symlink - wird aus Sicherheitsgründen nicht überschrieben.")
        raise ValueError(_helper_message(e))

    verb = "Überschrieben" if res["existed"] else "Neu erstellt"
    note = ""
    if res["created"]:
        note += " Neu angelegte Ordner: " + ", ".join(f"{base}/{c}" for c in res["created"]) + "."
    if res["fix_owner"]:
        note += " Besitzer der Datei von root umgestellt."
    if res["fixed"]:
        note += " Ordner von root umgestellt: " + ", ".join(f"{base}/{c}" for c in res["fixed"]) + "."
    if res["backup"]:
        note += f" Backup der alten Version: {base}/{res['backup']}"
    return (
        f"{verb}: {full} ({res['size']} Bytes). Besitzer: {res['owner']}, "
        f"Rechte: {res['mode']}.{note}"
    )


_REPLACE_MAX_BYTES = int(os.environ.get("REPLACE_MAX_BYTES", str(5 * 1024 * 1024)))
_REPLACE_MAX_FILES = 50
_REPLACE_MAX_CONTEXT = 50


def _replace_parts(ctx: dict[str, Any], path: str, allow_executable: bool) -> list[str]:
    """Pfadprüfung wie write_vhost_file/upload_begin: relativ ohne "..",
    absolute Pfade oder Wildcards (_strict_parts), kein geschützter Bereich,
    ausführbare Dateien nur mit allow_executable."""
    parts = _strict_parts(path)
    _check_not_protected(parts, ctx, "Ziel")
    _check_executable(parts, allow_executable)
    return parts


def _replace_run(domain: str, old_str: str, new_str: str, replace_all: bool,
                 paths: list[list[str]] | None = None, glob: str | None = None,
                 allow_executable: bool = False) -> dict[str, Any]:
    if not isinstance(old_str, str) or old_str == "":
        raise ValueError("old_str darf nicht leer sein.")
    if not isinstance(new_str, str):
        raise ValueError("new_str muss ein String sein (leer = Textstelle löschen).")
    if old_str == new_str:
        raise ValueError("old_str und new_str sind identisch - nichts zu ersetzen.")
    base = f"{_VHOST_BASE}/{_domain_arg(domain)}"
    payload = json.dumps({"old": old_str, "new": new_str}).encode("utf-8")
    args = _helper_args(base, paths=paths or [], glob=glob or "", replace_all=bool(replace_all),
                        max_bytes=_REPLACE_MAX_BYTES, max_files=_REPLACE_MAX_FILES,
                        max_ctx=_REPLACE_MAX_CONTEXT, max_payload=len(payload),
                        reserve=_UPLOAD_MIN_FREE, allow_exec=bool(allow_executable))
    # old_str/new_str über stdin statt Kommandozeile: nicht in der Prozessliste
    # des Servers sichtbar und ohne argv-Längenlimit.
    session = _helper_session_factory("replace", args)
    try:
        session.send(payload)
    except BaseException:
        session.abort()
        raise
    try:
        res = _helper_result(session.finish())
    except _HelperError as e:
        _audit("replace", domain=_domain_arg(domain), paths=["/".join(p) for p in paths or []],
               glob=glob or None, result="failed", error=e.code)
        if e.code == "ambiguous":
            rel, _, cnt = e.detail.rpartition("|")
            raise ValueError(
                f"old_str kommt in '{rel}' {cnt}-mal vor - nichts geändert. Suchtext mit mehr "
                "Kontext eindeutig machen oder replace_all=true setzen."
            )
        raise ValueError(_helper_message(e))
    _audit("replace", domain=_domain_arg(domain),
           files=[{"path": f["path"], "replacements": f["replacements"]} for f in res["files"]],
           skipped=len(res["skipped"]), result="ok")
    return res


def _replace_file_view(base: str, f: dict[str, Any]) -> dict[str, Any]:
    out = {
        "datei": f"{base}/{f['path']}",
        "ersetzungen": f["replacements"],
        "backup": f"{base}/{f['backup']}",
        "groesse_neu": f["size"],
        "groesse_alt": f["old_size"],
        "besitzer": f["owner"],
        "rechte": f["mode"],
        "kontext": f["context"],
    }
    if f["replacements"] > len(f["context"]):
        out["kontext_hinweis"] = f"Nur die ersten {len(f['context'])} von {f['replacements']} Treffern gezeigt."
    if f["crlf"]:
        out["zeilenenden"] = "CRLF (beibehalten)"
    if f["fix_owner"]:
        out["hinweis"] = "Besitzer der Datei von root umgestellt (wie write_vhost_file)."
    return out


@mcp.tool()
def replace_in_vhost_file(
    domain: str,
    path: str,
    old_str: str,
    new_str: str,
    replace_all: bool = False,
    confirm: bool = False,
    allow_executable: bool = False,
) -> str:
    """Ersetzt eine Textstelle in einer Datei im Vhost-Verzeichnis der Domain
    (/var/www/vhosts/<domain>/<path>, path relativ, z.B.
    "httpdocs/wp-config.php"), ohne die ganze Datei neu senden zu müssen.
    old_str muss exakt (inkl. Leerzeichen/Einrückung) vorkommen; new_str darf
    leer sein (= Textstelle löschen). Kommt old_str mehrfach vor, wird ohne
    replace_all=true abgebrochen und die Trefferzahl genannt - nichts wird
    geändert. Kein Treffer ist ebenfalls ein Fehler.

    Nur UTF-8-Textdateien bis 5 MB; Binärdateien (Nullbytes) oder nicht
    dekodierbare Dateien werden abgelehnt. Zeilenenden bleiben erhalten (in
    reinen CRLF-Dateien wird "\\n" in old_str/new_str automatisch als "\\r\\n"
    behandelt).

    ACHTUNG - schreibt auf einem Produktivserver: erfordert confirm=true als
    bewusste Bestätigung PRO Aufruf. Vor dem Schreiben wird automatisch ein
    Backup "<path>.bak-<YYYYMMDDHHMMSS>" angelegt (siehe delete_vhost_backup),
    danach wird atomar ersetzt (Temp-Datei + rename). Sicherheitsregeln wie
    write_vhost_file/upload_begin: domain muss die Hauptdomain einer
    Subscription sein, kein "..", keine absoluten Pfade, geschützte Bereiche
    (conf, logs, .ssh, Dotfiles im Vhost-Root, ...) abgelehnt, ausführbare
    Dateien (.php, .htaccess, .user.ini, ...) nur mit allow_executable=true,
    Abbruch bei Symlinks (Datei oder Ordner im Pfad), keine root-eigenen
    Dateien ausserhalb von httpdocs; Besitzer, Gruppe und Rechte bleiben
    erhalten (root-eigene Dateien unter httpdocs werden wie bei
    write_vhost_file auf den Subscription-User umgestellt).

    Rückgabe: Anzahl Ersetzungen, Backup-Pfad, neue Dateigrösse und je Treffer
    eine Kontextzeile vorher/nachher (max. 120 Zeichen, max. 50 Treffer).
    """
    if not confirm:
        raise ValueError(
            "confirm=true erforderlich - dieses Tool schreibt auf einem "
            "Produktivserver und braucht eine explizite Bestätigung pro Aufruf."
        )
    ctx = _vhost_ctx(domain)
    parts = _replace_parts(ctx, path, allow_executable)
    res = _replace_run(ctx["domain"], old_str, new_str, replace_all, paths=[parts],
                       allow_executable=allow_executable)
    if not res["files"]:
        raise ValueError(
            f"old_str kommt in '{'/'.join(parts)}' nicht vor - nichts geändert. Exakten Text "
            "inkl. Leerzeichen/Einrückung prüfen (z.B. mit read_vhost_file)."
        )
    return _json(_replace_file_view(ctx["base"], res["files"][0]))


@mcp.tool()
def replace_in_vhost_files(
    domain: str,
    old_str: str,
    new_str: str,
    paths: list[str] | None = None,
    glob: str = "",
    replace_all: bool = False,
    confirm: bool = False,
    allow_executable: bool = False,
) -> str:
    """Wie replace_in_vhost_file, aber für mehrere Dateien in einem Aufruf.
    Entweder paths (Liste relativer Pfade, max. 50) ODER glob angeben. glob
    ist relativ zu /var/www/vhosts/<domain>/ und muss mit "httpdocs/"
    beginnen (z.B. "httpdocs/wp-content/themes/mein-theme/**/*.php"; "*", "?",
    "[...]" pro Ordnerebene, "**" für beliebig viele Ebenen; max. 50 Treffer).
    Beim glob werden Symlinks nicht verfolgt (nur aufgelistet), Backups
    (.bak-*) und der Papierkorb ignoriert.

    Pro Datei dieselben Prüfungen und Backups wie replace_in_vhost_file;
    ausführbare Dateien (.php, .htaccess, ...) - auch als glob-Treffer - nur
    mit allow_executable=true.
    Dateien ohne Treffer werden übersprungen und im Ergebnis aufgelistet.
    Alles-oder-nichts: Zuerst werden alle Dateien gelesen und geprüft (UTF-8,
    Grösse, Symlinks, Mehrfachtreffer ohne replace_all, ...), dann alle
    Temp-Dateien und Backups geschrieben und erst danach umbenannt - ein
    Fehler in einer Datei lässt alle Dateien unverändert.
    Erfordert confirm=true pro Aufruf.
    """
    if not confirm:
        raise ValueError(
            "confirm=true erforderlich - dieses Tool schreibt auf einem "
            "Produktivserver und braucht eine explizite Bestätigung pro Aufruf."
        )
    g = (glob or "").strip()
    if bool(paths) == bool(g):
        raise ValueError("Entweder paths oder glob angeben (genau eines davon).")
    ctx = _vhost_ctx(domain)
    plist = None
    if paths:
        if len(paths) > _REPLACE_MAX_FILES:
            raise ValueError(f"Maximal {_REPLACE_MAX_FILES} Dateien pro Aufruf.")
        plist = [_replace_parts(ctx, p, allow_executable) for p in paths]
        if len({"/".join(p) for p in plist}) != len(plist):
            raise ValueError("paths enthält doppelte Einträge.")
    else:
        gparts = g.split("/")
        if (g.startswith("/") or "\\" in g or "\0" in g or len(g) > 4096 or len(gparts) < 2
                or gparts[0] != "httpdocs" or any(c in ("", ".", "..") for c in gparts)):
            raise ValueError(
                f"Ungültiges glob {glob!r}: relativ, muss mit 'httpdocs/' beginnen, ohne '..', '.' "
                "und leere Teile."
            )
    res = _replace_run(ctx["domain"], old_str, new_str, replace_all, paths=plist, glob=g or None,
                       allow_executable=allow_executable)
    base = ctx["base"]
    out: dict[str, Any] = {
        "geaendert": [_replace_file_view(base, f) for f in res["files"]],
        "ersetzungen_gesamt": sum(f["replacements"] for f in res["files"]),
        "ohne_treffer": res["skipped"],
    }
    if res["symlinks"]:
        out["symlinks_uebersprungen"] = res["symlinks"]
    if not res["files"]:
        out["hinweis"] = "old_str kommt in keiner Datei vor - nichts geändert."
    return _json(out)


@mcp.tool()
def delete_vhost_backup(domain: str, path: str, confirm: bool = False) -> str:
    """Löscht eine von write_vhost_file angelegte Backup-Datei
    (/var/www/vhosts/<domain>/<path>, path muss auf ".bak-<14-stellige
    Zeitstempel>" enden, z.B. "httpdocs/index.php.bak-20260919143012") -
    zum Aufräumen, nachdem eine Änderung verifiziert wurde. Löscht bewusst
    NUR Dateien mit diesem Namensmuster, keine sonstigen Vhost-Dateien.
    Erfordert confirm=true pro Aufruf.
    """
    if not confirm:
        raise ValueError(
            "confirm=true erforderlich - dieses Tool löscht eine Datei auf "
            "einem Produktivserver und braucht eine explizite Bestätigung pro Aufruf."
        )
    full = _vhost_path(domain, path)
    if not _BACKUP_SUFFIX_RE.search(full):
        raise ValueError(
            f"'{path}' sieht nicht wie eine von write_vhost_file angelegte "
            "Backup-Datei aus (erwartet: ...bak-JJJJMMTTHHMMSS). Aus "
            "Sicherheitsgründen löscht dieses Tool ausschliesslich solche Dateien."
        )

    client = _ssh_connect()
    try:
        sftp = client.open_sftp()
        try:
            _assert_real_within_vhost(sftp, full, domain)
            lst = sftp.lstat(full)
            if stat.S_ISLNK(lst.st_mode):
                raise ValueError(
                    f"'{full}' ist ein Symlink - wird aus Sicherheitsgründen "
                    "nicht gelöscht."
                )
            sftp.remove(full)
        finally:
            sftp.close()
    finally:
        client.close()

    return f"Backup gelöscht: {full}"


# Rotierte Logs (logrotate/Plesk): access_log.1, error_log.2.gz,
# proxy_error_log-20260919.gz, access_log.processed.1.gz etc. - alles, was
# NICHT die aktuell vom Webserver beschriebene Datei ist.
_ROTATED_LOG_RE = re.compile(r"(\.gz|\.\d+|-\d{8})$")
_LOG_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# ---------------------------------------------------------------------------
# Vhost-Dateien: Helper auf dem Plesk-Server, Upload, Fetch, Verschieben,
# Löschen/Papierkorb
# ---------------------------------------------------------------------------
#
# Der Container hat keinen direkten Dateisystemzugriff auf den Plesk-Server.
# Alle Dateioperationen laufen über einen kleinen Python-Helper (_HELPER_SRC),
# der per SSH auf dem Plesk-Server gestartet wird und dort ausschliesslich mit
# Verzeichnis-Handles arbeitet (openat/O_NOFOLLOW/renameat2). Dadurch kann
# zwischen Prüfung und Aktion kein Symlink untergeschoben werden. Datei-Inhalte
# (write_vhost_file, Upload, Fetch) werden in Frames über die SSH-Verbindung
# gestreamt - nie komplett im RAM, nie durch den LLM-Kontext.

_HELPER_PYTHON = os.environ.get("PLESK_HELPER_PYTHON", "/usr/libexec/platform-python").strip()
_HELPER_TIMEOUT = int(os.environ.get("PLESK_HELPER_TIMEOUT", "600"))
_UPLOAD_MAX_BYTES = int(os.environ.get("UPLOAD_MAX_BYTES", str(25 * 1024 * 1024)))
_UPLOAD_TOKEN_TTL = 300
_UPLOAD_RATE_LIMIT = int(os.environ.get("UPLOAD_RATE_LIMIT_PER_MIN", "20"))
_UPLOAD_MAX_CONCURRENT = int(os.environ.get("UPLOAD_MAX_CONCURRENT", "2"))
_UPLOAD_MIN_FREE = int(os.environ.get("UPLOAD_MIN_FREE_MB", "256")) * 1024 * 1024
_UPLOAD_TIMEOUT = int(os.environ.get("UPLOAD_TIMEOUT", "300"))
_UPLOAD_TRUSTED_PROXIES = {
    p.strip() for p in os.environ.get("UPLOAD_TRUSTED_PROXIES", "").split(",") if p.strip()
}
_PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
_FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "120"))
_FETCH_MAX_REDIRECTS = 3
_TRASH_MAX_BYTES = int(os.environ.get("VHOST_TRASH_MAX_MB", "500")) * 1024 * 1024
_TRASH_AUTOCLEAN_DAYS = int(os.environ.get("VHOST_TRASH_AUTOCLEAN_DAYS", "0"))
_DELETE_TOKEN_TTL = 900
_AUDIT_LOG_FILE = os.environ.get("AUDIT_LOG_FILE", "").strip()

# Ausführbare Dateien: nur mit allow_executable=true UND confirm=true.
_EXEC_SUFFIXES = [
    "php", "php3", "php4", "php5", "php7", "php8", "phtml", "pht", "phps", "phar",
    "cgi", "pl", "py", "sh",
]
_EXEC_NAMES = [".htaccess", ".user.ini"]

# Geschützt (immer ablehnen): Unterbäume direkt im Vhost-Root.
_PROTECTED_TOP = {
    "conf", "logs", "statistics", ".ssh", ".mcp-trash",
    "mail", "maildir", "mailnames",
    # chroot-Skelett der Plesk-Shell (bin -> usr/bin, dev, etc ...)
    "bin", "dev", "etc", "lib", "lib64", "usr", "var", "tmp",
}
_GLOB_CHARS = set("*?[]{}")

_HELPER_SRC = r'''# plesk-mcp vhost helper - läuft per SSH auf dem Plesk-Server (Python >= 3.6).
# Arbeitet ausschliesslich über Verzeichnis-Handles (openat/O_NOFOLLOW), damit
# zwischen Prüfung und Aktion kein Symlink untergeschoben werden kann.
import base64
import ctypes
import errno
import fnmatch
import grp
import hashlib
import json
import os
import pwd
import re
import stat
import struct
import sys
import time

O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
TRASH = ".mcp-trash"
META = ".mcp-trash-meta.json"
SCAN_MAX = 200000
ENTRY_RE = re.compile(r"^\d{14}(-\d+)?$")


class HErr(Exception):
    def __init__(self, code, detail=""):
        Exception.__init__(self, code)
        self.code = code
        self.detail = detail


def check_parts(parts, allow_empty=False):
    if not isinstance(parts, list) or (not parts and not allow_empty):
        raise HErr("bad_path")
    for p in parts:
        if (not isinstance(p, str) or p in ("", ".", "..") or "/" in p or "\0" in p
                or len(p.encode("utf-8")) > 255):
            raise HErr("bad_path")
    return parts


def join(parts):
    return "/".join(parts)


def open_abs(path):
    fd = os.open("/", O_DIR)
    for comp in [c for c in path.split("/") if c]:
        try:
            nfd = os.open(comp, O_DIR, dir_fd=fd)
        except OSError:
            os.close(fd)
            raise HErr("base_unavailable")
        os.close(fd)
        fd = nfd
    return fd


def lst(dfd, name):
    try:
        return os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def open_dir(dfd, name, rel):
    try:
        return os.open(name, O_DIR, dir_fd=dfd)
    except FileNotFoundError:
        raise HErr("not_found", rel)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise HErr("symlink_in_path", rel)
        raise


def psaserv_gid():
    try:
        return grp.getgrnam("psaserv").gr_gid
    except KeyError:
        raise HErr("psaserv_missing")


def walk(base_fd, parts, create=None, fix=None, prefix=()):
    """Öffnet base/parts Komponente für Komponente ohne Symlinks zu folgen.
    create: {"uid","gid","mode"} legt fehlende Ordner an. fix: {"uid","gid"}
    stellt root-eigene Ordner unter httpdocs um (httpdocs selbst auf psaserv)."""
    fd = os.dup(base_fd)
    rel = list(prefix)
    created = []
    fixed = []
    try:
        for comp in parts:
            rel.append(comp)
            try:
                nfd = os.open(comp, O_DIR, dir_fd=fd)
            except FileNotFoundError:
                if create is None:
                    raise HErr("not_found", join(rel))
                os.mkdir(comp, 0o700, dir_fd=fd)
                nfd = open_dir(fd, comp, join(rel))
                os.fchown(nfd, create["uid"], create["gid"])
                os.fchmod(nfd, create["mode"])
                created.append(join(rel))
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise HErr("symlink_in_path", join(rel))
                raise
            os.close(fd)
            fd = nfd
            if fix is not None and rel[0] == "httpdocs":
                st = os.fstat(fd)
                if st.st_uid == 0 or st.st_gid == 0:
                    gid = psaserv_gid() if len(rel) == 1 else fix["gid"]
                    os.fchown(fd, fix["uid"], gid)
                    fixed.append(join(rel))
        return fd, created, fixed
    except BaseException:
        os.close(fd)
        raise


def ref_owner(base_fd, dparts):
    """User: nächster existierender Ordner, sonst httpdocs, sonst Vhost-Root -
    nie root. Gruppe: primäre Gruppe des Users."""
    fd = os.dup(base_fd)
    base_uid = os.fstat(fd).st_uid
    uid = base_uid
    try:
        for comp in dparts:
            try:
                nfd = os.open(comp, O_DIR, dir_fd=fd)
            except FileNotFoundError:
                break
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise HErr("symlink_in_path", comp)
                raise
            os.close(fd)
            fd = nfd
            uid = os.fstat(fd).st_uid
    finally:
        os.close(fd)
    if uid == 0:
        st = lst(base_fd, "httpdocs")
        if st is not None and stat.S_ISDIR(st.st_mode):
            uid = st.st_uid
    if uid == 0:
        uid = base_uid
    if uid == 0:
        raise HErr("owner_root")
    try:
        gid = pwd.getpwuid(uid).pw_gid
    except KeyError:
        raise HErr("owner_unknown")
    if gid == 0:
        raise HErr("owner_root")
    return uid, gid


def names(uid, gid):
    try:
        u = pwd.getpwuid(uid).pw_name
    except KeyError:
        u = str(uid)
    try:
        g = grp.getgrgid(gid).gr_name
    except KeyError:
        g = str(gid)
    return u + ":" + g


_renameat2 = None


def rename_noreplace(sfd, sname, dfd, dname):
    """renameat2(RENAME_NOREPLACE): schlägt fehl, wenn das Ziel existiert."""
    global _renameat2
    if _renameat2 is None:
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            _renameat2 = getattr(libc, "renameat2")
        except (OSError, AttributeError):
            _renameat2 = False
    if _renameat2:
        r = _renameat2(sfd, sname.encode("utf-8"), dfd, dname.encode("utf-8"), 1)
        if r == 0:
            return
        e = ctypes.get_errno()
        if e == errno.EEXIST:
            raise HErr("exists")
        if e == errno.EXDEV:
            raise HErr("cross_device")
        if e not in (errno.ENOSYS, errno.EINVAL):
            raise OSError(e, os.strerror(e))
    if lst(dfd, dname) is not None:
        raise HErr("exists")
    rename(sfd, sname, dfd, dname)


def rename(sfd, sname, dfd, dname):
    try:
        os.rename(sname, dname, src_dir_fd=sfd, dst_dir_fd=dfd)
    except OSError as e:
        if e.errno == errno.EXDEV:
            raise HErr("cross_device")
        if e.errno == errno.EINVAL:
            raise HErr("invalid_move")
        raise


def copy_file(dfd, src_name, dst_name, uid, gid, mode, expect_ino):
    """Kopiert eine reguläre Datei im selben Ordner (Backup), ohne Symlinks."""
    sfd = os.open(src_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
    try:
        st = os.fstat(sfd)
        if not stat.S_ISREG(st.st_mode) or st.st_ino != expect_ino:
            raise HErr("changed")
        tfd = os.open(dst_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                      0o600, dir_fd=dfd)
        try:
            os.fchown(tfd, uid, gid)
            os.fchmod(tfd, mode)
            while True:
                buf = os.read(sfd, 1048576)
                if not buf:
                    break
                while buf:
                    n = os.write(tfd, buf)
                    buf = buf[n:]
            os.fsync(tfd)
        finally:
            os.close(tfd)
    finally:
        os.close(sfd)


def is_exec(name, a):
    n = name.lower()
    if n in a.get("exec_names", []):
        return True
    segs = n.split(".")[1:]
    return any(s in a.get("exec_suffixes", []) for s in segs)


def scan(pfd, name, rel, a):
    """Rekursive Auflistung ohne Symlinks zu folgen (für Dry-Run/Token)."""
    st = lst(pfd, name)
    if st is None:
        raise HErr("not_found", rel)
    entries = []
    state = {"exec": False, "size": 0}

    def rec(dfd, nm, r, s):
        if len(entries) >= SCAN_MAX:
            raise HErr("too_many_entries")
        if stat.S_ISLNK(s.st_mode):
            typ = "link"
        elif stat.S_ISDIR(s.st_mode):
            typ = "dir"
        elif stat.S_ISREG(s.st_mode):
            typ = "file"
        else:
            typ = "other"
        size = 0 if typ == "dir" else s.st_size
        state["size"] += size
        entries.append([r, typ, size, s.st_mtime_ns, s.st_ino])
        if is_exec(nm, a):
            state["exec"] = True
        if typ == "dir":
            fd = open_dir(dfd, nm, r)
            try:
                for c in sorted(os.listdir(fd)):
                    cs = lst(fd, c)
                    if cs is not None:
                        rec(fd, c, r + "/" + c, cs)
            finally:
                os.close(fd)

    rec(pfd, name, rel, st)
    digest = hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "type": entries[0][1],
        "files": sum(1 for e in entries if e[1] != "dir"),
        "dirs": sum(1 for e in entries if e[1] == "dir"),
        "size": state["size"],
        "has_exec": state["exec"],
        "digest": digest,
        "paths": [e[0] for e in entries[:50]],
        "ino": st.st_ino,
    }


def rm_tree(pfd, name):
    st = lst(pfd, name)
    if st is None:
        return
    if stat.S_ISDIR(st.st_mode):
        fd = open_dir(pfd, name, name)
        try:
            for c in os.listdir(fd):
                rm_tree(fd, c)
        finally:
            os.close(fd)
        os.rmdir(name, dir_fd=pfd)
    else:
        os.unlink(name, dir_fd=pfd)


def read_frames(tfd, max_bytes):
    h = hashlib.sha256()
    size = 0
    inp = sys.stdin.buffer
    while True:
        hdr = inp.read(4)
        if len(hdr) != 4:
            raise HErr("incomplete")
        (n,) = struct.unpack(">I", hdr)
        if n == 0:
            break
        size += n
        if size > max_bytes:
            raise HErr("too_large")
        while n:
            buf = inp.read(min(n, 1048576))
            if not buf:
                raise HErr("incomplete")
            n -= len(buf)
            h.update(buf)
            while buf:
                w = os.write(tfd, buf)
                buf = buf[w:]
    try:
        commit = json.loads(inp.readline().decode("utf-8"))
    except ValueError:
        raise HErr("incomplete")
    return size, h.hexdigest(), commit


def plan_put(base_fd, parts):
    dparts, name = parts[:-1], parts[-1]
    in_httpdocs = len(parts) >= 2 and parts[0] == "httpdocs"
    try:
        pfd, _, _ = walk(base_fd, dparts)
    except HErr as e:
        if e.code != "not_found":
            raise
        pfd = None
    try:
        st = lst(pfd, name) if pfd is not None else None
    finally:
        if pfd is not None:
            os.close(pfd)
    plan = {"exists": st is not None, "fix_owner": False}
    if st is not None:
        if stat.S_ISLNK(st.st_mode):
            raise HErr("target_symlink", join(parts))
        if not stat.S_ISREG(st.st_mode):
            raise HErr("target_not_file", join(parts))
        uid, gid, mode = st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)
        if in_httpdocs and (uid == 0 or gid == 0):
            uid, gid = ref_owner(base_fd, dparts)
            plan["fix_owner"] = True
        elif uid == 0:
            raise HErr("target_root_outside_httpdocs", join(parts))
        plan.update(ino=st.st_ino, old_uid=st.st_uid, old_gid=st.st_gid, old_mode=st.st_mode, old_size=st.st_size)
    else:
        uid, gid = ref_owner(base_fd, dparts)
        mode = 0o644
    plan.update(uid=uid, gid=gid, mode=mode, owner=names(uid, gid))
    return plan


def op_plan(a):
    base_fd = open_abs(a["base"])
    try:
        return plan_put(base_fd, check_parts(a["parts"]))
    finally:
        os.close(base_fd)


def op_put(a):
    parts = check_parts(a["parts"])
    dparts, name = parts[:-1], parts[-1]
    base_fd = open_abs(a["base"])
    dfd = None
    tmp = None
    try:
        p = plan_put(base_fd, parts)
        uid, gid, mode = p["uid"], p["gid"], p["mode"]
        dfd, created, fixed = walk(base_fd, dparts, create={"uid": uid, "gid": gid, "mode": 0o755},
                                   fix={"uid": uid, "gid": gid})
        st = lst(dfd, name)
        if (st is not None) != p["exists"] or (st is not None and (
                st.st_ino != p["ino"] or st.st_uid != p["old_uid"] or st.st_gid != p["old_gid"]
                or st.st_mode != p["old_mode"])):
            raise HErr("changed", join(parts))
        vfs = os.fstatvfs(dfd)
        need = a["max_bytes"] + a.get("reserve", 0) + (p.get("old_size", 0) if p["exists"] else 0)
        if vfs.f_bavail * vfs.f_frsize < need:
            raise HErr("no_space")
        tmp = "." + name[:200] + ".mcp-" + os.urandom(4).hex() + ".tmp"
        tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                      0o600, dir_fd=dfd)
        try:
            size, digest, commit = read_frames(tfd, a["max_bytes"])
            if commit.get("sha256") != digest:
                raise HErr("transfer_mismatch")
            if a.get("sha256") and a["sha256"] != digest:
                raise HErr("sha256_mismatch")
            os.fchown(tfd, uid, gid)
            os.fchmod(tfd, mode)
            os.fsync(tfd)
        finally:
            os.close(tfd)
        backup = None
        if p["exists"]:
            backup = name + ".bak-" + time.strftime("%Y%m%d%H%M%S")
            copy_file(dfd, name, backup, uid, gid, mode, p["ino"])
            rename(dfd, tmp, dfd, name)
        else:
            rename_noreplace(dfd, tmp, dfd, name)
        tmp = None
        os.fsync(dfd)
        return {"size": size, "sha256": digest, "existed": p["exists"], "fix_owner": p["fix_owner"],
                "owner": p["owner"], "mode": "%04o" % mode, "created": created, "fixed": fixed,
                "backup": join(dparts + [backup]) if backup else None}
    finally:
        if tmp is not None and dfd is not None:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
        if dfd is not None:
            os.close(dfd)
        os.close(base_fd)


def mode_str(m):
    return stat.filemode(m)


def op_list(a):
    parts = check_parts(a["parts"], allow_empty=True)
    base_fd = open_abs(a["base"])
    out = []
    state = {"truncated": False}
    limit = a["max_entries"]

    def entry(r, s):
        if stat.S_ISLNK(s.st_mode):
            typ = "symlink"
        elif stat.S_ISDIR(s.st_mode):
            typ = "dir"
        elif stat.S_ISREG(s.st_mode):
            typ = "file"
        else:
            typ = "other"
        out.append({"path": r, "type": typ, "size": s.st_size, "mode": mode_str(s.st_mode),
                    "owner": names(s.st_uid, s.st_gid),
                    "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s.st_mtime))})
        return typ

    def rec(fd, r, depth):
        for c in sorted(os.listdir(fd)):
            if len(out) >= limit:
                state["truncated"] = True
                return
            s = lst(fd, c)
            if s is None:
                continue
            cr = (r + "/" + c) if r else c
            if entry(cr, s) == "dir" and depth > 1:
                cfd = open_dir(fd, c, cr)
                try:
                    rec(cfd, cr, depth - 1)
                finally:
                    os.close(cfd)
                if state["truncated"]:
                    return

    try:
        if parts:
            pfd, _, _ = walk(base_fd, parts[:-1])
            try:
                s = lst(pfd, parts[-1])
                if s is None:
                    raise HErr("not_found", join(parts))
                if not stat.S_ISDIR(s.st_mode):
                    entry(join(parts), s)
                    return {"entries": out, "truncated": False}
                dfd = open_dir(pfd, parts[-1], join(parts))
            finally:
                os.close(pfd)
        else:
            dfd = os.dup(base_fd)
        try:
            rec(dfd, join(parts), a["depth"])
        finally:
            os.close(dfd)
        return {"entries": out, "truncated": state["truncated"]}
    finally:
        os.close(base_fd)


def op_scan(a):
    parts = check_parts(a["parts"])
    base_fd = open_abs(a["base"])
    try:
        pfd, _, _ = walk(base_fd, parts[:-1])
        try:
            return scan(pfd, parts[-1], join(parts), a)
        finally:
            os.close(pfd)
    finally:
        os.close(base_fd)


def open_trash(base_fd, owner, create):
    st = lst(base_fd, TRASH)
    if st is None:
        if not create:
            return None
        os.mkdir(TRASH, 0o700, dir_fd=base_fd)
        fd = open_dir(base_fd, TRASH, TRASH)
        os.fchown(fd, owner["uid"], owner["gid"])
        os.fchmod(fd, 0o700)
        return fd
    return open_dir(base_fd, TRASH, TRASH)


def op_delete(a):
    parts = check_parts(a["parts"])
    name = parts[-1]
    base_fd = open_abs(a["base"])
    try:
        pfd, _, _ = walk(base_fd, parts[:-1])
        try:
            info = scan(pfd, name, join(parts), a)
            if info["type"] == "dir" and not a.get("digest"):
                raise HErr("token_required")
            if a.get("digest") and a["digest"] != info["digest"]:
                raise HErr("listing_changed")
            if info["has_exec"] and not a.get("allow_exec"):
                raise HErr("executable")
            if a["permanent"]:
                rm_tree(pfd, name)
                return {"permanent": True, "files": info["files"], "dirs": info["dirs"], "size": info["size"]}
            if info["size"] > a["trash_max_bytes"]:
                raise HErr("trash_too_large")
            owner = a["owner"]
            tfd = open_trash(base_fd, owner, True)
            try:
                ts = time.strftime("%Y%m%d%H%M%S")
                entry = ts
                n = 1
                while True:
                    try:
                        os.mkdir(entry, 0o700, dir_fd=tfd)
                        break
                    except FileExistsError:
                        n += 1
                        entry = ts + "-" + str(n)
                efd = open_dir(tfd, entry, entry)
                try:
                    os.fchown(efd, owner["uid"], owner["gid"])
                    edfd, _, _ = walk(efd, parts[:-1], create={"uid": owner["uid"], "gid": owner["gid"], "mode": 0o700})
                    try:
                        st = lst(pfd, name)
                        if st is None or st.st_ino != info["ino"]:
                            raise HErr("listing_changed")
                        rename_noreplace(pfd, name, edfd, name)
                    except BaseException:
                        os.close(edfd)
                        rm_tree(tfd, entry)
                        raise
                    os.close(edfd)
                    meta = json.dumps({"original_path": join(parts), "deleted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                       "type": info["type"], "files": info["files"], "size": info["size"]})
                    mfd = os.open(META, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                  0o600, dir_fd=efd)
                    try:
                        os.fchown(mfd, owner["uid"], owner["gid"])
                        os.write(mfd, meta.encode("utf-8"))
                        os.fsync(mfd)
                    finally:
                        os.close(mfd)
                finally:
                    os.close(efd)
            finally:
                os.close(tfd)
            return {"permanent": False, "trash_entry": entry, "files": info["files"], "dirs": info["dirs"],
                    "size": info["size"]}
        finally:
            os.close(pfd)
    finally:
        os.close(base_fd)


def op_move(a):
    src = check_parts(a["src"])
    dst = check_parts(a["dst"])
    base_fd = open_abs(a["base"])
    spfd = dpfd = None
    try:
        spfd, _, _ = walk(base_fd, src[:-1])
        info = scan(spfd, src[-1], join(src), a)
        if (info["has_exec"] or is_exec(dst[-1], a)) and not a.get("allow_exec"):
            raise HErr("executable")
        create = None
        if a.get("create_parents"):
            uid, gid = ref_owner(base_fd, dst[:-1])
            create = {"uid": uid, "gid": gid, "mode": 0o755}
        dpfd, created, _ = walk(base_fd, dst[:-1], create=create)
        dst_st = lst(dpfd, dst[-1])
        backup = None
        sst = lst(spfd, src[-1])
        if sst is None or sst.st_ino != info["ino"]:
            raise HErr("changed", join(src))
        if dst_st is None:
            rename_noreplace(spfd, src[-1], dpfd, dst[-1])
        else:
            if not a.get("overwrite"):
                raise HErr("exists", join(dst))
            if stat.S_ISLNK(dst_st.st_mode):
                raise HErr("target_symlink", join(dst))
            if not stat.S_ISREG(dst_st.st_mode) or stat.S_ISDIR(sst.st_mode):
                raise HErr("target_not_file", join(dst))
            backup = dst[-1] + ".bak-" + time.strftime("%Y%m%d%H%M%S")
            copy_file(dpfd, dst[-1], backup, dst_st.st_uid, dst_st.st_gid, stat.S_IMODE(dst_st.st_mode), dst_st.st_ino)
            rename(spfd, src[-1], dpfd, dst[-1])
            backup = join(dst[:-1] + [backup])
        os.fsync(dpfd)
        return {"created": created, "backup": backup, "type": info["type"], "files": info["files"]}
    finally:
        for fd in (spfd, dpfd):
            if fd is not None:
                os.close(fd)
        os.close(base_fd)


def read_meta(efd):
    try:
        mfd = os.open(META, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=efd)
    except OSError:
        raise HErr("trash_meta_missing")
    try:
        data = b""
        while len(data) < 65536:
            buf = os.read(mfd, 65536)
            if not buf:
                break
            data += buf
    finally:
        os.close(mfd)
    try:
        meta = json.loads(data.decode("utf-8"))
        parts = check_parts(meta["original_path"].split("/"))
    except (ValueError, KeyError, AttributeError, HErr):
        raise HErr("trash_meta_invalid")
    return meta, parts


def op_trash_info(a):
    base_fd = open_abs(a["base"])
    try:
        tfd = open_trash(base_fd, None, False)
        if tfd is None:
            raise HErr("trash_entry_missing")
        try:
            efd = open_dir(tfd, a["entry"], a["entry"])
            try:
                meta, _ = read_meta(efd)
                return {"meta": meta}
            finally:
                os.close(efd)
        except HErr as e:
            if e.code == "not_found":
                raise HErr("trash_entry_missing")
            raise
        finally:
            os.close(tfd)
    finally:
        os.close(base_fd)


def op_restore(a):
    base_fd = open_abs(a["base"])
    try:
        tfd = open_trash(base_fd, None, False)
        if tfd is None:
            raise HErr("trash_entry_missing")
        try:
            try:
                efd = open_dir(tfd, a["entry"], a["entry"])
            except HErr:
                raise HErr("trash_entry_missing")
            try:
                meta, parts = read_meta(efd)
                if join(parts) != a["expect_path"]:
                    raise HErr("changed")
                ipfd, _, _ = walk(efd, parts[:-1])
                try:
                    info = scan(ipfd, parts[-1], join(parts), a)
                    if info["has_exec"] and not a.get("allow_exec"):
                        raise HErr("executable")
                    tpfd, _, _ = walk(base_fd, parts[:-1])
                    try:
                        rename_noreplace(ipfd, parts[-1], tpfd, parts[-1])
                    finally:
                        os.close(tpfd)
                finally:
                    os.close(ipfd)
            finally:
                os.close(efd)
            rm_tree(tfd, a["entry"])
            return {"restored": join(parts), "type": info["type"], "files": info["files"]}
        finally:
            os.close(tfd)
    finally:
        os.close(base_fd)


def entry_age_days(name, now):
    try:
        t = time.mktime(time.strptime(name[:14], "%Y%m%d%H%M%S"))
    except ValueError:
        return None
    return (now - t) / 86400.0


def empty_trash(base_fd, days, dry):
    tfd = open_trash(base_fd, None, False)
    if tfd is None:
        return []
    removed = []
    now = time.time()
    try:
        for e in sorted(os.listdir(tfd)):
            if not ENTRY_RE.match(e):
                continue
            age = entry_age_days(e, now)
            if age is None or age < days:
                continue
            st = lst(tfd, e)
            if st is None or not stat.S_ISDIR(st.st_mode):
                continue
            meta = None
            try:
                efd = open_dir(tfd, e, e)
                try:
                    meta, _ = read_meta(efd)
                finally:
                    os.close(efd)
            except HErr:
                pass
            if not dry:
                rm_tree(tfd, e)
            removed.append({"entry": e, "original_path": (meta or {}).get("original_path"),
                            "size": (meta or {}).get("size")})
    finally:
        os.close(tfd)
    return removed


def op_empty_trash(a):
    base_fd = open_abs(a["base"])
    try:
        return {"removed": empty_trash(base_fd, a["days"], a.get("dry", False))}
    finally:
        os.close(base_fd)


def op_empty_trash_all(a):
    root_fd = open_abs(a["root"])
    result = {}
    try:
        for d in sorted(os.listdir(root_fd)):
            st = lst(root_fd, d)
            if st is None or not stat.S_ISDIR(st.st_mode):
                continue
            try:
                dfd = os.open(d, O_DIR, dir_fd=root_fd)
            except OSError:
                continue
            try:
                removed = empty_trash(dfd, a["days"], False)
            except (HErr, OSError):
                removed = []
            finally:
                os.close(dfd)
            if removed:
                result[d] = removed
        return {"domains": result}
    finally:
        os.close(root_fd)


BAK_RE = re.compile(r"\.bak-\d{14}$")
CTX_MAX = 120


def glob_seg_match(pats, names_):
    """Segmentweiser Glob-Abgleich, "**" = beliebig viele Ordnerebenen."""
    if not pats:
        return not names_
    if pats[0] == "**":
        return any(glob_seg_match(pats[1:], names_[i:]) for i in range(len(names_) + 1))
    return bool(names_) and fnmatch.fnmatchcase(names_[0], pats[0]) and glob_seg_match(pats[1:], names_[1:])


def glob_files(base_fd, pattern, limit):
    """Reguläre Dateien unter httpdocs, deren Pfad (relativ zum Vhost-Root) auf
    pattern passt. Symlinks werden weder verfolgt noch geliefert."""
    pats = pattern.split("/")
    out = []
    links = []
    count = [0]

    def rec(fd, rel):
        for c in sorted(os.listdir(fd)):
            count[0] += 1
            if count[0] > SCAN_MAX:
                raise HErr("too_many_entries")
            s = lst(fd, c)
            if s is None:
                continue
            r = rel + [c]
            if stat.S_ISLNK(s.st_mode):
                if glob_seg_match(pats, r):
                    links.append(join(r))
            elif stat.S_ISDIR(s.st_mode):
                if c == TRASH:
                    continue
                cfd = open_dir(fd, c, join(r))
                try:
                    rec(cfd, r)
                finally:
                    os.close(cfd)
            elif stat.S_ISREG(s.st_mode) and not BAK_RE.search(c) and ".mcp-" not in c:
                if glob_seg_match(pats, r):
                    out.append(r)
                    if len(out) > limit:
                        raise HErr("glob_too_many", str(limit))

    hfd = open_dir(base_fd, "httpdocs", "httpdocs")
    try:
        rec(hfd, ["httpdocs"])
    finally:
        os.close(hfd)
    return out, links


def read_payload(max_bytes):
    data = sys.stdin.buffer.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HErr("too_large")
    try:
        return json.loads(data.decode("utf-8"))
    except ValueError:
        raise HErr("incomplete")


def ctx_line(text, start, length):
    """Zeile(n) um text[start:start+length], gekürzt auf CTX_MAX Zeichen."""
    ls = text.rfind("\n", 0, start) + 1
    le = text.find("\n", start + length)
    if le < 0:
        le = len(text)
    line = text[ls:le].rstrip("\r")
    s = start - ls
    if len(line) > CTX_MAX:
        lo = max(0, min(s - (CTX_MAX - length) // 2, len(line) - CTX_MAX))
        line = ("..." if lo > 0 else "") + line[lo:lo + CTX_MAX] + ("..." if lo + CTX_MAX < len(line) else "")
    return line.replace("\r", "\\r").replace("\n", "\\n").replace("\t", " ")


def prepare_replace(base_fd, parts, old, new, replace_all, max_bytes, max_ctx):
    """Phase 1: lesen und prüfen, nichts schreiben."""
    rel = join(parts)
    p = plan_put(base_fd, parts)
    if not p["exists"]:
        raise HErr("not_found", rel)
    dfd, _, _ = walk(base_fd, parts[:-1])
    try:
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
    except OSError as e:
        os.close(dfd)
        if e.errno == errno.ELOOP:
            raise HErr("target_symlink", rel)
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_ino != p["ino"]:
            raise HErr("changed", rel)
        if st.st_size > max_bytes:
            raise HErr("file_too_large", rel)
        chunks = []
        size = 0
        while True:
            buf = os.read(fd, 1048576)
            if not buf:
                break
            size += len(buf)
            if size > max_bytes:
                raise HErr("file_too_large", rel)
            chunks.append(buf)
        data = b"".join(chunks)
    finally:
        os.close(fd)
        os.close(dfd)
    if b"\0" in data:
        raise HErr("binary", rel)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise HErr("not_utf8", rel)
    o, n = old, new
    # Zeilenenden beibehalten: reine CRLF-Datei, Suchtext mit "\n" -> "\r\n"
    crlf = "\r\n" in text and text.count("\n") == text.count("\r\n")
    if crlf and "\r" not in o and "\n" in o:
        o = o.replace("\n", "\r\n")
        if "\r" not in n:
            n = n.replace("\n", "\r\n")
    count = text.count(o)
    res = {"path": rel, "parts": parts, "count": count}
    if count == 0:
        return res
    if count > 1 and not replace_all:
        raise HErr("ambiguous", "%s|%d" % (rel, count))
    starts = []
    i = text.find(o)
    while i >= 0:
        starts.append(i)
        i = text.find(o, i + len(o))
    new_text = text.replace(o, n) if replace_all else text.replace(o, n, 1)
    out = new_text.encode("utf-8")
    if len(out) > max_bytes:
        raise HErr("file_too_large", rel)
    ctx = []
    delta = len(n) - len(o)
    for k, s in enumerate(starts[:max_ctx]):
        ctx.append({"vorher": ctx_line(text, s, len(o)), "nachher": ctx_line(new_text, s + k * delta, len(n))})
    res.update(plan=p, st=st, data=data, out=out, ctx=ctx, crlf=crlf)
    return res


def write_tmp(dfd, name, data, uid, gid, mode):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
        while data:
            w = os.write(fd, data)
            data = data[w:]
        os.fsync(fd)
    finally:
        os.close(fd)


def op_replace(a):
    """Ersetzt old durch new in einer oder mehreren Textdateien. Zuerst werden
    alle Dateien gelesen und geprüft, dann alle Temp-Dateien und Backups
    geschrieben, erst danach wird umbenannt - ein Fehler vor dem Umbenennen
    lässt alle Dateien unverändert."""
    pay = read_payload(a["max_payload"])
    old, new = pay.get("old"), pay.get("new")
    if not isinstance(old, str) or not old or not isinstance(new, str):
        raise HErr("bad_request")
    base_fd = open_abs(a["base"])
    try:
        links = []
        if a.get("glob"):
            targets, links = glob_files(base_fd, a["glob"], a["max_files"])
        else:
            targets = [check_parts(p) for p in a["paths"]]
        if not targets:
            raise HErr("glob_empty")
        if len(set(join(p) for p in targets)) != len(targets):
            raise HErr("duplicate_path")
        if not a.get("allow_exec") and any(is_exec(p[-1], a) for p in targets):
            raise HErr("executable")
        items = [prepare_replace(base_fd, p, old, new, bool(a["replace_all"]), a["max_bytes"], a["max_ctx"])
                 for p in targets]
        todo = [it for it in items if it["count"]]
        skipped = [it["path"] for it in items if not it["count"]]
        if not todo:
            return {"files": [], "skipped": skipped, "symlinks": links}
        need = sum(len(it["out"]) + len(it["data"]) for it in todo)
        now = time.time()
        created = []
        try:
            for it in todo:
                parts = it["parts"]
                p = it["plan"]
                dfd, _, _ = walk(base_fd, parts[:-1])
                it["dfd"] = dfd
                vfs = os.fstatvfs(dfd)
                if vfs.f_bavail * vfs.f_frsize < need + a.get("reserve", 0):
                    raise HErr("no_space")
                name = parts[-1]
                it["tmp"] = "." + name[:200] + ".mcp-" + os.urandom(4).hex() + ".tmp"
                write_tmp(dfd, it["tmp"], it["out"], p["uid"], p["gid"], p["mode"])
                created.append((dfd, it["tmp"]))
                # Mehrere Ersetzungen in derselben Sekunde: nächsten freien
                # Zeitstempel nehmen, damit kein Zwischenstand verloren geht.
                for k in range(10):
                    it["backup"] = name + ".bak-" + time.strftime("%Y%m%d%H%M%S", time.localtime(now + k))
                    try:
                        write_tmp(dfd, it["backup"], it["data"], p["uid"], p["gid"], p["mode"])
                        break
                    except OSError as e:
                        if e.errno != errno.EEXIST:
                            raise
                else:
                    raise HErr("exists", join(parts[:-1] + [it["backup"]]))
                created.append((dfd, it["backup"]))
            for it in todo:
                st = lst(it["dfd"], it["parts"][-1])
                o = it["st"]
                if st is None or (st.st_ino, st.st_size, st.st_mtime_ns, st.st_uid, st.st_gid, st.st_mode) != (
                        o.st_ino, o.st_size, o.st_mtime_ns, o.st_uid, o.st_gid, o.st_mode):
                    raise HErr("changed", it["path"])
        except BaseException:
            for dfd, nm in created:
                try:
                    os.unlink(nm, dir_fd=dfd)
                except OSError:
                    pass
            for it in todo:
                if "dfd" in it:
                    os.close(it.pop("dfd"))
            raise
        done = []
        try:
            for it in todo:
                rename(it["dfd"], it["tmp"], it["dfd"], it["parts"][-1])
                os.fsync(it["dfd"])
                done.append(it)
        except BaseException:
            for it in todo:
                if it not in done:
                    try:
                        os.unlink(it["tmp"], dir_fd=it["dfd"])
                    except OSError:
                        pass
            if done:
                raise HErr("partial", ", ".join(it["path"] for it in done))
            raise
        finally:
            for it in todo:
                os.close(it["dfd"])
        files = []
        for it in todo:
            p = it["plan"]
            files.append({"path": it["path"], "replacements": it["count"], "size": len(it["out"]),
                          "old_size": len(it["data"]), "backup": join(it["parts"][:-1] + [it["backup"]]),
                          "owner": p["owner"], "mode": "%04o" % p["mode"], "fix_owner": p["fix_owner"],
                          "crlf": it["crlf"], "context": it["ctx"]})
        return {"files": files, "skipped": skipped, "symlinks": links}
    finally:
        os.close(base_fd)


OPS = {"plan": op_plan, "put": op_put, "list": op_list, "scan": op_scan, "delete": op_delete,
       "move": op_move, "trash_info": op_trash_info, "restore": op_restore,
       "empty_trash": op_empty_trash, "empty_trash_all": op_empty_trash_all, "replace": op_replace}


def main():
    os.umask(0o077)
    try:
        op = OPS[sys.argv[1]]
        args = json.loads(base64.b64decode(sys.argv[2]).decode("utf-8"))
    except (IndexError, KeyError, ValueError):
        res = {"ok": False, "error": "bad_request", "detail": ""}
    else:
        try:
            res = op(args)
            res["ok"] = True
        except HErr as e:
            res = {"ok": False, "error": e.code, "detail": e.detail}
        except OSError as e:
            res = {"ok": False, "error": "os_error", "detail": errno.errorcode.get(e.errno, "") if e.errno else ""}
        except Exception as e:
            res = {"ok": False, "error": "internal", "detail": type(e).__name__}
    sys.stdout.write(json.dumps(res) + "\n")
    sys.stdout.flush()


main()
'''


class _HelperError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


_HELPER_MESSAGES = {
    "bad_path": "Ungültiger Pfad.",
    "base_unavailable": "Vhost-Verzeichnis nicht gefunden oder kein regulärer Ordner.",
    "not_found": "Nicht gefunden: {detail}",
    "symlink_in_path": "Symlink oder kein Ordner im Pfad ({detail}) - aus Sicherheitsgründen abgelehnt.",
    "target_symlink": "Ziel '{detail}' ist ein Symlink - wird nicht überschrieben.",
    "target_not_file": "Ziel '{detail}' ist keine reguläre Datei.",
    "target_root_outside_httpdocs": "'{detail}' gehört root und liegt ausserhalb von httpdocs - wird nicht überschrieben (Ergebnis wäre root-eigen).",
    "owner_root": "Referenzordner, httpdocs und Vhost-Root gehören root - Subscription-User für den Besitzer nicht ermittelbar, es wurde nichts geschrieben.",
    "owner_unknown": "Besitzer des Referenzordners ist kein bekannter System-User.",
    "psaserv_missing": "Gruppe psaserv nicht gefunden.",
    "changed": "'{detail}' wurde während der Aktion verändert - abgebrochen.",
    "no_space": "Zu wenig freier Speicherplatz auf dem Server.",
    "incomplete": "Übertragung unvollständig - nichts geschrieben.",
    "too_large": "Grössenlimit überschritten - nichts geschrieben.",
    "transfer_mismatch": "Prüfsumme nach der Übertragung stimmt nicht - nichts geschrieben.",
    "sha256_mismatch": "sha256 stimmt nicht mit dem erwarteten Wert überein - Datei nicht ersetzt.",
    "exists": "Ziel '{detail}' existiert bereits.",
    "cross_device": "Quelle und Ziel liegen auf verschiedenen Dateisystemen - kein atomares Verschieben möglich.",
    "invalid_move": "Ungültiges Verschieben (z.B. Ordner in sich selbst).",
    "token_required": "Ordner nur zweistufig: zuerst dry_run=true, dann mit delete_token löschen.",
    "listing_changed": "Inhalt hat sich seit dem Dry-Run geändert - abgebrochen. Neuen Dry-Run ausführen.",
    "executable": "Enthält ausführbare Dateien (.php, .htaccess, ...) - nur mit allow_executable=true.",
    "trash_too_large": "Zu gross für den Papierkorb - für endgültiges Löschen permanent=true verwenden.",
    "too_many_entries": "Zu viele Einträge für eine einzelne Aktion.",
    "trash_entry_missing": "Papierkorb-Eintrag nicht gefunden.",
    "trash_meta_missing": "Metadaten des Papierkorb-Eintrags fehlen.",
    "trash_meta_invalid": "Metadaten des Papierkorb-Eintrags sind ungültig.",
    "bad_request": "Interner Fehler im Datei-Helper.",
    "os_error": "Dateisystemfehler ({detail}).",
    "internal": "Interner Fehler im Datei-Helper.",
    "file_too_large": "'{detail}' ist grösser als das Limit für Textersetzungen - nichts geändert.",
    "binary": "'{detail}' enthält Nullbytes (Binärdatei) - nichts geändert.",
    "not_utf8": "'{detail}' ist nicht UTF-8-dekodierbar - nichts geändert.",
    "glob_too_many": "glob passt auf mehr als {detail} Dateien - Muster einschränken, nichts geändert.",
    "glob_empty": "glob passt auf keine Datei - nichts geändert.",
    "duplicate_path": "Doppelte Datei in der Liste - nichts geändert.",
    "partial": "Fehler beim Umbenennen - bereits ersetzt (Backups vorhanden): {detail}. Übrige Dateien unverändert.",
}


def _helper_message(e: _HelperError) -> str:
    return _HELPER_MESSAGES.get(e.code, "Fehler im Datei-Helper.").format(detail=e.detail)


class _SSHHelperSession:
    """Startet den Helper per SSH auf dem Plesk-Server."""

    def __init__(self, op: str, args: dict[str, Any]):
        payload = base64.b64encode(json.dumps(args).encode("utf-8")).decode("ascii")
        cmd = (
            f"{shlex.quote(_HELPER_PYTHON)} -I -c {shlex.quote(_HELPER_SRC)} "
            f"{shlex.quote(op)} {payload}"
        )
        self.client = _ssh_connect()
        try:
            self.chan = self.client.get_transport().open_session()
            self.chan.settimeout(_HELPER_TIMEOUT)
            self.chan.exec_command(cmd)
        except Exception:
            self.client.close()
            raise

    def send(self, data: bytes) -> None:
        self.chan.sendall(data)

    def finish(self) -> bytes:
        try:
            self.chan.shutdown_write()
            out = b""
            while True:
                buf = self.chan.recv(65536)
                if not buf:
                    break
                out += buf
            self.chan.recv_exit_status()
            return out
        finally:
            self.client.close()

    def abort(self) -> None:
        self.client.close()


# In Tests durch eine lokale Variante ersetzbar.
_helper_session_factory = _SSHHelperSession


def _helper_result(raw: bytes) -> dict[str, Any]:
    for line in reversed(raw.decode("utf-8", errors="replace").strip().splitlines()):
        try:
            res = json.loads(line)
        except ValueError:
            continue
        if isinstance(res, dict) and "ok" in res:
            if not res["ok"]:
                raise _HelperError(res.get("error", "internal"), res.get("detail", ""))
            return res
    raise _HelperError("internal")


def _helper_args(base: str, **kw: Any) -> dict[str, Any]:
    return {"base": base, "exec_suffixes": _EXEC_SUFFIXES, "exec_names": _EXEC_NAMES, **kw}


def _helper(op: str, args: dict[str, Any]) -> dict[str, Any]:
    session = _helper_session_factory(op, args)
    return _helper_result(session.finish())


def _frame(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def _helper_put(args: dict[str, Any], chunks) -> dict[str, Any]:
    """Streamt chunks (Iterable[bytes]) an den Helper (op put). Bricht bei
    Überschreitung von max_bytes ab, ohne etwas zu schreiben."""
    session = _helper_session_factory("put", args)
    h = hashlib.sha256()
    size = 0
    try:
        for chunk in chunks:
            if not chunk:
                continue
            size += len(chunk)
            if size > args["max_bytes"]:
                raise _HelperError("too_large")
            h.update(chunk)
            for i in range(0, len(chunk), 1048576):
                session.send(_frame(chunk[i:i + 1048576]))
        session.send(_frame(b"") + json.dumps({"sha256": h.hexdigest()}).encode() + b"\n")
    except BaseException:
        session.abort()
        raise
    return _helper_result(session.finish())


def _audit(action: str, **fields: Any) -> None:
    """Audit-Log pro Aktion - nie Tokens, Passwörter oder Dateiinhalte."""
    entry = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
             "action": action, **fields}
    line = json.dumps(entry, ensure_ascii=False)
    print(f"AUDIT {line}", file=sys.stderr, flush=True)
    if _AUDIT_LOG_FILE:
        try:
            with open(_AUDIT_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def _strict_parts(path: str, allow_root: bool = False) -> list[str]:
    """Relativer Pfad ohne "..", ".", absolute Pfade oder Wildcards."""
    p = (path or "").strip()
    if p in ("", ".") and allow_root:
        return []
    if not p or p.startswith("/") or "\\" in p or "\0" in p or len(p) > 4096:
        raise ValueError(f"Ungültiger Pfad {path!r}: nur relative Pfade (z.B. httpdocs/bild.jpg).")
    if _GLOB_CHARS & set(p):
        raise ValueError(f"Ungültiger Pfad {path!r}: Wildcards/Globs sind nicht erlaubt.")
    parts = p.rstrip("/").split("/")
    for c in parts:
        if c in ("", ".", ".."):
            raise ValueError(f"Ungültiger Pfad {path!r}: '..', '.' und leere Teile sind nicht erlaubt.")
        if len(c.encode("utf-8")) > 255:
            raise ValueError(f"Ungültiger Pfad {path!r}: Name zu lang.")
    return parts


def _is_executable_name(name: str) -> bool:
    n = name.lower()
    return n in _EXEC_NAMES or any(s in _EXEC_SUFFIXES for s in n.split(".")[1:])


def _protected_reason(parts: list[str], docroots: list[list[str]]) -> str | None:
    """Grund, warum ein Pfad (relativ zum Vhost-Root) geschützt ist, sonst None."""
    if not parts:
        return "das Vhost-Root"
    if parts in docroots:
        return "ein Docroot"
    top = parts[0]
    if top.lower() in _PROTECTED_TOP:
        return f"im geschützten Bereich '{top}'"
    if top.startswith("."):
        return "eine Dotfile/ein Dot-Ordner im Vhost-Root"
    if parts[-1] == "cgi-bin" and (len(parts) == 1 or parts[:-1] in docroots):
        return "cgi-bin"
    return None


def _check_not_protected(parts: list[str], ctx: dict[str, Any], what: str) -> None:
    reason = _protected_reason(parts, ctx["docroots"])
    if reason:
        raise ValueError(f"{what} '{'/'.join(parts) or '.'}' ist {reason} und geschützt - abgelehnt.")


def _vhost_ctx(domain: str) -> dict[str, Any]:
    """Subscription-Daten aus der Plesk-DB: Vhost-Root, System-User und alle
    Docroots der Subscription (relativ zum Vhost-Root)."""
    d = _wp_domain(domain)
    base = f"{_VHOST_BASE}/{d}"
    client = _ssh_connect()
    try:
        sql = (
            "SELECT d.id, d.webspace_id, s.login, s.home FROM domains d "
            "JOIN hosting h ON h.dom_id = d.id JOIN sys_users s ON s.id = h.sys_user_id "
            f"WHERE d.name = {_sql_quote(d)}"
        )
        out, _, rc = _client_exec(client, f"plesk db -Ne {shlex.quote(sql)}")
        rows = [r.split("\t") for r in out.strip().splitlines() if r.strip()]
        if rc != 0 or len(rows) != 1 or len(rows[0]) != 4:
            raise ValueError(f"Domain '{d}' ist in Plesk nicht als Hosting vorhanden.")
        dom_id, webspace_id, login, home = (c.strip() for c in rows[0])
        if not dom_id.isdigit() or home != base or webspace_id not in ("0", "", "NULL"):
            raise ValueError(
                f"'{d}' ist keine Subscription mit eigenem Vhost-Verzeichnis - die "
                "Hauptdomain der Subscription angeben."
            )
        if not _WP_SYSUSER_RE.match(login):
            raise ValueError(f"Unerwarteter System-User für '{d}'.")
        sql = (
            "SELECT d.id, h.www_root FROM domains d JOIN hosting h ON h.dom_id = d.id "
            f"WHERE d.id = {int(dom_id)} OR d.webspace_id = {int(dom_id)}"
        )
        out, _, rc = _client_exec(client, f"plesk db -Ne {shlex.quote(sql)}")
        docroots = []
        main_docroot = None
        for line in out.strip().splitlines():
            rid, _, r = line.partition("\t")
            r = posixpath.normpath(r.strip())
            if r.startswith(base + "/"):
                docroots.append(r[len(base) + 1:].split("/"))
                if rid.strip() == dom_id:
                    main_docroot = docroots[-1]
        out, _, rc = _client_exec(client, f"id -u {shlex.quote(login)} && id -g {shlex.quote(login)}")
        ids = out.split()
        if rc != 0 or len(ids) != 2 or not all(i.isdigit() for i in ids) or "0" in ids:
            raise ValueError(f"System-User von '{d}' nicht ermittelbar oder root.")
    finally:
        client.close()
    if not docroots:
        raise ValueError(f"Kein Docroot für '{d}' gefunden.")
    return {"domain": d, "base": base, "login": login, "uid": int(ids[0]), "gid": int(ids[1]),
            "docroots": docroots, "main_docroot": main_docroot}


def _docroot_of(parts: list[str], docroots: list[list[str]]) -> list[str] | None:
    """Längstes Docroot, unter dem parts (echt) liegt."""
    best = None
    for dr in docroots:
        if len(parts) > len(dr) and parts[:len(dr)] == dr and (best is None or len(dr) > len(best)):
            best = dr
    return best


def _check_executable(parts: list[str], allow_executable: bool) -> None:
    if _is_executable_name(parts[-1]) and not allow_executable:
        raise ValueError(
            f"'{parts[-1]}' ist ausführbar (.php, .htaccess, ...) - nur mit allow_executable=true."
        )


def _call(op: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        return _helper(op, args)
    except _HelperError as e:
        raise ValueError(_helper_message(e))


# --- Upload (Token + HTTP-Endpunkt) -----------------------------------------

_uploads: dict[str, dict[str, Any]] = {}
_uploads_lock = threading.Lock()
_upload_active = 0
_upload_rate: dict[str, list[float]] = {}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def _upload_gc(now: float) -> None:
    for uid, rec in list(_uploads.items()):
        if rec["status"] == "pending" and now > rec["expires"]:
            rec["status"] = "expired"
        if now - rec["created"] > 86400:
            del _uploads[uid]


def _check_sha(sha256: str | None) -> str | None:
    if sha256 is None or sha256 == "":
        return None
    s = sha256.strip().lower()
    if not _SHA256_RE.match(s):
        raise ValueError("sha256 muss 64 Hex-Zeichen sein.")
    return s


def _limit_bytes(max_bytes: int | None) -> int:
    if max_bytes is None:
        return _UPLOAD_MAX_BYTES
    if not isinstance(max_bytes, int) or max_bytes < 1 or max_bytes > _UPLOAD_MAX_BYTES:
        raise ValueError(f"max_bytes muss zwischen 1 und {_UPLOAD_MAX_BYTES} liegen.")
    return max_bytes


def _prepare_write(domain: str, path: str, allow_executable: bool) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    ctx = _vhost_ctx(domain)
    parts = _strict_parts(path)
    _check_not_protected(parts, ctx, "Ziel")
    _check_executable(parts, allow_executable)
    plan = _call("plan", _helper_args(ctx["base"], parts=parts))
    return ctx, parts, plan


@mcp.tool()
def upload_begin(
    domain: str,
    path: str,
    max_bytes: int | None = None,
    sha256: str | None = None,
    allow_executable: bool = False,
    confirm: bool = False,
) -> str:
    """Bereitet einen Datei-Upload direkt in das Vhost-Verzeichnis vor - für
    Binärdateien (Bilder, Schriften, ZIPs), ohne dass der Inhalt durch den
    Chat läuft. Gibt upload_id, Upload-URL und ein einmaliges Token (5 Minuten
    gültig) zurück; danach lädt der Client direkt hoch:
    curl -X PUT -H "Authorization: Bearer <token>" --data-binary @datei <url>

    path relativ zu /var/www/vhosts/<domain>/ (z.B. "httpdocs/img/logo.png").
    max_bytes verkleinert das Server-Limit, sha256 (optional) wird nach dem
    Upload geprüft - bei Abweichung wird nichts ersetzt. Ausführbare Dateien
    (.php, .htaccess, ...) nur mit allow_executable=true. Bestehende Dateien
    werden als <path>.bak-<Zeitstempel> gesichert. Erfordert confirm=true.
    Status danach mit upload_status(upload_id) prüfen.
    """
    if not confirm:
        raise ValueError("confirm=true erforderlich - der Upload schreibt auf dem Produktivserver.")
    if not _PUBLIC_BASE_URL:
        raise ValueError("Upload nur mit gesetzter PUBLIC_BASE_URL verfügbar.")
    limit = _limit_bytes(max_bytes)
    sha = _check_sha(sha256)
    ctx, parts, plan = _prepare_write(domain, path, allow_executable)
    upload_id = secrets.token_urlsafe(16)
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _uploads_lock:
        _upload_gc(now)
        if sum(1 for r in _uploads.values() if r["status"] == "pending") >= 50:
            raise ValueError("Zu viele offene Uploads - später erneut versuchen.")
        _uploads[upload_id] = {
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "domain": ctx["domain"], "base": ctx["base"], "parts": parts,
            "max_bytes": limit, "sha256": sha, "created": now, "expires": now + _UPLOAD_TOKEN_TTL,
            "status": "pending", "size": None, "sha256_actual": None, "error": None,
        }
    url = f"{_PUBLIC_BASE_URL}/upload/{upload_id}"
    _audit("upload_begin", domain=ctx["domain"], path="/".join(parts), max_bytes=limit,
           sha256=sha, upload_id=upload_id, result="ok")
    return _json({
        "upload_id": upload_id,
        "upload_url": url,
        "method": "PUT",
        "token": token,
        "expires_at": datetime.datetime.fromtimestamp(now + _UPLOAD_TOKEN_TTL, datetime.timezone.utc).isoformat(timespec="seconds"),
        "max_bytes": limit,
        "path": "/".join(parts),
        "ueberschreibt": plan["exists"],
        "besitzer": plan["owner"],
        "curl": f'curl -X PUT -H "Authorization: Bearer {token}" --data-binary @<datei> {url}',
        "hinweis": "Token ist einmalig und 5 Minuten gültig. Danach upload_status(upload_id) aufrufen.",
    })


@mcp.tool()
def upload_status(upload_id: str) -> str:
    """Status eines Uploads: pending/uploading/completed/expired/failed, mit
    path, size und sha256 - zur Kontrolle nach dem Upload."""
    with _uploads_lock:
        _upload_gc(time.time())
        rec = _uploads.get(upload_id.strip())
        if rec is None:
            raise ValueError("Unbekannte upload_id (oder älter als 24 Stunden / Server neu gestartet).")
        return _json({
            "upload_id": upload_id.strip(), "status": rec["status"], "domain": rec["domain"],
            "path": "/".join(rec["parts"]), "size": rec["size"], "sha256": rec["sha256_actual"],
            "fehler": rec["error"],
        })


def _client_ip(scope: dict[str, Any]) -> str:
    peer = (scope.get("client") or ("", 0))[0]
    if peer in _UPLOAD_TRUSTED_PROXIES:
        for k, v in scope.get("headers", []):
            if k == b"x-forwarded-for":
                hops = [h.strip() for h in v.decode("latin-1").split(",") if h.strip()]
                if hops:
                    return hops[-1]
    return peer


def _rate_limited(ip: str, now: float) -> bool:
    with _uploads_lock:
        hits = [t for t in _upload_rate.get(ip, []) if now - t < 60]
        hits.append(now)
        _upload_rate[ip] = hits
        if len(_upload_rate) > 10000:
            for k in [k for k, v in _upload_rate.items() if now - v[-1] > 60]:
                del _upload_rate[k]
        return len(hits) > _UPLOAD_RATE_LIMIT


async def _upload_asgi(scope, receive, send) -> None:
    """PUT /upload/{upload_id} - eigenes Einmal-Token im Authorization-Header."""
    import asyncio
    from starlette.responses import JSONResponse

    async def reply(status: int, body: dict[str, Any]) -> None:
        await JSONResponse(body, status_code=status)(scope, receive, send)

    now = time.time()
    if scope["method"] != "PUT":
        await reply(405, {"error": "method_not_allowed"})
        return
    upload_id = scope["path"][len("/upload/"):]
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    auth = headers.get("authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    global _upload_active
    with _uploads_lock:
        _upload_gc(now)
        rec = _uploads.get(upload_id) if _UPLOAD_ID_RE.match(upload_id) else None
        ok = (
            rec is not None and token
            and hmac.compare_digest(rec["token_hash"], hashlib.sha256(token.encode()).hexdigest())
            and rec["status"] == "pending" and now <= rec["expires"]
        )
        if ok and _upload_active >= _UPLOAD_MAX_CONCURRENT:
            busy = True
        else:
            busy = False
            if ok:
                rec["status"] = "uploading"
                _upload_active += 1
    if not ok:
        # Nur Fehlversuche zaehlen fuers Rate-Limit: Ein gueltiger Upload wird
        # damit nie durch fremde Fehlversuche aus demselben (Proxy-)Bucket
        # blockiert; Token-Raten bleibt trotzdem gedrosselt.
        if _rate_limited(_client_ip(scope), now):
            await reply(429, {"error": "too_many_requests"})
            return
        await reply(401, {"error": "unauthorized"})
        return
    if busy:
        await reply(429, {"error": "too_many_uploads"})
        return

    result_status, body = 500, {"error": "upload_failed"}
    try:
        length = headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > rec["max_bytes"]):
            raise _HelperError("too_large")
        args = _helper_args(rec["base"], parts=rec["parts"], max_bytes=rec["max_bytes"],
                            sha256=rec["sha256"], reserve=_UPLOAD_MIN_FREE)
        session = await asyncio.to_thread(_helper_session_factory, "put", args)
        h = hashlib.sha256()
        size = 0
        deadline = time.monotonic() + _UPLOAD_TIMEOUT
        try:
            while True:
                msg = await asyncio.wait_for(receive(), timeout=max(1.0, deadline - time.monotonic()))
                if msg["type"] == "http.disconnect":
                    raise _HelperError("incomplete")
                chunk = msg.get("body", b"")
                if chunk:
                    size += len(chunk)
                    if size > rec["max_bytes"]:
                        raise _HelperError("too_large")
                    h.update(chunk)
                    await asyncio.to_thread(session.send, _frame(chunk))
                if not msg.get("more_body", False):
                    break
            await asyncio.to_thread(
                session.send, _frame(b"") + json.dumps({"sha256": h.hexdigest()}).encode() + b"\n"
            )
        except BaseException:
            await asyncio.to_thread(session.abort)
            raise
        res = _helper_result(await asyncio.to_thread(session.finish))
        rec.update(status="completed", size=res["size"], sha256_actual=res["sha256"])
        result_status = 200
        body = {"domain": rec["domain"], "path": "/".join(rec["parts"]), "size": res["size"],
                "sha256": res["sha256"], "backup": res.get("backup")}
    except _HelperError as e:
        rec.update(status="failed", error=_helper_message(e))
        result_status = {"too_large": 413, "sha256_mismatch": 422, "transfer_mismatch": 422,
                         "no_space": 507, "incomplete": 400}.get(e.code, 409)
        body = {"error": e.code}
    except asyncio.TimeoutError:
        rec.update(status="failed", error="Zeitüberschreitung beim Upload.")
        result_status, body = 408, {"error": "timeout"}
    except Exception as e:
        rec.update(status="failed", error="Interner Fehler beim Upload.")
        print(f"upload {upload_id}: {type(e).__name__}", file=sys.stderr, flush=True)
    finally:
        with _uploads_lock:
            _upload_active -= 1
    _audit("upload", domain=rec["domain"], path="/".join(rec["parts"]), upload_id=upload_id,
           size=rec["size"], sha256=rec["sha256_actual"], result=rec["status"], error=rec["error"])
    await reply(result_status, body)


# --- Fetch (Server lädt selbst von einer URL) -------------------------------


def _ip_is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return bool(addr.is_global) and not addr.is_multicast


def _resolve_host(host: str, port: int) -> list[str]:
    import socket
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({i[4][0] for i in infos})


def _resolve_public(host: str, port: int) -> str:
    """Löst host auf; alle Adressen müssen öffentlich sein (SSRF-Schutz)."""
    try:
        ips = _resolve_host(host, port)
    except OSError:
        raise ValueError(f"Host '{host}' nicht auflösbar.")
    if not ips:
        raise ValueError(f"Host '{host}' nicht auflösbar.")
    bad = [ip for ip in ips if not _ip_is_public(ip)]
    if bad:
        raise ValueError(f"Host '{host}' zeigt auf eine nicht-öffentliche Adresse - abgelehnt (SSRF-Schutz).")
    return ips[0]


def _parse_fetch_url(url: str):
    from urllib.parse import urlsplit
    u = urlsplit(url.strip())
    if u.scheme != "https" or not u.hostname:
        raise ValueError("Nur https-URLs sind erlaubt.")
    if u.username or u.password:
        raise ValueError("Zugangsdaten in der URL sind nicht erlaubt.")
    return u


def _fetch_client():
    # trust_env=False: keine Proxy-Umgebungsvariablen - die Verbindung geht
    # immer direkt an die geprüfte, gepinnte IP.
    return httpx.Client(timeout=httpx.Timeout(_FETCH_TIMEOUT, connect=10.0), follow_redirects=False,
                        trust_env=False)


def _fetch_chunks(url: str, limit: int, state: dict[str, Any]):
    """Lädt url mit an die geprüfte IP gepinnter Verbindung (gegen DNS-
    Rebinding), max. 3 Redirects, jede Station erneut geprüft."""
    from urllib.parse import urljoin
    current = url
    deadline = time.monotonic() + _FETCH_TIMEOUT
    with _fetch_client() as client:
        for hop in range(_FETCH_MAX_REDIRECTS + 1):
            u = _parse_fetch_url(current)
            port = u.port or 443
            ip = _resolve_public(u.hostname, port)
            ip_host = f"[{ip}]" if ":" in ip else ip
            target = f"https://{ip_host}:{port}{u.path or '/'}" + (f"?{u.query}" if u.query else "")
            host_header = u.hostname if port == 443 else f"{u.hostname}:{port}"
            req = client.build_request("GET", target, headers={"Host": host_header},
                                       extensions={"sni_hostname": u.hostname})
            resp = client.send(req, stream=True)
            try:
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("location")
                    if not loc:
                        raise ValueError("Redirect ohne Ziel.")
                    if hop == _FETCH_MAX_REDIRECTS:
                        raise ValueError("Zu viele Redirects (max. 3).")
                    current = urljoin(current, loc)
                    continue
                if resp.status_code != 200:
                    raise ValueError(f"Download fehlgeschlagen: HTTP {resp.status_code}.")
                cl = resp.headers.get("content-length")
                if cl and cl.isdigit() and int(cl) > limit:
                    raise _HelperError("too_large")
                state["final_url"] = current
                for chunk in resp.iter_bytes(65536):
                    if time.monotonic() > deadline:
                        raise ValueError("Zeitüberschreitung beim Download.")
                    yield chunk
                return
            finally:
                resp.close()
    raise ValueError("Zu viele Redirects (max. 3).")


@mcp.tool()
def fetch_to_vhost(
    domain: str,
    path: str,
    url: str,
    sha256: str | None = None,
    allow_executable: bool = False,
    confirm: bool = False,
) -> str:
    """Der Server lädt eine Datei selbst von einer https-URL und legt sie im
    Vhost-Verzeichnis ab (path relativ zu /var/www/vhosts/<domain>/). Max. 3
    Redirects, Grössenlimit UPLOAD_MAX_BYTES, SSRF-Schutz (nur öffentliche
    IPs, je Station geprüft und gepinnt). Dieselbe Schreiblogik wie
    write_vhost_file/Upload: Temp-Datei, optional sha256-Prüfung, atomar,
    Backup bestehender Dateien, Besitzer der Subscription. Ausführbare
    Dateien nur mit allow_executable=true. Erfordert confirm=true.
    """
    if not confirm:
        raise ValueError("confirm=true erforderlich - fetch_to_vhost schreibt auf dem Produktivserver.")
    _parse_fetch_url(url)
    sha = _check_sha(sha256)
    ctx, parts, _plan = _prepare_write(domain, path, allow_executable)
    state: dict[str, Any] = {}
    args = _helper_args(ctx["base"], parts=parts, max_bytes=_UPLOAD_MAX_BYTES, sha256=sha,
                        reserve=_UPLOAD_MIN_FREE)
    try:
        res = _helper_put(args, _fetch_chunks(url, _UPLOAD_MAX_BYTES, state))
    except _HelperError as e:
        _audit("fetch", domain=ctx["domain"], path="/".join(parts), url=url, result="failed", error=e.code)
        raise ValueError(_helper_message(e))
    except (ValueError, httpx.HTTPError) as e:
        msg = str(e) if isinstance(e, ValueError) else f"Download fehlgeschlagen ({type(e).__name__})."
        _audit("fetch", domain=ctx["domain"], path="/".join(parts), url=url, result="failed", error=msg)
        raise ValueError(msg)
    _audit("fetch", domain=ctx["domain"], path="/".join(parts), url=url, size=res["size"],
           sha256=res["sha256"], result="ok")
    return _json({"domain": ctx["domain"], "path": "/".join(parts), "size": res["size"],
                  "sha256": res["sha256"], "besitzer": res["owner"], "rechte": res["mode"],
                  "backup": res["backup"], "neu_angelegte_ordner": res["created"]})


# --- Dateiverwaltung ---------------------------------------------------------


@mcp.tool()
def list_vhost_dir(domain: str, path: str = ".", depth: int = 1) -> str:
    """Listet ein Verzeichnis unter /var/www/vhosts/<domain>/ (rein lesend):
    Name, Typ (file/dir/symlink), Grösse, Rechte, Besitzer, Änderungsdatum.
    depth max. 3, max. 500 Einträge (danach truncated=true). Symlinks werden
    nicht verfolgt. Der Papierkorb liegt unter ".mcp-trash".
    """
    d = _wp_domain(domain)
    parts = _strict_parts(path, allow_root=True)
    depth = max(1, min(3, int(depth)))
    res = _call("list", _helper_args(f"{_VHOST_BASE}/{d}", parts=parts, depth=depth, max_entries=500))
    out = {"domain": d, "path": "/".join(parts) or ".", "depth": depth, "entries": res["entries"]}
    if res["truncated"]:
        out["hinweis"] = "Gekürzt auf 500 Einträge - kleineren Pfad oder geringere Tiefe wählen."
    return _json(out)


@mcp.tool()
def move_vhost_file(
    domain: str,
    src: str,
    dst: str,
    overwrite: bool = False,
    create_parents: bool = False,
    allow_executable: bool = False,
    confirm: bool = False,
) -> str:
    """Verschiebt/benennt eine Datei oder einen Ordner atomar (rename) um -
    nur innerhalb desselben Docroots derselben Domain (src/dst relativ zu
    /var/www/vhosts/<domain>/, z.B. "httpdocs/alt.jpg"). Existiert dst, wird
    abgebrochen, ausser overwrite=true (nur für Dateien; dst wird vorher als
    .bak-<Zeitstempel> gesichert). create_parents legt fehlende Zielordner
    an. Ausführbare Dateien (.php, .htaccess, ...) nur mit
    allow_executable=true. Symlinks werden nur selbst verschoben. Erfordert
    confirm=true.
    """
    if not confirm:
        raise ValueError("confirm=true erforderlich - move_vhost_file ändert Dateien auf dem Produktivserver.")
    ctx = _vhost_ctx(domain)
    sp = _strict_parts(src)
    dp = _strict_parts(dst)
    for parts, what in ((sp, "Quelle"), (dp, "Ziel")):
        _check_not_protected(parts, ctx, what)
    sdr = _docroot_of(sp, ctx["docroots"])
    if sdr is None or _docroot_of(dp, ctx["docroots"]) != sdr:
        raise ValueError("Verschieben nur innerhalb desselben Docroots möglich.")
    if sp == dp or dp[:len(sp)] == sp:
        raise ValueError("Ziel liegt in der Quelle selbst - abgelehnt.")
    for parts in (sp, dp):
        _check_executable(parts, allow_executable)
    args = _helper_args(ctx["base"], src=sp, dst=dp, overwrite=bool(overwrite),
                        create_parents=bool(create_parents), allow_exec=bool(allow_executable))
    try:
        res = _helper("move", args)
    except _HelperError as e:
        _audit("move", domain=ctx["domain"], src="/".join(sp), dst="/".join(dp), result="failed", error=e.code)
        raise ValueError(_helper_message(e))
    _audit("move", domain=ctx["domain"], src="/".join(sp), dst="/".join(dp), backup=res["backup"], result="ok")
    return _json({"domain": ctx["domain"], "verschoben": "/".join(sp), "nach": "/".join(dp),
                  "typ": res["type"], "backup_ueberschriebenes_ziel": res["backup"],
                  "neu_angelegte_ordner": res["created"]})


_delete_secret = secrets.token_bytes(32)


def _delete_token(domain: str, rel: str, permanent: bool, digest: str, ts: int) -> str:
    msg = f"{domain}\0{rel}\0{int(permanent)}\0{digest}\0{ts}".encode()
    mac = hmac.new(_delete_secret, msg, hashlib.sha256).hexdigest()
    return f"{ts}.{digest}.{mac}"


def _delete_token_digest(token: str, domain: str, rel: str, permanent: bool) -> str:
    try:
        ts_s, digest, _ = token.strip().split(".")
        ts = int(ts_s)
    except ValueError:
        raise ValueError("delete_token ungültig.")
    if not hmac.compare_digest(_delete_token(domain, rel, permanent, digest, ts), token.strip()):
        raise ValueError("delete_token ungültig (passt nicht zu Domain/Pfad/permanent).")
    if time.time() - ts > _DELETE_TOKEN_TTL:
        raise ValueError("delete_token abgelaufen - neuen Dry-Run ausführen.")
    return digest


@mcp.tool()
def delete_vhost_file(
    domain: str,
    path: str,
    recursive: bool = False,
    permanent: bool = False,
    dry_run: bool = False,
    delete_token: str | None = None,
    allow_executable: bool = False,
    confirm: bool = False,
) -> str:
    """Löscht eine Datei/einen Ordner unter /var/www/vhosts/<domain>/ -
    standardmässig in den Papierkorb (.mcp-trash/<Zeitstempel>/<Pfad>,
    ausserhalb des Docroots, 0700, wiederherstellbar mit restore_vhost_trash).
    permanent=true löscht endgültig. Ordner nur mit recursive=true und
    zweistufig: erst dry_run=true (Anzahl, Grösse, erste 50 Pfade,
    delete_token), dann mit delete_token + confirm=true - nur wenn sich der
    Inhalt nicht geändert hat. Keine Wildcards, kein "..". Symlinks werden nur
    selbst entfernt. Geschützte Pfade (Docroot, Vhost-Root, cgi-bin, conf,
    logs, statistics, .ssh, Mail, .mcp-trash, Dotfiles im Vhost-Root) werden
    immer abgelehnt. Ausführbare Dateien nur mit allow_executable=true.
    """
    ctx = _vhost_ctx(domain)
    parts = _strict_parts(path)
    rel = "/".join(parts)
    _check_not_protected(parts, ctx, "Pfad")
    args = _helper_args(ctx["base"], parts=parts)
    info = _call("scan", args)
    if info["type"] == "dir" and not recursive:
        raise ValueError(f"'{rel}' ist ein Ordner - nur mit recursive=true.")
    if dry_run:
        out = {"domain": ctx["domain"], "path": rel, "typ": info["type"], "dateien": info["files"],
               "ordner": info["dirs"], "groesse_bytes": info["size"], "erste_pfade": info["paths"],
               "enthaelt_ausfuehrbare": info["has_exec"], "permanent": bool(permanent),
               "delete_token": _delete_token(ctx["domain"], rel, bool(permanent), info["digest"], int(time.time())),
               "hinweis": "Ausführen mit delete_token und confirm=true (Token 15 Minuten gültig)."}
        if not permanent and info["size"] > _TRASH_MAX_BYTES:
            out["warnung"] = "Zu gross für den Papierkorb - nur mit permanent=true löschbar."
        return _json(out)
    if not confirm:
        raise ValueError("confirm=true erforderlich - delete_vhost_file löscht auf dem Produktivserver.")
    digest = None
    if info["type"] == "dir":
        if not delete_token:
            raise ValueError("Ordner nur zweistufig: zuerst dry_run=true, dann mit delete_token löschen.")
        digest = _delete_token_digest(delete_token, ctx["domain"], rel, bool(permanent))
    elif delete_token:
        digest = _delete_token_digest(delete_token, ctx["domain"], rel, bool(permanent))
    if info["has_exec"] and not allow_executable:
        raise ValueError(_HELPER_MESSAGES["executable"])
    args.update(permanent=bool(permanent), digest=digest, allow_exec=bool(allow_executable),
                trash_max_bytes=_TRASH_MAX_BYTES, owner={"uid": ctx["uid"], "gid": ctx["gid"]})
    try:
        res = _helper("delete", args)
    except _HelperError as e:
        _audit("delete", domain=ctx["domain"], path=rel, permanent=bool(permanent), result="failed", error=e.code)
        raise ValueError(_helper_message(e))
    _audit("delete", domain=ctx["domain"], path=rel, permanent=bool(permanent), size=res["size"],
           files=res["files"], trash_entry=res.get("trash_entry"), result="ok")
    out = {"domain": ctx["domain"], "path": rel, "dateien": res["files"], "groesse_bytes": res["size"]}
    if res["permanent"]:
        out["ergebnis"] = "Endgültig gelöscht."
    else:
        out["ergebnis"] = "In den Papierkorb verschoben."
        out["trash_entry"] = res["trash_entry"]
        out["hinweis"] = f"Wiederherstellen mit restore_vhost_trash(domain, trash_entry=\"{res['trash_entry']}\")."
    return _json(out)


_TRASH_ENTRY_RE = re.compile(r"^\d{14}(-\d+)?$")


@mcp.tool()
def restore_vhost_trash(
    domain: str, trash_entry: str, allow_executable: bool = False, confirm: bool = False
) -> str:
    """Stellt einen Papierkorb-Eintrag (z.B. "20260101120000", siehe
    list_vhost_dir(domain, ".mcp-trash", 2)) an den ursprünglichen Ort
    wieder her. Liegt dort inzwischen etwas, wird abgebrochen (nichts wird
    überschrieben). Ohne confirm=true nur Vorschau. Ausführbare Dateien nur
    mit allow_executable=true.
    """
    entry = trash_entry.strip()
    if not _TRASH_ENTRY_RE.match(entry):
        raise ValueError("trash_entry muss die Form JJJJMMTTHHMMSS (ggf. mit -N) haben.")
    ctx = _vhost_ctx(domain)
    meta = _call("trash_info", _helper_args(ctx["base"], entry=entry))["meta"]
    orig = str(meta.get("original_path", ""))
    parts = _strict_parts(orig)
    _check_not_protected(parts, ctx, "Ziel")
    if not confirm:
        return _json({"domain": ctx["domain"], "trash_entry": entry, "ziel": orig, "meta": meta,
                      "ergebnis": "Vorschau - nichts geändert. Mit confirm=true wiederherstellen."})
    _check_executable(parts, allow_executable)
    try:
        res = _helper("restore", _helper_args(ctx["base"], entry=entry, expect_path=orig,
                                              allow_exec=bool(allow_executable)))
    except _HelperError as e:
        _audit("restore", domain=ctx["domain"], trash_entry=entry, path=orig, result="failed", error=e.code)
        raise ValueError(_helper_message(e))
    _audit("restore", domain=ctx["domain"], trash_entry=entry, path=orig, result="ok")
    return _json({"domain": ctx["domain"], "wiederhergestellt": res["restored"], "typ": res["type"],
                  "ergebnis": "Wiederhergestellt."})


@mcp.tool()
def empty_vhost_trash(domain: str, older_than_days: int = 14, confirm: bool = False) -> str:
    """Löscht Papierkorb-Einträge der Domain endgültig, die älter als
    older_than_days Tage sind. Ohne confirm=true nur Vorschau.
    """
    days = int(older_than_days)
    if days < 0:
        raise ValueError("older_than_days darf nicht negativ sein.")
    ctx = _vhost_ctx(domain)
    res = _call("empty_trash", _helper_args(ctx["base"], days=days, dry=not confirm))
    if confirm:
        _audit("empty_trash", domain=ctx["domain"], older_than_days=days,
               entries=[r["entry"] for r in res["removed"]], result="ok")
    return _json({"domain": ctx["domain"], "aelter_als_tage": days,
                  "eintraege": res["removed"],
                  "ergebnis": "Endgültig gelöscht." if confirm else "Vorschau - nichts gelöscht. Mit confirm=true ausführen."})


def _trash_autoclean() -> None:
    """Optional beim Serverstart: Papierkorb aller Domains aufräumen."""
    try:
        res = _helper("empty_trash_all", _helper_args(_VHOST_BASE, root=_VHOST_BASE, days=_TRASH_AUTOCLEAN_DAYS))
        for dom, removed in res["domains"].items():
            _audit("empty_trash", domain=dom, older_than_days=_TRASH_AUTOCLEAN_DAYS,
                   entries=[r["entry"] for r in removed], result="ok", trigger="startup")
    except Exception as e:
        _audit("empty_trash", trigger="startup", result="failed", error=type(e).__name__)


# ---------------------------------------------------------------------------
# WordPress-Optionen (wp_option_update / wp_option_rollback)
# ---------------------------------------------------------------------------
#
# Änderungen laufen über WP-CLI als System-User der Subscription (runuser,
# nie als root) mit der PHP-Version der Domain. So laufen die WordPress-Hooks
# (update_option) und ein Objekt-Cache (z.B. Redis) wird korrekt aktualisiert.
# Jede Option mit gültigem Namen ist erlaubt (Vorschau ohne confirm, Backup vor
# jeder Änderung, Rollback). Zugangsdaten (wp-config.php,
# DB-Passwort, Salts, /etc/psa/.psa.shadow) werden von diesen Tools weder
# gelesen noch ausgegeben: die Domain-Daten kommen über "plesk db", das sich
# selbst authentifiziert.

_WP_CLI_PATH = os.environ.get("WP_CLI_PATH", "/usr/local/bin/wp-standalone").strip()
_WP_CLI_TIMEOUT = int(os.environ.get("WP_CLI_TIMEOUT", "120"))
_WP_OPTION_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,191}$")
_WP_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_WP_SYSUSER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,63}$")
_WP_BACKUP_ID_RE = re.compile(r"^(\d{14})-([0-9a-f]{8})@([a-z0-9.-]+)$")
_WP_BACKUP_SUBDIR = ".plesk-mcp/wp-option-backups"


def _wp_check_option(option_name: str) -> str:
    """Nur Namensprüfung (Buchstaben, Ziffern, _ und -, max. 191 Zeichen)."""
    name = option_name.strip()
    if not _WP_OPTION_NAME_RE.match(name):
        raise ValueError(f"Ungültiger Optionsname {option_name!r} - erlaubt sind Buchstaben, Ziffern, _ und -.")
    return name


def _wp_domain(domain: str) -> str:
    d = domain.strip().lower()
    if not _WP_DOMAIN_RE.match(d) or ".." in d:
        raise ValueError(f"Ungültiger Domainname: {domain!r}")
    return d


# memory_limit für WP-CLI: Wert der Domain (Plesk-PHP-Einstellungen), auf
# 256M..2048M begrenzt; ohne ermittelbaren Wert 512M. Der PHP-CLI-Standard
# (meist 128M) reicht für grössere Installationen (WooCommerce, Jetpack) nicht.
_WP_MEM_RE = re.compile(r"^(\d{1,10})([KMGkmg]?)$")
_WP_MEM_ARG_RE = re.compile(r"^\d{1,5}M$")
_WP_MEM_MIN_MB = 256
_WP_MEM_MAX_MB = 2048
_WP_MEM_FALLBACK = "512M"
# PHP-/WP-CLI-Warnungen in der Ausgabe: kein Fehler, separat gemeldet
_WP_NOTICE_RE = re.compile(
    r"^\s*(?:PHP\s+)?(?:Warning|Deprecated|Notice|Strict Standards|"
    r"User (?:Warning|Deprecated|Notice))\s*:",
    re.IGNORECASE,
)
_WP_NOTICE_MAX = 30
# Sammelt pro Tool-Aufruf Warnungen und den verwendeten memory_limit
_wp_report: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "_wp_report", default=None
)


def _wp_memory_limit(raw: Any) -> tuple[str, bool]:
    """memory_limit für den WP-CLI-Aufruf aus dem Wert der Domain.
    Rückgabe: (Wert in M, aus der Domain ermittelt?). "-1" -> 2048M,
    unter 256M -> 256M, über 2048M -> 2048M, ungültig/leer -> 512M."""
    v = str(raw).strip() if raw is not None else ""
    if v == "-1":
        return f"{_WP_MEM_MAX_MB}M", True
    m = _WP_MEM_RE.match(v)
    if not m:
        return _WP_MEM_FALLBACK, False
    mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}[m.group(2).upper()]
    mb = int(m.group(1)) * mult // 1024 ** 2
    return f"{max(_WP_MEM_MIN_MB, min(_WP_MEM_MAX_MB, mb))}M", True


def _wp_domain_memory_limit(client: paramiko.SSHClient, domain: str) -> str | None:
    """Wirksamer memory_limit der Domain aus `plesk bin site --show-php-settings`
    (wie hosting_settings/php_settings_show); "Additional directives" haben
    Vorrang. None, wenn nicht lesbar."""
    try:
        out, _, rc = _client_exec(
            client, f"plesk bin site --show-php-settings {shlex.quote(domain)}", timeout=_HOSTING_TIMEOUT
        )
    except Exception:  # noqa: BLE001 - Fallback statt Abbruch
        return None
    if rc != 0:
        return None
    show = _parse_php_settings(out)
    value = show.get("Additional directives", {}).get("memory_limit")
    if value is None:
        value = next((show[sec]["memory_limit"] for sec in _PHPS_SECTIONS
                      if "memory_limit" in show.get(sec, {})), None)
    return value


def _wp_notices(text: str) -> list[str]:
    return [line.strip()[:300] for line in text.splitlines() if _WP_NOTICE_RE.match(line)]


def _wp_strip_notices(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not _WP_NOTICE_RE.match(line))


def _wp_reporting(fn):
    """Für Tools mit WP-CLI: hängt an das JSON-Ergebnis den verwendeten
    memory_limit (bei Fallback mit Hinweis) und gesammelte Warnungen an."""
    @functools.wraps(fn)
    def wrapper(*a: Any, **kw: Any):
        token = _wp_report.set({"warnings": [], "memory_limit": None, "from_domain": True})
        try:
            out = fn(*a, **kw)
            rep = _wp_report.get()
        finally:
            _wp_report.reset(token)
        return _wp_annotate(out, rep)
    return wrapper


def _wp_annotate(out: Any, rep: dict[str, Any] | None) -> Any:
    if not rep or rep.get("memory_limit") is None or not isinstance(out, str):
        return out
    try:
        data = json.loads(out)
    except ValueError:
        return out
    if not isinstance(data, dict):
        return out
    data["php_memory_limit"] = rep["memory_limit"]
    if not rep["from_domain"]:
        data["hinweis_memory_limit"] = (
            f"memory_limit der Domain nicht ermittelbar - WP-CLI mit {_WP_MEM_FALLBACK} gestartet."
        )
    if rep["warnings"]:
        data["warnungen"] = rep["warnings"]
    return json.dumps(data, ensure_ascii=False, indent=2)


def _wp_context(client: paramiko.SSHClient, domain: str) -> dict[str, Any]:
    """System-User, Home und PHP-CLI der Domain aus der Plesk-Datenbank,
    dazu der memory_limit für WP-CLI."""
    sql = (
        "SELECT s.login, s.home, h.php_handler_id FROM domains d "
        "JOIN hosting h ON h.dom_id = d.id JOIN sys_users s ON s.id = h.sys_user_id "
        f"WHERE d.name = {_sql_quote(domain)}"
    )
    out, err, rc = _client_exec(client, f"plesk db -Ne {shlex.quote(sql)}")
    rows = [line.split("\t") for line in out.strip().splitlines() if line.strip()]
    if rc != 0 or len(rows) != 1 or len(rows[0]) != 3:
        raise ValueError(
            f"Domain '{domain}' nicht als Hosting in Plesk gefunden"
            + (f": {err.strip()}" if err.strip() else ".")
        )
    login, home, handler = (c.strip() for c in rows[0])
    if not _WP_SYSUSER_RE.match(login):
        raise ValueError(f"Unerwarteter System-User {login!r} für '{domain}'.")
    if home != posixpath.normpath(home) or not home.startswith(_VHOST_BASE + "/"):
        raise ValueError(f"Unerwartetes Home-Verzeichnis {home!r} für '{domain}'.")
    out, _, rc = _client_exec(client, f"id -u {shlex.quote(login)}")
    if rc != 0 or not out.strip().isdigit() or int(out.strip()) == 0:
        raise ValueError(f"System-User {login!r} fehlt oder ist root - abgebrochen.")

    out, err, rc = _client_exec(client, "plesk bin php_handler --list -json true")
    try:
        handlers = json.loads(out)
    except ValueError:
        raise ValueError(f"PHP-Handler-Liste nicht lesbar: {err.strip() or out[:200]}")
    php = next((h.get("clipath") for h in handlers if h.get("id") == handler), None)
    if not php or not php.startswith("/") or any(c.isspace() for c in php):
        raise ValueError(
            f"PHP-CLI für den Handler {handler!r} der Domain '{domain}' nicht gefunden."
        )
    mem, from_domain = _wp_memory_limit(_wp_domain_memory_limit(client, domain))
    rep = _wp_report.get()
    warnings: list[str] = rep["warnings"] if rep else []
    if rep is not None:
        rep["memory_limit"], rep["from_domain"] = mem, from_domain
    return {"domain": domain, "login": login, "home": home, "php": php,
            "memory_limit": mem, "memory_from_domain": from_domain, "warnings": warnings}


def _wp_run_as_user(
    client: paramiko.SSHClient,
    ctx: dict[str, str],
    argv: list[str],
    cwd: str,
    stdin_data: bytes | None = None,
    timeout: int | None = None,
) -> tuple[str, str, int]:
    """Führt argv als System-User der Subscription aus (runuser, leere
    Umgebung, HOME = Subscription-Home) - nie als root."""
    cmd = (
        f"cd {shlex.quote(cwd)} && runuser -u {shlex.quote(ctx['login'])} -- "
        f"env -i HOME={shlex.quote(ctx['home'])} PATH=/usr/bin:/bin "
        + " ".join(shlex.quote(a) for a in argv)
    )
    return _client_exec(client, cmd, timeout=timeout or _WP_CLI_TIMEOUT, stdin_data=stdin_data)


def _wp_cli_argv(ctx: dict[str, Any], wp_dir: str, args: list[str]) -> list[str]:
    """Einziger Ort, an dem der WP-CLI-Aufruf gebaut wird: PHP der Domain mit
    memory_limit und Fehlerausgabe auf stderr, dann die WP-CLI-Phar direkt."""
    mem = ctx.get("memory_limit") or _WP_MEM_FALLBACK
    if not _WP_MEM_ARG_RE.match(mem):
        raise ValueError(f"Ungültiger memory_limit {mem!r} für WP-CLI.")
    return [ctx["php"], "-d", f"memory_limit={mem}", "-d", "display_errors=stderr",
            _WP_CLI_PATH, f"--path={wp_dir}", "--no-color", *args]


def _wp_cli_run(
    client: paramiko.SSHClient,
    ctx: dict[str, Any],
    wp_dir: str,
    args: list[str],
    stdin_data: bytes | None = None,
    timeout: int | None = None,
) -> tuple[str, str, int]:
    """WP-CLI als Subscription-User. Massgebend ist der Exit-Code; PHP- und
    WP-CLI-Warnungen werden aus stdout entfernt und in ctx["warnings"]
    gesammelt."""
    out, err, rc = _wp_run_as_user(
        client, ctx, _wp_cli_argv(ctx, wp_dir, args),
        cwd=wp_dir, stdin_data=stdin_data, timeout=timeout,
    )
    warnings = ctx.setdefault("warnings", [])
    for w in _wp_notices(err) + _wp_notices(out):
        if w not in warnings and len(warnings) < _WP_NOTICE_MAX:
            warnings.append(w)
    return _wp_strip_notices(out), err, rc


def _wp_cli_error(out: str, err: str, lines: int = 5) -> str:
    """Letzte Zeilen der Fehlerausgabe ohne Warnungen."""
    text = _wp_strip_notices(err).strip() or _wp_strip_notices(out).strip() or err.strip() or out.strip()
    return "\n".join(text.splitlines()[-lines:])


def _wp_cli(
    client: paramiko.SSHClient, ctx: dict[str, Any], wp_dir: str, args: list[str]
) -> str:
    out, err, rc = _wp_cli_run(client, ctx, wp_dir, args)
    if rc != 0:
        raise SSHError(
            f"WP-CLI 'wp {args[0]} {args[1] if len(args) > 1 else ''}' fehlgeschlagen: {_wp_cli_error(out, err)}"
        )
    return out


def _wp_json(text: str) -> Any:
    """JSON aus WP-CLI-Ausgabe; toleriert Zeilen, die Plugins vorneweg ausgeben,
    und PHP-Warnungen."""
    text = _wp_strip_notices(text).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    raise ValueError("WP-CLI-Ausgabe ist kein gültiges JSON.")


_WP_MISSING = object()


def _wp_get_option(
    client: paramiko.SSHClient, ctx: dict[str, str], wp_dir: str, name: str
) -> Any:
    """Aktueller Wert oder _WP_MISSING, falls die Option nicht existiert."""
    out, err, rc = _wp_cli_run(client, ctx, wp_dir, ["option", "get", name, "--format=json"])
    if rc != 0:
        if "does it exist" in (err + out).lower():
            return _WP_MISSING
        raise SSHError(f"WP-CLI 'wp option get' fehlgeschlagen: {_wp_cli_error(out, err)}")
    return _wp_json(out)


def _wp_install_dir(sftp, domain: str, wp_path: str) -> str:
    wp_dir = _vhost_path(domain, wp_path)
    _assert_real_within_vhost(sftp, wp_dir, domain)
    try:
        st = sftp.lstat(f"{wp_dir}/wp-load.php")
    except FileNotFoundError:
        raise ValueError(f"Keine WordPress-Installation in {wp_dir} (wp-load.php fehlt).")
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"{wp_dir}/wp-load.php ist keine reguläre Datei.")
    return wp_dir


def _wp_plan(current: Any, mode: str, targets: dict[str, Any]) -> dict[str, Any]:
    """Vergleicht aktuelle mit Zielwerten. targets: key -> Wert oder
    _WP_MISSING (= Schlüssel/Option löschen). Im value-Modus ist der einzige
    Schlüssel "" (die ganze Option)."""
    plan: dict[str, Any] = {}
    for key, new in targets.items():
        if mode == "keys":
            old = current.get(key, _WP_MISSING)
        else:
            old = current
        plan[key] = {
            "old": old,
            "new": new,
            "changed": not (old is not _WP_MISSING and new is not _WP_MISSING and old == new)
            and not (old is _WP_MISSING and new is _WP_MISSING),
        }
    return plan


def _wp_plan_view(plan: dict[str, Any]) -> dict[str, Any]:
    view = {}
    for key, p in plan.items():
        view[key or "(Wert)"] = {
            "alt": "(nicht vorhanden)" if p["old"] is _WP_MISSING else p["old"],
            "neu": "(wird entfernt)" if p["new"] is _WP_MISSING else p["new"],
            "status": "ändern" if p["changed"] else "unverändert",
        }
    return view


def _wp_write_backup(
    client: paramiko.SSHClient,
    ctx: dict[str, str],
    wp_dir: str,
    wp_path: str,
    option: str,
    mode: str,
    current: Any,
    plan: dict[str, Any],
    source: str,
) -> str:
    """Legt den alten Zustand als JSON unter <home>/.plesk-mcp/wp-option-backups/
    ab - ausserhalb des Webroots, geschrieben als Subscription-User (0600)."""
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    backup_id = f"{ts}-{os.urandom(4).hex()}@{ctx['domain']}"
    record = {
        "id": backup_id,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "domain": ctx["domain"],
        "wp_path": wp_path,
        "option_name": option,
        "mode": mode,
        "option_existed": current is not _WP_MISSING,
        "old_option_value": None if current is _WP_MISSING else current,
        "entries": {
            key: {"existed": p["old"] is not _WP_MISSING,
                  "value": None if p["old"] is _WP_MISSING else p["old"]}
            for key, p in plan.items() if p["changed"]
        },
    }
    data = json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")
    bdir = f"{ctx['home']}/{_WP_BACKUP_SUBDIR}"
    _, err, rc = _wp_run_as_user(
        client, ctx,
        ["/bin/sh", "-c", 'umask 077 && mkdir -p "$1" && set -C && cat > "$2"',
         "sh", bdir, f"{bdir}/{backup_id}.json"],
        cwd=ctx["home"], stdin_data=data,
    )
    if rc != 0:
        raise SSHError(f"Backup konnte nicht angelegt werden - nichts geändert: {err.strip()}")
    return backup_id


def _wp_apply(
    client: paramiko.SSHClient,
    ctx: dict[str, str],
    wp_dir: str,
    option: str,
    mode: str,
    plan: dict[str, Any],
) -> list[str]:
    """Schreibt die geänderten Einträge über WP-CLI; gibt Fehler je Schlüssel zurück."""
    errors = []
    for key, p in plan.items():
        if not p["changed"]:
            continue
        try:
            if mode == "value":
                if p["new"] is _WP_MISSING:
                    args = ["option", "delete", option]
                else:
                    args = ["option", "update", option, json.dumps(p["new"]), "--format=json"]
            elif p["new"] is _WP_MISSING:
                args = ["option", "patch", "delete", option, key]
            else:
                verb = "insert" if p["old"] is _WP_MISSING else "update"
                args = ["option", "patch", verb, option, key, json.dumps(p["new"]), "--format=json"]
            _wp_cli(client, ctx, wp_dir, args)
        except SSHError as e:
            errors.append(f"{key or option}: {e}")
    return errors


def _wp_change(
    domain: str,
    wp_path: str,
    option: str,
    mode: str,
    targets: dict[str, Any],
    confirm: bool,
    source: str,
) -> str:
    """Gemeinsamer Ablauf für Update und Rollback: Kontext, alter Wert,
    Vorschau bzw. Backup + Schreiben + Kontrolle."""
    d = _wp_domain(domain)
    client = _ssh_connect()
    try:
        ctx = _wp_context(client, d)
        sftp = client.open_sftp()
        try:
            wp_dir = _wp_install_dir(sftp, d, wp_path)
        finally:
            sftp.close()
        current = _wp_get_option(client, ctx, wp_dir, option)
        if mode == "keys":
            if current is _WP_MISSING:
                raise ValueError(f"Option '{option}' existiert nicht - Teilschlüssel nicht setzbar.")
            if not isinstance(current, dict):
                raise ValueError(
                    f"Option '{option}' ist kein Array mit Schlüsseln - 'value' statt 'keys' verwenden."
                )
        plan = _wp_plan(current, mode, targets)
        result: dict[str, Any] = {
            "domain": d,
            "wp_path": wp_dir,
            "option": option,
            "system_user": ctx["login"],
            "aenderungen": _wp_plan_view(plan),
        }
        if not any(p["changed"] for p in plan.values()):
            result["ergebnis"] = "Keine Änderung nötig - alle Werte sind bereits gesetzt."
            return json.dumps(result, ensure_ascii=False, indent=2)
        if not confirm:
            result["ergebnis"] = "Vorschau - nichts geschrieben. Mit confirm=true ausführen."
            return json.dumps(result, ensure_ascii=False, indent=2)

        backup_id = _wp_write_backup(
            client, ctx, wp_dir, wp_path, option, mode, current, plan, source
        )
        errors = _wp_apply(client, ctx, wp_dir, option, mode, plan)
        after = _wp_get_option(client, ctx, wp_dir, option)
        mismatches = []
        for key, p in plan.items():
            if not p["changed"]:
                continue
            now = after.get(key, _WP_MISSING) if (mode == "keys" and isinstance(after, dict)) else after
            if now != p["new"] and not (now is _WP_MISSING and p["new"] is _WP_MISSING):
                mismatches.append(key or option)
    finally:
        client.close()

    result["backup_id"] = backup_id
    result["backup_datei"] = f"{ctx['home']}/{_WP_BACKUP_SUBDIR}/{backup_id}.json"
    if errors or mismatches:
        result["ergebnis"] = "Teilweise fehlgeschlagen - Rollback mit backup_id möglich."
        result["fehler"] = errors
        result["abweichend_nach_kontrolle"] = mismatches
    else:
        result["ergebnis"] = "Geschrieben und kontrolliert."
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
@_wp_reporting
def wp_option_update(
    domain: str,
    wp_path: str,
    option_name: str,
    keys: dict[str, Any] | None = None,
    value: Any = None,
    confirm: bool = False,
) -> str:
    """Ändert eine einzelne WordPress-Option gezielt über WP-CLI - auch bei
    Installationen, die nicht im WP Toolkit eingebunden sind.

    domain: z.B. "example.com"; wp_path: WordPress-Verzeichnis relativ zu
    /var/www/vhosts/<domain>/ (z.B. "httpdocs").
    keys: für serialisierte Arrays - dict Teilschlüssel (oberste Ebene) ->
    neuer Wert, z.B. {"minify_css": 1} (wp option patch update/insert).
    value: für einfache Optionen der neue Wert, z.B. "swap" (wp option update).
    Genau eines von beiden angeben.

    Läuft als System-User der Subscription (nie root) mit der PHP-Version
    der Domain; WordPress-Hooks und Objekt-Cache (Redis) werden dadurch
    korrekt bedient. Jede Option mit gültigem Namen (z.B. imagify_settings,
    blog_public).
    Ohne confirm=true nur Vorschau (alt/neu je Schlüssel, nichts geschrieben).
    Mit confirm=true wird vorher ein JSON-Backup ausserhalb des Webroots
    angelegt; Rückgabe mit backup_id (für wp_option_rollback) und alt/neu.
    """
    option = _wp_check_option(option_name)
    if (keys is None) == (value is None):
        raise ValueError("Genau eines von 'keys' (Teilschlüssel) oder 'value' angeben.")
    if keys is not None:
        if not isinstance(keys, dict) or not keys:
            raise ValueError("'keys' muss ein nicht-leeres dict Teilschlüssel -> Wert sein.")
        for k in keys:
            if not isinstance(k, str) or not k.strip() or len(k) > 191:
                raise ValueError(f"Ungültiger Teilschlüssel: {k!r}")
        return _wp_change(domain, wp_path, option, "keys", dict(keys), confirm, "wp_option_update")
    return _wp_change(domain, wp_path, option, "value", {"": value}, confirm, "wp_option_update")


@mcp.tool()
@_wp_reporting
def wp_option_rollback(backup_id: str, confirm: bool = False) -> str:
    """Spielt ein von wp_option_update angelegtes Backup zurück (nur die
    damals geänderten Schlüssel bzw. den Wert; damals fehlende Schlüssel
    werden wieder entfernt). Ohne confirm=true nur Vorschau. Vor dem
    Zurückspielen wird der aktuelle Stand selbst wieder als Backup abgelegt
    (neue backup_id in der Rückgabe).
    """
    m = _WP_BACKUP_ID_RE.match(backup_id.strip())
    if not m:
        raise ValueError(f"Ungültige backup_id: {backup_id!r}")
    bid = m.group(0)
    d = _wp_domain(m.group(3))
    client = _ssh_connect()
    try:
        ctx = _wp_context(client, d)
        out, err, rc = _wp_run_as_user(
            client, ctx,
            ["/bin/cat", "--", f"{ctx['home']}/{_WP_BACKUP_SUBDIR}/{bid}.json"],
            cwd=ctx["home"],
        )
    finally:
        client.close()
    if rc != 0:
        raise ValueError(f"Backup '{bid}' nicht gefunden: {err.strip()}")
    try:
        record = json.loads(out)
        option = _wp_check_option(record["option_name"])
        mode = record["mode"]
        wp_path = record["wp_path"]
        entries = record["entries"]
        if record.get("domain") != d or mode not in ("keys", "value") or not isinstance(entries, dict):
            raise ValueError("Inhalt passt nicht zur backup_id")
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError(f"Backup '{bid}' ist ungültig: {e}")
    targets = {
        key: (e["value"] if e.get("existed") else _WP_MISSING) for key, e in entries.items()
    }
    return _wp_change(d, wp_path, option, mode, targets, confirm, f"wp_option_rollback:{bid}")


# ---------------------------------------------------------------------------
# WordPress Customizer "Zusätzliches CSS" (custom_css-Post je Theme)
# ---------------------------------------------------------------------------

_WP_CSS_BACKUP_SUBDIR = ".plesk-mcp/wp-custom-css-backups"
_WP_STYLESHEET_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_WP_CSS_MAX_BYTES = 512 * 1024

# Fester PHP-Code für `wp eval` - das CSS selbst kommt NIE in den Code,
# sondern per stdin; das Stylesheet wird nur nach Regex-Prüfung als
# einfach-quotiertes Literal eingesetzt (keine Sonderzeichen möglich).
_WP_CSS_GET_PHP = (
    '$st = %s; echo json_encode(array("stylesheet" => $st, '
    '"active" => get_stylesheet(), "css" => (string) wp_get_custom_css($st)));'
)
_WP_CSS_SET_PHP = (
    '$st = %s; $css = stream_get_contents(STDIN); '
    '$r = wp_update_custom_css_post($css, array("stylesheet" => $st)); '
    'if (is_wp_error($r)) { fwrite(STDERR, $r->get_error_message()); exit(1); } '
    'echo json_encode(array("post_id" => $r->ID, "stylesheet" => $st, '
    '"css" => (string) wp_get_custom_css($st)));'
)


def _wp_check_stylesheet(stylesheet: str) -> str:
    """Theme-Verzeichnisname (leer = aktives Theme)."""
    st = stylesheet.strip()
    if not st:
        return ""
    if not _WP_STYLESHEET_RE.match(st) or ".." in st:
        raise ValueError(
            f"Ungültiger Theme-Name {stylesheet!r} - erlaubt sind Buchstaben, Ziffern, _, - und Punkt."
        )
    return st


def _wp_css_php_literal(stylesheet: str) -> str:
    """Stylesheet als PHP-Ausdruck: Literal nach Regex-Prüfung, sonst get_stylesheet()."""
    return f"'{stylesheet}'" if stylesheet else "get_stylesheet()"


def _wp_check_css(css: str) -> str:
    if not isinstance(css, str):
        raise ValueError("css muss ein String sein.")
    if "\x00" in css:
        raise ValueError("CSS enthält ein Nullbyte.")
    if len(css.encode("utf-8")) > _WP_CSS_MAX_BYTES:
        raise ValueError(f"CSS zu gross (max. {_WP_CSS_MAX_BYTES // 1024} KB).")
    # WordPress selbst lehnt "</style" ab (wp_update_custom_css_post -> validate)
    if "</style" in css.lower():
        raise ValueError("CSS darf kein '</style' enthalten.")
    return css


def _wp_eval(
    client: paramiko.SSHClient,
    ctx: dict[str, str],
    wp_dir: str,
    php: str,
    stdin_data: bytes | None = None,
) -> Any:
    """`wp eval <php>` als Subscription-User; Rückgabe ist das JSON der Ausgabe."""
    out, err, rc = _wp_cli_run(client, ctx, wp_dir, ["eval", php], stdin_data=stdin_data)
    if rc != 0:
        raise SSHError(f"WP-CLI 'wp eval' fehlgeschlagen: {_wp_cli_error(out, err)}")
    return _wp_json(out)


def _wp_css_get(client, ctx, wp_dir: str, stylesheet: str) -> dict[str, Any]:
    data = _wp_eval(client, ctx, wp_dir, _WP_CSS_GET_PHP % _wp_css_php_literal(stylesheet))
    if not isinstance(data, dict) or "css" not in data:
        raise SSHError("Unerwartete Antwort von WordPress beim Lesen des Custom-CSS.")
    return data


def _wp_css_write_backup(client, ctx, wp_dir: str, wp_path: str, stylesheet: str,
                         old_css: str, source: str) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    backup_id = f"{ts}-{os.urandom(4).hex()}@{ctx['domain']}"
    record = {
        "id": backup_id,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "domain": ctx["domain"],
        "wp_path": wp_path,
        "stylesheet": stylesheet,
        "old_css": old_css,
    }
    data = json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")
    bdir = f"{ctx['home']}/{_WP_CSS_BACKUP_SUBDIR}"
    _, err, rc = _wp_run_as_user(
        client, ctx,
        ["/bin/sh", "-c", 'umask 077 && mkdir -p "$1" && set -C && cat > "$2"',
         "sh", bdir, f"{bdir}/{backup_id}.json"],
        cwd=ctx["home"], stdin_data=data,
    )
    if rc != 0:
        raise SSHError(f"Backup konnte nicht angelegt werden - nichts geändert: {err.strip()}")
    return backup_id


def _wp_css_diff(old: str, new: str) -> str:
    import difflib
    lines = difflib.unified_diff(
        old.splitlines(), new.splitlines(), "alt", "neu", lineterm="", n=2
    )
    text = "\n".join(lines)
    return text if len(text) <= 20000 else text[:20000] + "\n... (gekürzt)"


def _wp_css_change(domain: str, wp_path: str, stylesheet: str, new_css: str,
                   mode: str, confirm: bool, source: str) -> str:
    d = _wp_domain(domain)
    client = _ssh_connect()
    try:
        ctx = _wp_context(client, d)
        sftp = client.open_sftp()
        try:
            wp_dir = _wp_install_dir(sftp, d, wp_path)
        finally:
            sftp.close()
        current = _wp_css_get(client, ctx, wp_dir, stylesheet)
        old_css = current["css"]
        st = current["stylesheet"]
        if mode == "append":
            target = old_css.rstrip("\n") + ("\n\n" if old_css.strip() else "") + new_css
        else:
            target = new_css
        result: dict[str, Any] = {
            "domain": d,
            "wp_path": wp_dir,
            "stylesheet": st,
            "aktives_theme": current.get("active"),
            "system_user": ctx["login"],
            "modus": mode,
            "zeichen_alt": len(old_css),
            "zeichen_neu": len(target),
        }
        if st != current.get("active"):
            result["hinweis"] = (
                "Das CSS gehört zu einem anderen Theme als dem aktiven - es wird "
                "erst sichtbar, wenn dieses Theme aktiv ist."
            )
        if target == old_css:
            result["ergebnis"] = "Keine Änderung nötig - CSS ist bereits identisch."
            return json.dumps(result, ensure_ascii=False, indent=2)
        result["diff"] = _wp_css_diff(old_css, target)
        if not confirm:
            result["ergebnis"] = "Vorschau - nichts geschrieben. Mit confirm=true ausführen."
            return json.dumps(result, ensure_ascii=False, indent=2)

        backup_id = _wp_css_write_backup(client, ctx, wp_dir, wp_path, st, old_css, source)
        written = _wp_eval(
            client, ctx, wp_dir, _WP_CSS_SET_PHP % _wp_css_php_literal(st),
            stdin_data=target.encode("utf-8"),
        )
        after = _wp_css_get(client, ctx, wp_dir, st)
        # Objekt-Cache leeren (best effort, z.B. Redis); Seiten-Caches von
        # Plugins werden durch den WordPress-Hook beim Speichern des Posts
        # ausgelöst, sonst via delete_vhost_file im Cache-Verzeichnis.
        cache_note = ""
        try:
            _wp_cli(client, ctx, wp_dir, ["cache", "flush"])
        except SSHError as e:
            cache_note = str(e)
    finally:
        client.close()

    result["backup_id"] = backup_id
    result["backup_datei"] = f"{ctx['home']}/{_WP_CSS_BACKUP_SUBDIR}/{backup_id}.json"
    if isinstance(written, dict) and written.get("post_id"):
        result["post_id"] = written["post_id"]
    if after["css"] != target:
        result["ergebnis"] = "Geschrieben, aber Kontrolle weicht ab - Rollback mit backup_id möglich."
        result["abweichung"] = _wp_css_diff(target, after["css"])
    else:
        result["ergebnis"] = "Geschrieben und kontrolliert."
    if cache_note:
        result["cache_hinweis"] = cache_note
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
@_wp_reporting
def wp_custom_css_show(domain: str, wp_path: str, stylesheet: str = "") -> str:
    """Zeigt das "Zusätzliche CSS" aus dem WordPress-Customizer (Design >
    Customizer > Zusätzliches CSS) einer Installation - standardmässig für
    das aktive Theme, optional für ein anderes Theme (stylesheet =
    Theme-Verzeichnisname, z.B. "hello-elementor").

    domain: z.B. "example.com"; wp_path: WordPress-Verzeichnis relativ zu
    /var/www/vhosts/<domain>/ (z.B. "httpdocs"). Rein lesend.
    """
    st = _wp_check_stylesheet(stylesheet)
    d = _wp_domain(domain)
    client = _ssh_connect()
    try:
        ctx = _wp_context(client, d)
        sftp = client.open_sftp()
        try:
            wp_dir = _wp_install_dir(sftp, d, wp_path)
        finally:
            sftp.close()
        data = _wp_css_get(client, ctx, wp_dir, st)
    finally:
        client.close()
    return json.dumps({
        "domain": d,
        "wp_path": wp_dir,
        "stylesheet": data["stylesheet"],
        "aktives_theme": data.get("active"),
        "zeichen": len(data["css"]),
        "css": data["css"],
    }, ensure_ascii=False, indent=2)


@mcp.tool()
@_wp_reporting
def wp_custom_css_update(
    domain: str,
    wp_path: str,
    css: str,
    mode: str = "replace",
    stylesheet: str = "",
    confirm: bool = False,
) -> str:
    """Setzt das "Zusätzliche CSS" des WordPress-Customizers (Design >
    Customizer > Zusätzliches CSS) - genau das, was man dort von Hand
    eintragen würde, damit es auch im Backend sichtbar und editierbar bleibt.

    css: der CSS-Text. mode: "replace" (Standard, ersetzt den kompletten
    Inhalt) oder "append" (hängt css an den bestehenden Inhalt an).
    stylesheet: leer = aktives Theme (Customizer-CSS ist an das Theme
    gebunden), sonst Theme-Verzeichnisname.

    Läuft über WP-CLI (`wp eval` mit festem Code, CSS per stdin) als
    System-User der Subscription - nie root; WordPress legt dabei wie im
    Customizer eine Revision des custom_css-Posts an. Ohne confirm=true nur
    Vorschau mit Diff alt/neu, nichts geschrieben. Mit confirm=true wird
    vorher ein JSON-Backup des alten CSS ausserhalb des Webroots angelegt
    (backup_id für wp_custom_css_rollback), geschrieben, erneut gelesen und
    kontrolliert; danach `wp cache flush` (Objekt-Cache). Seiten-Caches
    (z.B. WP Super Cache) ggf. zusätzlich leeren.
    """
    if mode not in ("replace", "append"):
        raise ValueError("mode muss 'replace' oder 'append' sein.")
    st = _wp_check_stylesheet(stylesheet)
    text = _wp_check_css(css)
    if mode == "append" and not text.strip():
        raise ValueError("Leeres css kann nicht angehängt werden.")
    return _wp_css_change(domain, wp_path, st, text, mode, confirm, "wp_custom_css_update")


@mcp.tool()
@_wp_reporting
def wp_custom_css_rollback(backup_id: str, confirm: bool = False) -> str:
    """Spielt ein von wp_custom_css_update angelegtes Backup zurück (das
    damalige komplette CSS des Themes). Ohne confirm=true nur Vorschau mit
    Diff. Vor dem Zurückspielen wird der aktuelle Stand selbst wieder als
    Backup abgelegt (neue backup_id in der Rückgabe).
    """
    m = _WP_BACKUP_ID_RE.match(backup_id.strip())
    if not m:
        raise ValueError(f"Ungültige backup_id: {backup_id!r}")
    bid = m.group(0)
    d = _wp_domain(m.group(3))
    client = _ssh_connect()
    try:
        ctx = _wp_context(client, d)
        out, err, rc = _wp_run_as_user(
            client, ctx,
            ["/bin/cat", "--", f"{ctx['home']}/{_WP_CSS_BACKUP_SUBDIR}/{bid}.json"],
            cwd=ctx["home"],
        )
    finally:
        client.close()
    if rc != 0:
        raise ValueError(f"Backup '{bid}' nicht gefunden: {err.strip()}")
    try:
        record = json.loads(out)
        st = _wp_check_stylesheet(record["stylesheet"])
        wp_path = record["wp_path"]
        old_css = record["old_css"]
        if record.get("domain") != d or not isinstance(old_css, str) or not isinstance(wp_path, str):
            raise ValueError("Inhalt passt nicht zur backup_id")
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError(f"Backup '{bid}' ist ungültig: {e}")
    return _wp_css_change(d, wp_path, st, old_css, "replace", confirm, f"wp_custom_css_rollback:{bid}")


# ---------------------------------------------------------------------------
# WordPress-Kommentare (Moderation: Spam, Papierkorb, Freigabe, Löschen)
# ---------------------------------------------------------------------------

_WP_COMMENT_STATUSES = ("hold", "approve", "spam", "trash", "all")
_WP_COMMENT_ACTIONS = {
    "spam": ["comment", "spam"],
    "unspam": ["comment", "unspam"],
    "trash": ["comment", "trash"],
    "untrash": ["comment", "untrash"],
    "approve": ["comment", "approve"],
    "unapprove": ["comment", "unapprove"],
    "delete": ["comment", "delete", "--force"],
}
# Erwarteter Wert von comment_approved nach der Aktion (None = Kommentar weg)
_WP_COMMENT_EXPECTED = {
    "spam": "spam", "unspam": None, "trash": "trash", "untrash": None,
    "approve": "1", "unapprove": "0", "delete": None,
}
_WP_COMMENT_FIELDS = (
    "comment_ID,comment_post_ID,comment_author,comment_author_email,"
    "comment_author_url,comment_author_IP,comment_date,comment_approved,comment_content"
)
_WP_COMMENT_MAX_IDS = 200


def _wp_comment_ids(comment_ids: list[int] | int) -> list[int]:
    ids = [comment_ids] if isinstance(comment_ids, int) else list(comment_ids or [])
    if not ids or len(ids) > _WP_COMMENT_MAX_IDS:
        raise ValueError(f"1 bis {_WP_COMMENT_MAX_IDS} Kommentar-IDs angeben.")
    out: list[int] = []
    for i in ids:
        if isinstance(i, bool) or not isinstance(i, int) or i <= 0:
            raise ValueError(f"Ungültige Kommentar-ID: {i!r}")
        if i not in out:
            out.append(i)
    return out


def _wp_comment_view(c: dict[str, Any], excerpt: int = 300) -> dict[str, Any]:
    content = str(c.get("comment_content") or "")
    content = re.sub(r"\s+", " ", content).strip()
    return {
        "id": int(c.get("comment_ID") or 0),
        "post_id": int(c.get("comment_post_ID") or 0),
        "status": c.get("comment_approved"),
        "autor": c.get("comment_author"),
        "email": c.get("comment_author_email"),
        "url": c.get("comment_author_url"),
        "ip": c.get("comment_author_IP"),
        "datum": c.get("comment_date"),
        "inhalt": content if len(content) <= excerpt else content[:excerpt] + "…",
    }


def _wp_comment_counts(text: str) -> dict[str, int]:
    """Parst die Ausgabe von `wp comment count` ("approved: 12" je Zeile)."""
    counts: dict[str, int] = {}
    for line in text.splitlines():
        m = re.match(r"^\s*([a-z_]+):\s*(\d+)\s*$", line)
        if m:
            counts[m.group(1)] = int(m.group(2))
    return counts


def _wp_comment_fetch(client, ctx, wp_dir: str, ids: list[int]) -> dict[int, dict[str, Any]]:
    """Kommentare per ID lesen; fehlende IDs fehlen im Ergebnis."""
    out = _wp_cli(
        client, ctx, wp_dir,
        ["comment", "list", f"--comment__in={','.join(str(i) for i in ids)}",
         f"--fields={_WP_COMMENT_FIELDS}", "--format=json", "--status=all"],
    )
    rows = _wp_json(out)
    if not isinstance(rows, list):
        raise SSHError("Unerwartete Antwort von 'wp comment list'.")
    return {int(r["comment_ID"]): r for r in rows if r.get("comment_ID")}


@mcp.tool()
@_wp_reporting
def wp_comment_list(
    domain: str,
    wp_path: str,
    status: str = "hold",
    limit: int = 50,
    search: str = "",
) -> str:
    """Listet WordPress-Kommentare einer Installation per WP-CLI - z.B. die
    auf Freigabe wartenden ("Bitte moderiere"-Mails).

    domain: z.B. "example.com"; wp_path: WordPress-Verzeichnis relativ zu
    /var/www/vhosts/<domain>/ (z.B. "httpdocs").
    status: "hold" (wartend auf Freigabe, Standard), "approve", "spam",
    "trash" oder "all". limit: max. Anzahl (1-200), neueste zuerst.
    search: optional, Textsuche in Autor/E-Mail/URL/Inhalt.
    Rein lesend; Inhalt wird auf 300 Zeichen gekürzt.
    """
    if status not in _WP_COMMENT_STATUSES:
        raise ValueError(f"status muss eines von {', '.join(_WP_COMMENT_STATUSES)} sein.")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise ValueError("limit muss zwischen 1 und 200 liegen.")
    q = search.strip()
    if "\x00" in q or len(q) > 200:
        raise ValueError("Ungültiger Suchtext.")
    d = _wp_domain(domain)
    client = _ssh_connect()
    try:
        ctx = _wp_context(client, d)
        sftp = client.open_sftp()
        try:
            wp_dir = _wp_install_dir(sftp, d, wp_path)
        finally:
            sftp.close()
        args = ["comment", "list", f"--status={status}", f"--number={limit}",
                "--orderby=comment_date_gmt", "--order=DESC",
                f"--fields={_WP_COMMENT_FIELDS}", "--format=json"]
        if q:
            args.append(f"--search={q}")
        rows = _wp_json(_wp_cli(client, ctx, wp_dir, args))
        counts = _wp_comment_counts(_wp_cli(client, ctx, wp_dir, ["comment", "count"]))
    finally:
        client.close()
    if not isinstance(rows, list):
        raise SSHError("Unerwartete Antwort von 'wp comment list'.")
    return json.dumps({
        "domain": d,
        "wp_path": wp_dir,
        "status": status,
        "anzahl": len(rows),
        "zaehler": counts,
        "kommentare": [_wp_comment_view(r) for r in rows],
    }, ensure_ascii=False, indent=2)


@mcp.tool()
@_wp_reporting
def wp_comment_moderate(
    domain: str,
    wp_path: str,
    comment_ids: list[int],
    action: str,
    confirm: bool = False,
) -> str:
    """Moderiert WordPress-Kommentare per WP-CLI (als System-User der
    Subscription, nie root): action = "spam", "trash", "approve",
    "unapprove", "unspam", "untrash" oder "delete" (endgültig löschen,
    `wp comment delete --force`).

    comment_ids: Liste der Kommentar-IDs (1-200), z.B. aus wp_comment_list
    oder aus dem "c=<id>"-Parameter der Moderations-Mail.
    Ohne confirm=true nur Vorschau (zeigt Autor, E-Mail, Inhalt und
    aktuellen Status je Kommentar, nichts geändert). Mit confirm=true wird
    die Aktion ausgeführt und der Status danach je Kommentar kontrolliert;
    WordPress-Hooks (z.B. Akismet-Lernen bei Spam) laufen dabei mit.
    Spam/Papierkorb sind in WordPress umkehrbar, "delete" nicht.
    """
    if action not in _WP_COMMENT_ACTIONS:
        raise ValueError(f"action muss eines von {', '.join(_WP_COMMENT_ACTIONS)} sein.")
    ids = _wp_comment_ids(comment_ids)
    d = _wp_domain(domain)
    client = _ssh_connect()
    try:
        ctx = _wp_context(client, d)
        sftp = client.open_sftp()
        try:
            wp_dir = _wp_install_dir(sftp, d, wp_path)
        finally:
            sftp.close()
        before = _wp_comment_fetch(client, ctx, wp_dir, ids)
        missing = [i for i in ids if i not in before]
        result: dict[str, Any] = {
            "domain": d,
            "wp_path": wp_dir,
            "action": action,
            "system_user": ctx["login"],
            "kommentare": [_wp_comment_view(before[i]) for i in ids if i in before],
        }
        if missing:
            result["nicht_gefunden"] = missing
        if not before:
            result["ergebnis"] = "Keine der angegebenen Kommentar-IDs existiert - nichts zu tun."
            return json.dumps(result, ensure_ascii=False, indent=2)
        if not confirm:
            result["ergebnis"] = "Vorschau - nichts geändert. Mit confirm=true ausführen."
            return json.dumps(result, ensure_ascii=False, indent=2)

        todo = [i for i in ids if i in before]
        errors: list[str] = []
        try:
            _wp_cli(client, ctx, wp_dir, [*_WP_COMMENT_ACTIONS[action], *[str(i) for i in todo]])
        except SSHError as e:
            errors.append(str(e))
        after = _wp_comment_fetch(client, ctx, wp_dir, todo)
        expected = _WP_COMMENT_EXPECTED[action]
        ok, failed = [], []
        for i in todo:
            now = after.get(i, {}).get("comment_approved") if i in after else None
            if action == "delete":
                (ok if i not in after else failed).append(i)
            elif expected is None:
                # unspam/untrash: zurück auf den vorherigen Status (0/1)
                (ok if now in ("0", "1") else failed).append(i)
            else:
                (ok if now == expected else failed).append(i)
    finally:
        client.close()

    result["erledigt"] = ok
    if failed or errors:
        result["ergebnis"] = "Teilweise fehlgeschlagen."
        if failed:
            result["abweichend_nach_kontrolle"] = {
                i: (after.get(i, {}).get("comment_approved") if i in after else "nicht mehr vorhanden")
                for i in failed
            }
        if errors:
            result["fehler"] = errors
    else:
        result["ergebnis"] = f"{len(ok)} Kommentar(e): {action} ausgeführt und kontrolliert."
    return json.dumps(result, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# WordPress-Plugins und -Themes aktualisieren (wp plugin|theme update)
# mit Backup und Rollback
# ---------------------------------------------------------------------------
#
# Läuft wie die übrigen wp_*-Tools per WP-CLI als System-User der
# Subscription (nie root). Vor jedem Update wird jedes betroffene Plugin
# (Verzeichnis bzw. Einzeldatei) bzw. Theme (Verzeichnis) als tar.gz unter
# <home>/.plesk-mcp/wp-plugin-backups/<backup_id>/ bzw.
# <home>/.plesk-mcp/wp-theme-backups/<backup_id>/ gesichert. Liefert die
# Startseite vorher keinen 5xx-Fehler und danach schon, wird automatisch aus
# dem Backup zurückgestellt.

_WP_EXT_KINDS: dict[str, dict[str, Any]] = {
    "plugin": {
        "subdir": ".plesk-mcp/wp-plugin-backups", "items": "plugins", "dir_key": "plugin_dir",
        "label": "Plugin", "label_pl": "Plugin(s)", "dir_only": False,
        "skip_status": ("must-use", "dropin"),
    },
    "theme": {
        "subdir": ".plesk-mcp/wp-theme-backups", "items": "themes", "dir_key": "theme_dir",
        "label": "Theme", "label_pl": "Theme(s)", "dir_only": True,
        "skip_status": (),
    },
}
_WP_PLUGIN_BACKUP_SUBDIR = _WP_EXT_KINDS["plugin"]["subdir"]
_WP_THEME_BACKUP_SUBDIR = _WP_EXT_KINDS["theme"]["subdir"]
_WP_PLUGIN_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_WP_PLUGIN_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.+-]{0,39}$")
_WP_EXT_FIELDS = "name,title,status,version,update,update_version"
_WP_PLUGIN_MAX = 50
_WP_PLUGIN_UPDATE_TIMEOUT = int(os.environ.get("WP_PLUGIN_UPDATE_TIMEOUT", "600"))

# Backup eines Eintrags als Subscription-User: $1 Backup-Ordner, $2 Zieldatei,
# $3 Plugin-/Theme-Verzeichnis von WordPress, $4 Eintrag darin
_WP_PLUGIN_BACKUP_SH = (
    'umask 077 && mkdir -p "$1" && [ ! -e "$2" ] && '
    'tar -czf "$2" -C "$3" -- "$4"'
)
# Wiederherstellen: entpacken in einen Temp-Ordner im Plugin-/Theme-
# Verzeichnis, dann aktuellen Stand wegschieben und den alten an seine Stelle
# setzen. $1 Verzeichnis, $2 Archiv, $3 Eintrag
_WP_PLUGIN_RESTORE_SH = (
    'set -e; cd "$1"; t=$(mktemp -d .plesk-mcp-restore.XXXXXX); '
    'if ! tar -xzf "$2" -C "$t" --no-same-owner -- "$3" || [ ! -e "$t/$3" ]; then '
    'rm -rf -- "$t"; exit 3; fi; '
    'if [ -e "$3" ] || [ -L "$3" ]; then mv -- "$3" "$t/.old"; fi; '
    'mv -- "$t/$3" "$3"; rm -rf -- "$t"'
)


def _wp_ext_slugs(items: list[str] | str, label: str = "Plugin") -> list[str]:
    values = [items] if isinstance(items, str) else list(items or [])
    out: list[str] = []
    for p in values:
        s = str(p).strip()
        if not _WP_PLUGIN_SLUG_RE.match(s) or ".." in s:
            raise ValueError(
                f"Ungültiger {label}-Name {p!r} - erwartet wird der Slug (Verzeichnisname), z.B. "
                + ("'woocommerce'." if label == "Plugin" else "'twentytwentyfive'.")
            )
        if s not in out:
            out.append(s)
    if len(out) > _WP_PLUGIN_MAX:
        raise ValueError(f"Höchstens {_WP_PLUGIN_MAX} {label}s pro Aufruf.")
    return out


def _wp_plugin_slugs(plugins: list[str] | str) -> list[str]:
    return _wp_ext_slugs(plugins, "Plugin")


def _wp_theme_slugs(themes: list[str] | str) -> list[str]:
    return _wp_ext_slugs(themes, "Theme")


def _wp_plugin_check_version(version: str) -> str:
    v = (version or "").strip()
    if v and not _WP_PLUGIN_VERSION_RE.match(v):
        raise ValueError(f"Ungültige Version {version!r}.")
    return v


def _wp_ext_list(client, ctx, wp_dir: str, kind: str) -> dict[str, dict[str, Any]]:
    rows = _wp_json(_wp_cli(
        client, ctx, wp_dir, [kind, "list", f"--fields={_WP_EXT_FIELDS}", "--format=json"]
    ))
    if not isinstance(rows, list):
        raise SSHError(f"Unerwartete Antwort von 'wp {kind} list'.")
    return {str(r.get("name")): r for r in rows if r.get("name")}


def _wp_ext_view(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": r.get("name"),
        "titel": r.get("title"),
        "status": r.get("status"),
        "version": r.get("version"),
        "update": r.get("update"),
        "update_version": r.get("update_version") or None,
    }


def _wp_ext_dir(client, ctx, wp_dir: str, domain: str, kind: str) -> str:
    """Plugin- bzw. Theme-Verzeichnis von WordPress, muss im Vhost liegen."""
    out = _wp_cli(client, ctx, wp_dir, [kind, "path"]).strip().splitlines()
    pdir = posixpath.normpath(out[-1].strip()) if out else ""
    if not pdir.startswith(f"{_VHOST_BASE}/{domain}/"):
        raise ValueError(
            f"{_WP_EXT_KINDS[kind]['label']}-Verzeichnis {pdir!r} liegt nicht unter dem Vhost von '{domain}'."
        )
    sftp = client.open_sftp()
    try:
        _assert_real_within_vhost(sftp, pdir, domain)
    finally:
        sftp.close()
    return pdir


def _wp_ext_entry(client, pdir: str, slug: str, kind: str) -> str:
    """Ordner <slug> oder (nur Plugins) Einzeldatei <slug>.php."""
    candidates = [(slug, True)]
    if not _WP_EXT_KINDS[kind]["dir_only"]:
        candidates.append((f"{slug}.php", False))
    sftp = client.open_sftp()
    try:
        for entry, want_dir in candidates:
            try:
                st = sftp.lstat(f"{pdir}/{entry}")
            except FileNotFoundError:
                continue
            if (stat.S_ISDIR(st.st_mode) if want_dir else stat.S_ISREG(st.st_mode)):
                return entry
    finally:
        sftp.close()
    raise ValueError(f"{_WP_EXT_KINDS[kind]['label']} '{slug}' nicht in {pdir} gefunden.")


def _wp_ext_backup(client, ctx, kind: str, wp_path: str, pdir: str,
                   items: dict[str, dict[str, Any]], source: str) -> str:
    """Sichert die Einträge als tar.gz und legt manifest.json daneben."""
    k = _WP_EXT_KINDS[kind]
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    backup_id = f"{ts}-{os.urandom(4).hex()}@{ctx['domain']}"
    bdir = f"{ctx['home']}/{k['subdir']}/{backup_id}"
    entries: dict[str, Any] = {}
    for slug, row in items.items():
        entry = _wp_ext_entry(client, pdir, slug, kind)
        _, err, rc = _wp_run_as_user(
            client, ctx,
            ["/bin/sh", "-c", _WP_PLUGIN_BACKUP_SH, "sh", bdir, f"{bdir}/{slug}.tar.gz", pdir, entry],
            cwd=ctx["home"], timeout=_WP_PLUGIN_UPDATE_TIMEOUT,
        )
        if rc != 0:
            raise SSHError(f"Backup von '{slug}' fehlgeschlagen - nichts geändert: {err.strip()}")
        entries[slug] = {"entry": entry, "archive": f"{slug}.tar.gz",
                         "version": row.get("version"), "status": row.get("status")}
    record = {
        "id": backup_id,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "domain": ctx["domain"],
        "wp_path": wp_path,
        k["dir_key"]: pdir,
        k["items"]: entries,
    }
    data = json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")
    _, err, rc = _wp_run_as_user(
        client, ctx,
        ["/bin/sh", "-c", 'umask 077 && set -C && cat > "$1"', "sh", f"{bdir}/manifest.json"],
        cwd=ctx["home"], stdin_data=data,
    )
    if rc != 0:
        raise SSHError(f"Backup-Manifest konnte nicht angelegt werden - nichts geändert: {err.strip()}")
    return backup_id


def _wp_ext_restore(client, ctx, kind: str, backup_id: str, record: dict[str, Any],
                    slugs: list[str]) -> list[str]:
    """Stellt die Einträge aus dem Backup zurück; gibt Fehler je Slug zurück."""
    k = _WP_EXT_KINDS[kind]
    bdir = f"{ctx['home']}/{k['subdir']}/{backup_id}"
    pdir = record[k["dir_key"]]
    errors = []
    for slug in slugs:
        e = record[k["items"]][slug]
        _, err, rc = _wp_run_as_user(
            client, ctx,
            ["/bin/sh", "-c", _WP_PLUGIN_RESTORE_SH, "sh", pdir, f"{bdir}/{e['archive']}", e["entry"]],
            cwd=pdir, timeout=_WP_PLUGIN_UPDATE_TIMEOUT,
        )
        if rc != 0:
            errors.append(f"{slug}: Wiederherstellen fehlgeschlagen ({err.strip() or f'rc={rc}'})")
    return errors


def _wp_ext_read_backup(client, ctx, kind: str, backup_id: str) -> dict[str, Any]:
    k = _WP_EXT_KINDS[kind]
    out, err, rc = _wp_run_as_user(
        client, ctx,
        ["/bin/cat", "--", f"{ctx['home']}/{k['subdir']}/{backup_id}/manifest.json"],
        cwd=ctx["home"],
    )
    if rc != 0:
        raise ValueError(f"Backup '{backup_id}' nicht gefunden: {err.strip()}")
    try:
        record = json.loads(out)
        items = record[k["items"]]
        if record.get("domain") != ctx["domain"] or not isinstance(items, dict) or not items:
            raise ValueError("Inhalt passt nicht zur backup_id")
        _wp_ext_slugs(list(items), k["label"])
        pdir = posixpath.normpath(str(record[k["dir_key"]]))
        if pdir != record[k["dir_key"]] or not pdir.startswith(f"{_VHOST_BASE}/{ctx['domain']}/"):
            raise ValueError(f"{k['label']}-Verzeichnis ungültig")
        allowed = (lambda s: (s,)) if k["dir_only"] else (lambda s: (s, f"{s}.php"))
        for slug, e in items.items():
            if e.get("entry") not in allowed(slug) or e.get("archive") != f"{slug}.tar.gz":
                raise ValueError(f"Eintrag für '{slug}' ungültig")
        if not isinstance(record["wp_path"], str):
            raise ValueError("wp_path fehlt")
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise ValueError(f"Backup '{backup_id}' ist ungültig: {e}")
    return record


def _wp_ext_targets(installed: dict[str, dict[str, Any]], slugs: list[str],
                    all_updates: bool, version: str,
                    skip_status: tuple[str, ...] = ("must-use", "dropin"),
                    ) -> tuple[list[str], dict[str, str]]:
    """Wählt die zu aktualisierenden Einträge; zweiter Wert: übersprungene mit Grund."""
    skipped: dict[str, str] = {}
    if all_updates:
        cand = [n for n, r in installed.items()
                if r.get("update") == "available" and r.get("status") not in skip_status]
    else:
        cand = []
        for s in slugs:
            r = installed.get(s)
            if r is None:
                skipped[s] = "nicht installiert"
            elif r.get("status") in skip_status:
                skipped[s] = f"{r.get('status')} - nicht per WP-CLI aktualisierbar"
            elif version:
                if str(r.get("version")) == version:
                    skipped[s] = f"Version {version} ist bereits installiert"
                else:
                    cand.append(s)
            elif r.get("update") != "available":
                skipped[s] = f"kein Update verfügbar (update={r.get('update')!r})"
            else:
                cand.append(s)
    return cand, skipped


def _wp_plugin_targets(installed, slugs, all_updates, version):
    return _wp_ext_targets(installed, slugs, all_updates, version)


def _wp_theme_hint(before: dict[str, dict[str, Any]], updated: list[str]) -> str | None:
    """Hinweis, wenn ein Child-Theme aktiv ist und sein Parent aktualisiert wurde."""
    active = next((n for n, r in before.items() if r.get("status") == "active"), None)
    parents = [s for s in updated if before.get(s, {}).get("status") == "parent"]
    if active and parents:
        return (f"Aktiv ist das Child-Theme '{active}'; aktualisiert wurde das Parent-Theme "
                f"'{', '.join(parents)}'. Anpassungen im Child-Theme (Templates, Hooks) prüfen.")
    return None


def _wp_ext_list_tool(kind: str, domain: str, wp_path: str, only_updates: bool) -> str:
    k = _WP_EXT_KINDS[kind]
    d = _wp_domain(domain)
    client = _ssh_connect()
    try:
        ctx = _wp_context(client, d)
        sftp = client.open_sftp()
        try:
            wp_dir = _wp_install_dir(sftp, d, wp_path)
        finally:
            sftp.close()
        installed = _wp_ext_list(client, ctx, wp_dir, kind)
    finally:
        client.close()
    rows = [_wp_ext_view(r) for r in installed.values()
            if not only_updates or r.get("update") == "available"]
    return json.dumps({
        "domain": d,
        "wp_path": wp_dir,
        "anzahl": len(rows),
        "updates_verfuegbar": sum(1 for r in installed.values() if r.get("update") == "available"),
        k["items"]: rows,
    }, ensure_ascii=False, indent=2)


def _wp_ext_update(kind: str, domain: str, wp_path: str, items: list[str] | None,
                   all_updates: bool, version: str, confirm: bool) -> str:
    k = _WP_EXT_KINDS[kind]
    slugs = _wp_ext_slugs(items or [], k["label"])
    v = _wp_plugin_check_version(version)
    if all_updates and slugs:
        raise ValueError(f"Entweder {k['items']} angeben oder all_updates=true, nicht beides.")
    if not all_updates and not slugs:
        raise ValueError(f"{k['items']} angeben oder all_updates=true setzen.")
    if v and (all_updates or len(slugs) != 1):
        raise ValueError(f"version ist nur mit genau einem {k['label']} möglich.")
    d = _wp_domain(domain)
    client = _ssh_connect()
    try:
        ctx = _wp_context(client, d)
        sftp = client.open_sftp()
        try:
            wp_dir = _wp_install_dir(sftp, d, wp_path)
        finally:
            sftp.close()
        installed = _wp_ext_list(client, ctx, wp_dir, kind)
        targets, skipped = _wp_ext_targets(installed, slugs, all_updates, v, k["skip_status"])
        result: dict[str, Any] = {
            "domain": d,
            "wp_path": wp_dir,
            "system_user": ctx["login"],
            "updates": [
                {"name": s, "status": installed[s].get("status"),
                 "alt": installed[s].get("version"),
                 "neu": v or installed[s].get("update_version")}
                for s in targets
            ],
        }
        if skipped:
            result["uebersprungen"] = skipped
        if not targets:
            result["ergebnis"] = "Nichts zu aktualisieren."
            return json.dumps(result, ensure_ascii=False, indent=2)
        if not confirm:
            result["ergebnis"] = "Vorschau - nichts geändert. Mit confirm=true ausführen."
            return json.dumps(result, ensure_ascii=False, indent=2)

        http_before, _ = _phps_http(d)
        pdir = _wp_ext_dir(client, ctx, wp_dir, d, kind)
        backup_id = _wp_ext_backup(
            client, ctx, kind, wp_path, pdir, {s: installed[s] for s in targets}, f"wp_{kind}_update"
        )
        args = [kind, "update", *targets]
        if v:
            args.append(f"--version={v}")
        out, err, rc = _wp_cli_run(client, ctx, wp_dir, args, timeout=_WP_PLUGIN_UPDATE_TIMEOUT)
        after = _wp_ext_list(client, ctx, wp_dir, kind)
        updated, unchanged = [], {}
        for s in targets:
            now = after.get(s, {}).get("version")
            want = v or installed[s].get("update_version")
            if now == want or (not want and now != installed[s].get("version")):
                updated.append(s)
            else:
                unchanged[s] = now or "nicht mehr vorhanden"
        http_after, http_detail = _phps_http(d)
        restored: list[str] = []
        restore_errors: list[str] = []
        if http_before is not None and http_before < 500 and http_after is not None and http_after >= 500:
            record = _wp_ext_read_backup(client, ctx, kind, backup_id)
            restore_errors = _wp_ext_restore(client, ctx, kind, backup_id, record, targets)
            restored = [s for s in targets if not any(e.startswith(f"{s}:") for e in restore_errors)]
            http_after, http_detail = _phps_http(d)
    finally:
        client.close()

    result["backup_id"] = backup_id
    result["backup_ordner"] = f"{ctx['home']}/{k['subdir']}/{backup_id}"
    result["aktualisiert"] = [] if restored else updated
    result["startseite"] = http_detail
    if rc != 0:
        result["wp_cli_fehler"] = _wp_cli_error(out, err, lines=10)
    if restored or restore_errors:
        result["ergebnis"] = (
            f"ZURÜCKGESTELLT: Die Startseite lieferte nach dem Update einen 5xx-Fehler - "
            f"{k['label_pl']} aus dem Backup wiederhergestellt."
            if not restore_errors else
            f"ACHTUNG: Die Startseite lieferte nach dem Update einen 5xx-Fehler - "
            f"Zurückstellen teilweise fehlgeschlagen!"
        )
        result["automatisch_zurueckgestellt"] = True
        result["zurueckgestellt"] = restored
        if restore_errors:
            result["fehler"] = restore_errors
    elif unchanged or rc != 0:
        result["ergebnis"] = "Teilweise fehlgeschlagen - Rollback mit backup_id möglich."
        result["nicht_aktualisiert"] = unchanged
    else:
        result["ergebnis"] = f"{len(updated)} {k['label_pl']} aktualisiert und kontrolliert."
    if kind == "theme" and not restored:
        hint = _wp_theme_hint(installed, updated)
        if hint:
            result["hinweis_child_theme"] = hint
    return json.dumps(result, ensure_ascii=False, indent=2)


def _wp_ext_rollback(kind: str, backup_id: str, items: list[str] | None, confirm: bool) -> str:
    k = _WP_EXT_KINDS[kind]
    m = _WP_BACKUP_ID_RE.match((backup_id or "").strip())
    if not m:
        raise ValueError(f"Ungültige backup_id: {backup_id!r}")
    only = _wp_ext_slugs(items or [], k["label"])
    bid = m.group(0)
    d = _wp_domain(m.group(3))
    client = _ssh_connect()
    try:
        ctx = _wp_context(client, d)
        record = _wp_ext_read_backup(client, ctx, kind, bid)
        missing = [s for s in only if s not in record[k["items"]]]
        if missing:
            raise ValueError(f"Nicht im Backup enthalten: {', '.join(missing)}")
        slugs = only or list(record[k["items"]])
        sftp = client.open_sftp()
        try:
            wp_dir = _wp_install_dir(sftp, d, record["wp_path"])
            _assert_real_within_vhost(sftp, record[k["dir_key"]], d)
        finally:
            sftp.close()
        installed = _wp_ext_list(client, ctx, wp_dir, kind)
        result: dict[str, Any] = {
            "domain": d,
            "wp_path": wp_dir,
            "backup_id": bid,
            k["items"]: [
                {"name": s, "version_backup": record[k["items"]][s].get("version"),
                 "version_aktuell": installed.get(s, {}).get("version") or "nicht installiert"}
                for s in slugs
            ],
        }
        if not confirm:
            result["ergebnis"] = "Vorschau - nichts geändert. Mit confirm=true ausführen."
            return json.dumps(result, ensure_ascii=False, indent=2)

        present = {s: installed[s] for s in slugs if s in installed}
        new_bid = None
        if present:
            new_bid = _wp_ext_backup(
                client, ctx, kind, record["wp_path"], record[k["dir_key"]], present,
                f"wp_{kind}_rollback:{bid}",
            )
        errors = _wp_ext_restore(client, ctx, kind, bid, record, slugs)
        after = _wp_ext_list(client, ctx, wp_dir, kind)
    finally:
        client.close()

    mismatches = {
        s: after.get(s, {}).get("version") or "nicht vorhanden"
        for s in slugs
        if after.get(s, {}).get("version") != record[k["items"]][s].get("version")
    }
    if new_bid:
        result["neues_backup_id"] = new_bid
    if errors or mismatches:
        result["ergebnis"] = "Teilweise fehlgeschlagen."
        if errors:
            result["fehler"] = errors
        if mismatches:
            result["abweichend_nach_kontrolle"] = mismatches
    else:
        result["ergebnis"] = f"{len(slugs)} {k['label_pl']} zurückgestellt und kontrolliert."
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
@_wp_reporting
def wp_plugin_list(domain: str, wp_path: str, only_updates: bool = False) -> str:
    """Listet die Plugins einer WordPress-Installation per WP-CLI mit Status,
    installierter Version und verfügbarem Update (`wp plugin list`).

    domain: z.B. "example.com"; wp_path: WordPress-Verzeichnis relativ zu
    /var/www/vhosts/<domain>/ (z.B. "httpdocs").
    only_updates=true: nur Plugins mit verfügbarem Update. Rein lesend.
    """
    return _wp_ext_list_tool("plugin", domain, wp_path, only_updates)


@mcp.tool()
@_wp_reporting
def wp_plugin_update(
    domain: str,
    wp_path: str,
    plugins: list[str] | None = None,
    all_updates: bool = False,
    version: str = "",
    confirm: bool = False,
) -> str:
    """Aktualisiert WordPress-Plugins per WP-CLI (`wp plugin update`, als
    System-User der Subscription, nie root).

    domain: z.B. "example.com"; wp_path: WordPress-Verzeichnis relativ zu
    /var/www/vhosts/<domain>/ (z.B. "httpdocs").
    plugins: Liste von Plugin-Slugs (Verzeichnisname, z.B. "woocommerce"),
    oder all_updates=true für alle Plugins mit verfügbarem Update (nicht
    beides). version: optional eine bestimmte Version (nur mit genau einem
    Plugin, auch für ein Downgrade; muss auf wordpress.org verfügbar sein).
    Ohne confirm=true nur Vorschau (installierte und neue Version je Plugin,
    nichts geändert). Mit confirm=true: Backup jedes Plugins als tar.gz unter
    /var/www/vhosts/<domain>/.plesk-mcp/wp-plugin-backups/<backup_id>/, dann
    Update, danach Kontrolle der Versionen und der Startseite. Liefert die
    Startseite vorher keinen 5xx-Fehler und danach schon, werden die Plugins
    automatisch zurückgestellt. Rückgängig machen: wp_plugin_rollback(backup_id).
    """
    return _wp_ext_update("plugin", domain, wp_path, plugins, all_updates, version, confirm)


@mcp.tool()
@_wp_reporting
def wp_plugin_rollback(backup_id: str, plugins: list[str] | None = None, confirm: bool = False) -> str:
    """Stellt Plugins aus einem Backup von wp_plugin_update wieder her
    (kompletter damaliger Plugin-Ordner bzw. -Datei).

    plugins: optional nur diese Slugs aus dem Backup, sonst alle.
    Ohne confirm=true nur Vorschau (Version im Backup vs. aktuell). Vor dem
    Zurückstellen wird der aktuelle Stand selbst wieder gesichert (neue
    backup_id in der Rückgabe). Datenbank-Änderungen, die ein Plugin-Update
    gemacht hat, werden nicht zurückgestellt.
    """
    return _wp_ext_rollback("plugin", backup_id, plugins, confirm)


@mcp.tool()
@_wp_reporting
def wp_theme_list(domain: str, wp_path: str, only_updates: bool = False) -> str:
    """Listet die Themes einer WordPress-Installation per WP-CLI mit Status
    (active/parent/inactive), installierter Version und verfügbarem Update
    (`wp theme list`).

    domain: z.B. "example.com"; wp_path: WordPress-Verzeichnis relativ zu
    /var/www/vhosts/<domain>/ (z.B. "httpdocs").
    only_updates=true: nur Themes mit verfügbarem Update. Rein lesend.
    """
    return _wp_ext_list_tool("theme", domain, wp_path, only_updates)


@mcp.tool()
@_wp_reporting
def wp_theme_update(
    domain: str,
    wp_path: str,
    themes: list[str] | None = None,
    all_updates: bool = False,
    version: str = "",
    confirm: bool = False,
) -> str:
    """Aktualisiert WordPress-Themes per WP-CLI (`wp theme update`, als
    System-User der Subscription, nie root).

    domain: z.B. "example.com"; wp_path: WordPress-Verzeichnis relativ zu
    /var/www/vhosts/<domain>/ (z.B. "httpdocs").
    themes: Liste von Theme-Slugs (Verzeichnisname, z.B. "twentytwentyfive"),
    oder all_updates=true für alle Themes mit verfügbarem Update (nicht
    beides). version: optional eine bestimmte Version (nur mit genau einem
    Theme).
    Ohne confirm=true nur Vorschau (installierte und neue Version je Theme,
    nichts geändert). Mit confirm=true: Backup jedes Theme-Ordners als tar.gz
    unter /var/www/vhosts/<domain>/.plesk-mcp/wp-theme-backups/<backup_id>/,
    dann Update, danach Kontrolle der Versionen und der Startseite. Liefert
    die Startseite vorher keinen 5xx-Fehler und danach schon, werden die
    Themes automatisch zurückgestellt. Ist ein Child-Theme aktiv und wurde
    sein Parent-Theme aktualisiert, enthält das Ergebnis einen Hinweis.
    Rückgängig machen: wp_theme_rollback(backup_id).
    """
    return _wp_ext_update("theme", domain, wp_path, themes, all_updates, version, confirm)


@mcp.tool()
@_wp_reporting
def wp_theme_rollback(backup_id: str, themes: list[str] | None = None, confirm: bool = False) -> str:
    """Stellt Themes aus einem Backup von wp_theme_update wieder her
    (kompletter damaliger Theme-Ordner).

    themes: optional nur diese Slugs aus dem Backup, sonst alle.
    Ohne confirm=true nur Vorschau (Version im Backup vs. aktuell). Vor dem
    Zurückstellen wird der aktuelle Stand selbst wieder gesichert (neue
    backup_id in der Rückgabe).
    """
    return _wp_ext_rollback("theme", backup_id, themes, confirm)


# ---------------------------------------------------------------------------
# WooCommerce-Gutscheine (Coupons) über die WooCommerce-CLI (wp wc shop_coupon)
# ---------------------------------------------------------------------------

_WC_COUPON_BACKUP_SUBDIR = ".plesk-mcp/wc-coupon-backups"
_WC_COUPON_CODE_RE = re.compile(r"^[^\s\x00'\"\\]{1,100}$")
_WC_DISCOUNT_TYPES = ("percent", "fixed_cart", "fixed_product")
_WC_COUPON_STATUSES = ("any", "publish", "draft", "pending", "private", "trash")
# Erlaubte Felder für create/update: Name -> Typ (REST-Schema von WooCommerce)
_WC_COUPON_FIELDS: dict[str, str] = {
    "code": "str", "amount": "money", "discount_type": "enum", "description": "str",
    "date_expires": "date", "individual_use": "bool", "product_ids": "ids",
    "excluded_product_ids": "ids", "usage_limit": "int", "usage_limit_per_user": "int",
    "limit_usage_to_x_items": "int", "free_shipping": "bool", "product_categories": "ids",
    "excluded_product_categories": "ids", "exclude_sale_items": "bool",
    "minimum_amount": "money", "maximum_amount": "money", "email_restrictions": "emails",
    "status": "str",
}
_WC_COUPON_LIST_FIELDS = (
    "id,code,amount,discount_type,status,description,date_expires,usage_count,"
    "usage_limit,usage_limit_per_user,individual_use,free_shipping,minimum_amount,"
    "maximum_amount,product_ids,excluded_product_ids,product_categories,"
    "excluded_product_categories,exclude_sale_items,email_restrictions,date_created,date_modified"
)
_WC_MONEY_RE = re.compile(r"^\d{1,9}(\.\d{1,4})?$")
_WC_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2})?$")
_WC_EMAIL_RE = re.compile(r"^[^@\s'\"\\]{1,64}@[^@\s'\"\\]{1,190}$")


def _wc_check_code(code: str) -> str:
    c = str(code).strip()
    if not _WC_COUPON_CODE_RE.match(c):
        raise ValueError(f"Ungültiger Gutscheincode {code!r} (max. 100 Zeichen, keine Leer-/Anführungszeichen).")
    return c


def _wc_check_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Validiert Gutschein-Felder gegen die Whitelist und normalisiert die Typen."""
    if not isinstance(fields, dict) or not fields:
        raise ValueError("fields muss ein nicht-leeres dict sein.")
    out: dict[str, Any] = {}
    for key, val in fields.items():
        kind = _WC_COUPON_FIELDS.get(key)
        if kind is None:
            raise ValueError(f"Unbekanntes Feld {key!r}. Erlaubt: {', '.join(_WC_COUPON_FIELDS)}")
        if kind == "str":
            if key == "code":
                out[key] = _wc_check_code(val)
                continue
            if key == "status" and val not in _WC_COUPON_STATUSES[1:]:
                raise ValueError(f"status muss eines von {', '.join(_WC_COUPON_STATUSES[1:])} sein.")
            s = "" if val is None else str(val)
            if "\x00" in s or len(s) > 2000:
                raise ValueError(f"Ungültiger Wert für {key}.")
            out[key] = s
        elif kind == "money":
            s = "" if val is None else str(val).strip()
            if s and not _WC_MONEY_RE.match(s):
                raise ValueError(f"{key} muss ein Betrag wie 10 oder 12.50 sein (oder leer).")
            out[key] = s
        elif kind == "enum":
            if val not in _WC_DISCOUNT_TYPES:
                raise ValueError(f"discount_type muss eines von {', '.join(_WC_DISCOUNT_TYPES)} sein.")
            out[key] = val
        elif kind == "date":
            s = "" if val is None else str(val).strip()
            if s and not _WC_DATE_RE.match(s):
                raise ValueError(f"{key} muss YYYY-MM-DD oder YYYY-MM-DDTHH:MM:SS sein (leer = kein Ablauf).")
            out[key] = s or None
        elif kind == "bool":
            if not isinstance(val, bool):
                raise ValueError(f"{key} muss true/false sein.")
            out[key] = val
        elif kind == "int":
            if val is None or val == "":
                out[key] = 0
            elif isinstance(val, bool) or not isinstance(val, int) or val < 0:
                raise ValueError(f"{key} muss eine ganze Zahl >= 0 sein (0 = unbegrenzt).")
            else:
                out[key] = val
        elif kind == "ids":
            if not isinstance(val, list) or any(isinstance(i, bool) or not isinstance(i, int) or i <= 0 for i in val):
                raise ValueError(f"{key} muss eine Liste positiver IDs sein (leere Liste = keine).")
            out[key] = list(dict.fromkeys(val))
        elif kind == "emails":
            if not isinstance(val, list) or any(not isinstance(e, str) or not _WC_EMAIL_RE.match(e.strip()) for e in val):
                raise ValueError("email_restrictions muss eine Liste gültiger E-Mail-Adressen sein.")
            out[key] = [e.strip().lower() for e in val]
    return out


def _wc_cli_args(fields: dict[str, Any]) -> list[str]:
    """Felder als --key=value für die WooCommerce-CLI (Listen als JSON)."""
    args = []
    for key, val in fields.items():
        if isinstance(val, bool):
            args.append(f"--{key}={'true' if val else 'false'}")
        elif isinstance(val, list):
            args.append(f"--{key}={json.dumps(val)}")
        elif val is None:
            args.append(f"--{key}=")
        else:
            args.append(f"--{key}={val}")
    return args


def _wc_context(client, ctx, wp_dir: str) -> str:
    """Prüft WooCommerce und liefert die ID eines Administrators für --user."""
    _, err, rc = _wp_cli_run(client, ctx, wp_dir, ["plugin", "is-active", "woocommerce"])
    if rc != 0:
        raise ValueError("WooCommerce ist in dieser Installation nicht aktiv.")
    out = _wp_cli(client, ctx, wp_dir, ["user", "list", "--role=administrator", "--field=ID",
                                        "--orderby=ID", "--order=ASC", "--number=1"])
    uid = out.strip().splitlines()[0].strip() if out.strip() else ""
    if not uid.isdigit():
        raise ValueError("Kein Administrator-Benutzer gefunden - die WooCommerce-CLI braucht einen (--user).")
    return uid


def _wc_coupon_view(c: dict[str, Any]) -> dict[str, Any]:
    keys = _WC_COUPON_LIST_FIELDS.split(",")
    return {k: c.get(k) for k in keys if k in c}


def _wc_coupon_find(client, ctx, wp_dir: str, uid: str, coupon: str) -> dict[str, Any] | None:
    """Gutschein per ID (Ziffern) oder Code; None wenn nicht vorhanden."""
    c = str(coupon).strip()
    if c.isdigit():
        out, err, rc = _wp_cli_run(client, ctx, wp_dir, [
            "wc", "shop_coupon", "get", c, f"--user={uid}", f"--fields={_WC_COUPON_LIST_FIELDS}", "--format=json",
        ])
        if rc != 0:
            if "invalid" in (err + out).lower() or "not found" in (err + out).lower():
                return None
            raise SSHError(f"WP-CLI 'wp wc shop_coupon get' fehlgeschlagen: {_wp_cli_error(out, err)[-300:]}")
        data = _wp_json(out)
        return data if isinstance(data, dict) and data.get("id") else None
    code = _wc_check_code(c)
    rows = _wp_json(_wp_cli(client, ctx, wp_dir, [
        "wc", "shop_coupon", "list", f"--user={uid}", f"--code={code}", "--status=any",
        f"--fields={_WC_COUPON_LIST_FIELDS}", "--format=json",
    ]))
    if not isinstance(rows, list):
        raise SSHError("Unerwartete Antwort von 'wp wc shop_coupon list'.")
    exact = [r for r in rows if str(r.get("code", "")).lower() == code.lower()]
    return exact[0] if exact else None


def _wc_coupon_backup(client, ctx, wp_path: str, coupon: dict[str, Any], source: str) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    backup_id = f"{ts}-{os.urandom(4).hex()}@{ctx['domain']}"
    record = {"id": backup_id, "created": datetime.datetime.now().isoformat(timespec="seconds"),
              "source": source, "domain": ctx["domain"], "wp_path": wp_path, "coupon": coupon}
    data = json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")
    bdir = f"{ctx['home']}/{_WC_COUPON_BACKUP_SUBDIR}"
    _, err, rc = _wp_run_as_user(
        client, ctx,
        ["/bin/sh", "-c", 'umask 077 && mkdir -p "$1" && set -C && cat > "$2"',
         "sh", bdir, f"{bdir}/{backup_id}.json"],
        cwd=ctx["home"], stdin_data=data,
    )
    if rc != 0:
        raise SSHError(f"Backup konnte nicht angelegt werden - nichts geändert: {err.strip()}")
    return backup_id


def _wc_open(domain: str, wp_path: str):
    """SSH + Kontext + WP-Verzeichnis + Admin-User; Aufrufer schliesst client."""
    d = _wp_domain(domain)
    client = _ssh_connect()
    try:
        ctx = _wp_context(client, d)
        sftp = client.open_sftp()
        try:
            wp_dir = _wp_install_dir(sftp, d, wp_path)
        finally:
            sftp.close()
        uid = _wc_context(client, ctx, wp_dir)
    except Exception:
        client.close()
        raise
    return client, ctx, wp_dir, uid, d


@mcp.tool()
@_wp_reporting
def wc_coupon_list(
    domain: str,
    wp_path: str,
    search: str = "",
    status: str = "any",
    limit: int = 50,
) -> str:
    """Listet WooCommerce-Gutscheine (Coupons) einer Installation über die
    WooCommerce-CLI (`wp wc shop_coupon list`), neueste zuerst.

    domain: z.B. "example.com"; wp_path: WordPress-Verzeichnis relativ zu
    /var/www/vhosts/<domain>/ (z.B. "httpdocs").
    search: optional Textsuche (Code/Beschreibung). status: "any" (Standard),
    "publish", "draft", "pending", "private" oder "trash". limit: 1-100.
    Rein lesend.
    """
    if status not in _WC_COUPON_STATUSES:
        raise ValueError(f"status muss eines von {', '.join(_WC_COUPON_STATUSES)} sein.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit muss zwischen 1 und 100 liegen.")
    q = search.strip()
    if "\x00" in q or len(q) > 200:
        raise ValueError("Ungültiger Suchtext.")
    client, ctx, wp_dir, uid, d = _wc_open(domain, wp_path)
    try:
        args = ["wc", "shop_coupon", "list", f"--user={uid}", f"--status={status}",
                f"--per_page={limit}", "--orderby=date", "--order=desc",
                f"--fields={_WC_COUPON_LIST_FIELDS}", "--format=json"]
        if q:
            args.append(f"--search={q}")
        rows = _wp_json(_wp_cli(client, ctx, wp_dir, args))
    finally:
        client.close()
    if not isinstance(rows, list):
        raise SSHError("Unerwartete Antwort von 'wp wc shop_coupon list'.")
    return json.dumps({"domain": d, "wp_path": wp_dir, "status": status, "anzahl": len(rows),
                       "gutscheine": [_wc_coupon_view(r) for r in rows]}, ensure_ascii=False, indent=2)


@mcp.tool()
@_wp_reporting
def wc_coupon_get(domain: str, wp_path: str, coupon: str) -> str:
    """Zeigt einen WooCommerce-Gutschein mit allen Feldern - coupon ist die
    ID (Ziffern) oder der Gutscheincode. Rein lesend.
    """
    client, ctx, wp_dir, uid, d = _wc_open(domain, wp_path)
    try:
        data = _wc_coupon_find(client, ctx, wp_dir, uid, coupon)
    finally:
        client.close()
    if data is None:
        raise ValueError(f"Gutschein {coupon!r} nicht gefunden.")
    return json.dumps({"domain": d, "wp_path": wp_dir, "gutschein": _wc_coupon_view(data)},
                      ensure_ascii=False, indent=2)


@mcp.tool()
@_wp_reporting
def wc_coupon_create(
    domain: str,
    wp_path: str,
    code: str,
    discount_type: str,
    amount: str,
    fields: dict[str, Any] | None = None,
    confirm: bool = False,
) -> str:
    """Legt einen WooCommerce-Gutschein an (`wp wc shop_coupon create`, als
    Subscription-User, nie root; WooCommerce validiert selbst).

    code: Gutscheincode; discount_type: "percent", "fixed_cart" oder
    "fixed_product"; amount: Betrag bzw. Prozent als Zahl, z.B. "10" oder
    "12.50". fields: weitere optionale Felder, z.B. {"description": "...",
    "date_expires": "2026-12-31", "usage_limit": 100, "usage_limit_per_user": 1,
    "individual_use": true, "free_shipping": false, "minimum_amount": "50",
    "product_ids": [12, 34], "email_restrictions": ["a@example.com"],
    "exclude_sale_items": true}. Erlaubte Feldnamen siehe Fehlermeldung.
    Ohne confirm=true nur Vorschau (und Prüfung, ob der Code schon existiert).
    Mit confirm=true wird der Gutschein angelegt und danach zur Kontrolle
    erneut gelesen.
    """
    base = _wc_check_fields({"code": code, "discount_type": discount_type, "amount": amount})
    extra = _wc_check_fields(fields) if fields else {}
    for k in ("code", "discount_type", "amount"):
        extra.pop(k, None)
    data = {**base, **extra}
    client, ctx, wp_dir, uid, d = _wc_open(domain, wp_path)
    try:
        existing = _wc_coupon_find(client, ctx, wp_dir, uid, data["code"])
        result: dict[str, Any] = {"domain": d, "wp_path": wp_dir, "felder": data}
        if existing:
            result["ergebnis"] = f"Code existiert bereits (ID {existing.get('id')}, Status {existing.get('status')}) - nichts angelegt. Für Änderungen wc_coupon_update verwenden."
            result["vorhanden"] = _wc_coupon_view(existing)
            return json.dumps(result, ensure_ascii=False, indent=2)
        if not confirm:
            result["ergebnis"] = "Vorschau - nichts angelegt. Mit confirm=true ausführen."
            return json.dumps(result, ensure_ascii=False, indent=2)
        out = _wp_cli(client, ctx, wp_dir, ["wc", "shop_coupon", "create", f"--user={uid}",
                                            "--porcelain", *_wc_cli_args(data)])
        new_id = out.strip().splitlines()[-1].strip() if out.strip() else ""
        created = _wc_coupon_find(client, ctx, wp_dir, uid, new_id) if new_id.isdigit() else None
    finally:
        client.close()
    if not created:
        raise SSHError(f"Gutschein angelegt, aber Kontrolle fehlgeschlagen (Ausgabe: {out.strip()[-200:]!r}).")
    result["ergebnis"] = f"Gutschein angelegt (ID {created['id']}) und kontrolliert."
    result["gutschein"] = _wc_coupon_view(created)
    return json.dumps(result, ensure_ascii=False, indent=2)


def _wc_coupon_change(domain: str, wp_path: str, coupon: str, fields: dict[str, Any],
                      confirm: bool, source: str) -> str:
    data = _wc_check_fields(fields)
    client, ctx, wp_dir, uid, d = _wc_open(domain, wp_path)
    try:
        before = _wc_coupon_find(client, ctx, wp_dir, uid, coupon)
        if before is None:
            raise ValueError(f"Gutschein {coupon!r} nicht gefunden.")
        changes = {k: {"alt": before.get(k), "neu": v} for k, v in data.items()
                   if _wc_norm(before.get(k)) != _wc_norm(v)}
        result: dict[str, Any] = {"domain": d, "wp_path": wp_dir, "id": before["id"],
                                  "code": before.get("code"), "aenderungen": changes}
        if not changes:
            result["ergebnis"] = "Keine Änderung nötig - alle Werte sind bereits gesetzt."
            return json.dumps(result, ensure_ascii=False, indent=2)
        if not confirm:
            result["ergebnis"] = "Vorschau - nichts geändert. Mit confirm=true ausführen."
            return json.dumps(result, ensure_ascii=False, indent=2)
        backup_id = _wc_coupon_backup(client, ctx, wp_path, before, source)
        _wp_cli(client, ctx, wp_dir, ["wc", "shop_coupon", "update", str(before["id"]),
                                      f"--user={uid}", *_wc_cli_args({k: data[k] for k in changes})])
        after = _wc_coupon_find(client, ctx, wp_dir, uid, str(before["id"]))
    finally:
        client.close()
    result["backup_id"] = backup_id
    result["backup_datei"] = f"{ctx['home']}/{_WC_COUPON_BACKUP_SUBDIR}/{backup_id}.json"
    mism = [k for k in changes if after is None or _wc_norm(after.get(k)) != _wc_norm(data[k])]
    if mism:
        result["ergebnis"] = "Geschrieben, aber Kontrolle weicht ab - Rollback mit backup_id möglich."
        result["abweichend_nach_kontrolle"] = {k: (after or {}).get(k) for k in mism}
    else:
        result["ergebnis"] = "Geschrieben und kontrolliert."
    if after:
        result["gutschein"] = _wc_coupon_view(after)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
@_wp_reporting
def wc_coupon_update(
    domain: str,
    wp_path: str,
    coupon: str,
    fields: dict[str, Any],
    confirm: bool = False,
) -> str:
    """Ändert Felder eines WooCommerce-Gutscheins (`wp wc shop_coupon update`).
    coupon: ID oder Code. fields: dict Feld -> neuer Wert, dieselben Felder
    wie bei wc_coupon_create (inkl. "code", "amount", "discount_type",
    "status" z.B. "draft" zum Deaktivieren, "date_expires": "" = kein Ablauf).
    Ohne confirm=true nur Vorschau alt/neu je Feld. Mit confirm=true vorher
    JSON-Backup des alten Stands ausserhalb des Webroots (backup_id für
    wc_coupon_rollback), dann Schreiben und Kontrolle je Feld.
    """
    return _wc_coupon_change(domain, wp_path, coupon, fields, confirm, "wc_coupon_update")


def _wc_norm(v: Any) -> Any:
    """Vergleichsnormalisierung: WooCommerce liefert Beträge als String,
    Datum als ISO mit Zeit, Listen ggf. in anderer Reihenfolge."""
    if v is None or v == "":
        return ""
    if isinstance(v, bool):
        return v
    if isinstance(v, list):
        return sorted(str(x).lower() for x in v)
    s = str(v).strip()
    if _WC_MONEY_RE.match(s):
        return f"{float(s):.4f}"
    if _WC_DATE_RE.match(s):
        return s[:10]
    return s


@mcp.tool()
@_wp_reporting
def wc_coupon_rollback(backup_id: str, confirm: bool = False) -> str:
    """Stellt die bei wc_coupon_update gesicherten Felder eines Gutscheins
    wieder her. Ohne confirm=true nur Vorschau; legt vorher selbst ein
    neues Backup an.
    """
    m = _WP_BACKUP_ID_RE.match(backup_id.strip())
    if not m:
        raise ValueError(f"Ungültige backup_id: {backup_id!r}")
    bid = m.group(0)
    d = _wp_domain(m.group(3))
    client = _ssh_connect()
    try:
        ctx = _wp_context(client, d)
        out, err, rc = _wp_run_as_user(
            client, ctx, ["/bin/cat", "--", f"{ctx['home']}/{_WC_COUPON_BACKUP_SUBDIR}/{bid}.json"],
            cwd=ctx["home"],
        )
    finally:
        client.close()
    if rc != 0:
        raise ValueError(f"Backup '{bid}' nicht gefunden: {err.strip()}")
    try:
        record = json.loads(out)
        coupon = record["coupon"]
        wp_path = record["wp_path"]
        if record.get("domain") != d or not isinstance(coupon, dict) or not coupon.get("id"):
            raise ValueError("Inhalt passt nicht zur backup_id")
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError(f"Backup '{bid}' ist ungültig: {e}")
    fields = {k: coupon[k] for k in _WC_COUPON_FIELDS if k in coupon}
    if "date_expires" in fields and fields["date_expires"]:
        fields["date_expires"] = str(fields["date_expires"])[:19]
    for k in ("amount", "minimum_amount", "maximum_amount"):
        if k in fields and fields[k] is not None:
            fields[k] = str(fields[k])
    return _wc_coupon_change(d, wp_path, str(coupon["id"]), fields, confirm, f"wc_coupon_rollback:{bid}")


@mcp.tool()
@_wp_reporting
def wc_coupon_delete(
    domain: str,
    wp_path: str,
    coupon: str,
    permanent: bool = False,
    confirm: bool = False,
) -> str:
    """Löscht einen WooCommerce-Gutschein (`wp wc shop_coupon delete`):
    standardmässig in den Papierkorb (in WordPress wiederherstellbar),
    permanent=true endgültig (--force). coupon: ID oder Code.
    Ohne confirm=true nur Vorschau. Vor dem Löschen wird ein JSON-Backup
    des Gutscheins ausserhalb des Webroots abgelegt.
    """
    client, ctx, wp_dir, uid, d = _wc_open(domain, wp_path)
    try:
        before = _wc_coupon_find(client, ctx, wp_dir, uid, coupon)
        if before is None:
            raise ValueError(f"Gutschein {coupon!r} nicht gefunden.")
        result: dict[str, Any] = {"domain": d, "wp_path": wp_dir, "permanent": permanent,
                                  "gutschein": _wc_coupon_view(before)}
        if not confirm:
            result["ergebnis"] = "Vorschau - nichts gelöscht. Mit confirm=true ausführen."
            return json.dumps(result, ensure_ascii=False, indent=2)
        backup_id = _wc_coupon_backup(client, ctx, wp_path, before, "wc_coupon_delete")
        args = ["wc", "shop_coupon", "delete", str(before["id"]), f"--user={uid}"]
        if permanent:
            args.append("--force")
        _wp_cli(client, ctx, wp_dir, args)
        after = _wc_coupon_find(client, ctx, wp_dir, uid, str(before["id"]))
    finally:
        client.close()
    result["backup_id"] = backup_id
    if permanent:
        result["ergebnis"] = "Endgültig gelöscht." if after is None else "Löschen fehlgeschlagen - Gutschein existiert noch."
    else:
        st = (after or {}).get("status")
        result["ergebnis"] = "In den Papierkorb verschoben." if st == "trash" else f"Status nach Löschen: {st!r} (erwartet 'trash')."
    return json.dumps(result, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# WooCommerce-Bestellungen: lesen und als verschickt markieren (mit Tracking)
# ---------------------------------------------------------------------------

_WC_TRACKING_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{3,49}$")
_WC_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,49}$")
# Meta-Felder, in denen Versand-Plugins die Sendungsnummer ablegen (z.B. das
# Post-Label-Plugin), als Quelle, wenn keine Sendungsnummer übergeben wird.
_WC_LABEL_TRACKING_KEYS = ("_spap_tracking",)

# Fester PHP-Code für `wp eval`; alle Werte kommen als JSON per stdin, nie in
# den Code. Bestellungen über die WooCommerce-API (HPOS-kompatibel).
_WC_ORDER_VIEW_PHP = r'''
function plmcp_order_view($o, $label_keys) {
    $items = array();
    foreach ($o->get_items() as $it) { $items[] = array("name" => $it->get_name(), "qty" => $it->get_quantity(), "total" => $it->get_total()); }
    $label = array();
    foreach ($label_keys as $k) { $v = $o->get_meta($k); if ($v !== "" && $v !== null) { $label[$k] = (string) $v; } }
    $tr = $o->get_meta("_wc_shipment_tracking_items");
    return array(
        "id" => $o->get_id(), "number" => $o->get_order_number(), "status" => $o->get_status(),
        "total" => $o->get_total(), "currency" => $o->get_currency(),
        "date_created" => $o->get_date_created() ? $o->get_date_created()->date("c") : null,
        "billing" => array("name" => trim($o->get_billing_first_name() . " " . $o->get_billing_last_name()), "company" => $o->get_billing_company(), "email" => $o->get_billing_email()),
        "shipping" => array("name" => trim($o->get_shipping_first_name() . " " . $o->get_shipping_last_name()), "company" => $o->get_shipping_company(), "address" => trim($o->get_shipping_address_1() . " " . $o->get_shipping_address_2()), "postcode" => $o->get_shipping_postcode(), "city" => $o->get_shipping_city(), "country" => $o->get_shipping_country()),
        "payment_method" => $o->get_payment_method_title(),
        "items" => $items,
        "tracking_items" => is_array($tr) ? array_values($tr) : array(),
        "label_tracking" => $label,
    );
}
$in = json_decode(stream_get_contents(STDIN), true);
$o = wc_get_order((int) $in["id"]);
if (!$o || $o->get_type() !== "shop_order") { fwrite(STDERR, "Bestellung nicht gefunden"); exit(1); }
'''

_WC_ORDER_GET_PHP = _WC_ORDER_VIEW_PHP + r'''
echo json_encode(array("order" => plmcp_order_view($o, $in["label_keys"]),
    "tracking_function" => function_exists("wc_st_add_tracking_number")));
'''

_WC_ORDER_SHIP_PHP = _WC_ORDER_VIEW_PHP + r'''
$nr = (string) $in["tracking_number"]; $prov = (string) $in["provider"];
$done = array();
$exists = false;
$tr = $o->get_meta("_wc_shipment_tracking_items");
if (is_array($tr)) { foreach ($tr as $t) { if (isset($t["tracking_number"]) && (string) $t["tracking_number"] === $nr) { $exists = true; } } }
if (!$exists) {
    if (!function_exists("wc_st_add_tracking_number")) { fwrite(STDERR, "Kein Shipment-Tracking-Plugin aktiv (wc_st_add_tracking_number fehlt) - nichts geändert"); exit(1); }
    wc_st_add_tracking_number($o->get_id(), $nr, $prov, current_time("Y-m-d"));
    $done[] = "tracking_hinzugefuegt";
} else { $done[] = "tracking_bereits_vorhanden"; }
$o = wc_get_order($o->get_id());
$note = "Als verschickt markiert (plesk-mcp), Sendungsnummer " . $nr . ".";
if ($in["set_completed"] && $o->get_status() !== "completed") {
    $o->update_status("completed", $note . " ");
    $done[] = "status_completed";
} elseif (!$exists) {
    $o->add_order_note($note);
}
$o = wc_get_order($o->get_id());
echo json_encode(array("done" => $done, "order" => plmcp_order_view($o, $in["label_keys"])));
'''


def _wc_check_order_id(order_id: Any) -> int:
    if isinstance(order_id, bool):
        raise ValueError("order_id muss eine Bestellnummer (Ziffern) sein.")
    s = str(order_id).strip().lstrip("#")
    if not s.isdigit() or not 0 < int(s) < 10**9:
        raise ValueError(f"Ungültige Bestellnummer {order_id!r}.")
    return int(s)


def _wc_check_tracking(nr: str) -> str:
    s = str(nr or "").strip().replace(" ", "")
    if not _WC_TRACKING_RE.match(s):
        raise ValueError(f"Ungültige Sendungsnummer {nr!r} (4-50 Zeichen, Buchstaben/Ziffern/Punkt/Bindestrich).")
    return s


def _wc_check_provider(provider: str) -> str:
    p = str(provider or "").strip().lower()
    if not _WC_PROVIDER_RE.match(p):
        raise ValueError(f"Ungültiger Versanddienstleister {provider!r} (Slug wie 'swiss-post').")
    return p


def _wc_order_eval(client, ctx, wp_dir: str, php: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = _wp_eval(client, ctx, wp_dir, php,
                    stdin_data=json.dumps({**payload, "label_keys": list(_WC_LABEL_TRACKING_KEYS)}).encode("utf-8"))
    if not isinstance(data, dict) or "order" not in data:
        raise SSHError("Unerwartete Antwort beim Lesen der Bestellung.")
    return data


@mcp.tool()
@_wp_reporting
def wc_order_get(domain: str, wp_path: str, order_id: str) -> str:
    """Zeigt eine WooCommerce-Bestellung: Status, Betrag, Rechnungs-/Lieferadresse,
    Positionen, eingetragene Sendungsverfolgung (Shipment Tracking) und die
    Sendungsnummer eines Label-Plugins (z.B. Post-Label), falls vorhanden.
    order_id: Bestellnummer (Ziffern). Rein lesend.
    """
    oid = _wc_check_order_id(order_id)
    client, ctx, wp_dir, uid, d = _wc_open(domain, wp_path)
    try:
        data = _wc_order_eval(client, ctx, wp_dir, _WC_ORDER_GET_PHP, {"id": oid})
    finally:
        client.close()
    return json.dumps({"domain": d, "wp_path": wp_dir, **data}, ensure_ascii=False, indent=2)


@mcp.tool()
@_wp_reporting
def wc_order_mark_shipped(
    domain: str,
    wp_path: str,
    order_id: str,
    tracking_number: str = "",
    provider: str = "swiss-post",
    set_completed: bool = True,
    confirm: bool = False,
) -> str:
    """Markiert eine WooCommerce-Bestellung als verschickt: trägt die
    Sendungsnummer über das Shipment-Tracking-Plugin ein
    (wc_st_add_tracking_number, wie beim manuellen Eintragen im Backend) und
    setzt den Status auf "Abgeschlossen" (set_completed=false: nur Tracking).

    tracking_number leer = Sendungsnummer aus dem Label-Plugin der Bestellung
    übernehmen (z.B. _spap_tracking vom Post-Label); fehlt sie dort, Abbruch.
    provider: Slug des Versanddienstleisters, Standard "swiss-post".
    Ohne confirm=true nur Vorschau (Bestellung, Sendungsnummer, geplante
    Änderungen). Mit confirm=true Ausführung als Subscription-User über die
    WooCommerce-API, danach Kontrolle. Achtung: Der Statuswechsel löst wie im
    Backend die WooCommerce-Mail "Bestellung abgeschlossen" an den Kunden aus.
    Ist die Sendungsnummer schon eingetragen, wird sie nicht doppelt angelegt.
    """
    oid = _wc_check_order_id(order_id)
    prov = _wc_check_provider(provider)
    nr_in = str(tracking_number or "").strip()
    nr = _wc_check_tracking(nr_in) if nr_in else ""
    client, ctx, wp_dir, uid, d = _wc_open(domain, wp_path)
    try:
        cur = _wc_order_eval(client, ctx, wp_dir, _WC_ORDER_GET_PHP, {"id": oid})
        order = cur["order"]
        quelle = "uebergeben"
        if not nr:
            label = order.get("label_tracking") or {}
            found = [v for v in label.values() if v]
            if not found:
                raise ValueError("Keine Sendungsnummer übergeben und keine im Label-Plugin der Bestellung gefunden - nichts geändert.")
            nr = _wc_check_tracking(found[0].split(",")[0])
            quelle = "label_plugin"
        exists = any(str(t.get("tracking_number")) == nr for t in order.get("tracking_items") or [] if isinstance(t, dict))
        geplant = []
        if not exists:
            geplant.append(f"Sendungsnummer {nr} ({prov}) eintragen")
        if set_completed and order.get("status") != "completed":
            geplant.append(f"Status {order.get('status')!r} -> 'completed' (Mail an Kunden durch WooCommerce)")
        result: dict[str, Any] = {"domain": d, "wp_path": wp_dir, "bestellung": oid,
                                  "sendungsnummer": nr, "quelle_sendungsnummer": quelle,
                                  "provider": prov, "geplant": geplant}
        if not exists and not cur.get("tracking_function"):
            raise ValueError("Kein Shipment-Tracking-Plugin aktiv (wc_st_add_tracking_number fehlt) - nichts geändert.")
        if not geplant:
            result["ergebnis"] = "Nichts zu tun - Sendungsnummer und Status sind bereits gesetzt."
            result["order"] = order
            return json.dumps(result, ensure_ascii=False, indent=2)
        if not confirm:
            result["ergebnis"] = "Vorschau - nichts geändert. Mit confirm=true ausführen."
            result["order"] = order
            return json.dumps(result, ensure_ascii=False, indent=2)
        res = _wc_order_eval(client, ctx, wp_dir, _WC_ORDER_SHIP_PHP,
                             {"id": oid, "tracking_number": nr, "provider": prov,
                              "set_completed": bool(set_completed)})
    finally:
        client.close()
    after = res["order"]
    ok_tr = any(str(t.get("tracking_number")) == nr for t in after.get("tracking_items") or [] if isinstance(t, dict))
    ok_st = (not set_completed) or after.get("status") == "completed"
    result["ausgefuehrt"] = res.get("done")
    result["ergebnis"] = "Ausgeführt und kontrolliert." if ok_tr and ok_st else "Ausgeführt, aber Kontrolle weicht ab - bitte Bestellung prüfen."
    result["order"] = after
    return json.dumps(result, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# WooCommerce-Lager: Bestand lesen, setzen, erhöhen, verringern
# ---------------------------------------------------------------------------

_WC_SKU_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")
_WC_STOCK_MAX = 1_000_000
_WC_STOCK_MAX_CHANGE = 10_000

# Fester PHP-Code; Werte nur per JSON auf stdin. Produkt per ID oder Artikelnummer.
# Bei Varianten mit Lager auf dem Hauptprodukt wird dort gebucht (wie WooCommerce selbst).
_WC_STOCK_VIEW_PHP = r'''
function plmcp_stock_view($p) {
    $managed_by = $p->get_stock_managed_by_id();
    $m = ($managed_by && $managed_by !== $p->get_id()) ? wc_get_product($managed_by) : $p;
    $out = array(
        "id" => $p->get_id(), "name" => $p->get_name(), "sku" => $p->get_sku(), "type" => $p->get_type(),
        "parent_id" => $p->get_parent_id(), "status" => $p->get_status(),
        "manage_stock" => $p->get_manage_stock(), "stock_managed_by" => $m ? $m->get_id() : null,
        "stock_quantity" => $m ? $m->get_stock_quantity() : null,
        "stock_status" => $p->get_stock_status(), "backorders" => $p->get_backorders(),
    );
    if ($p->is_type("variable")) {
        $vars = array();
        foreach ($p->get_children() as $cid) {
            $c = wc_get_product($cid); if (!$c) { continue; }
            $vars[] = array("id" => $c->get_id(), "name" => $c->get_name(), "sku" => $c->get_sku(),
                "manage_stock" => $c->get_manage_stock(), "stock_managed_by" => $c->get_stock_managed_by_id(),
                "stock_quantity" => $c->get_stock_quantity(), "stock_status" => $c->get_stock_status());
        }
        $out["variations"] = $vars;
    }
    return $out;
}
$in = json_decode(stream_get_contents(STDIN), true);
$id = 0;
if ($in["by"] === "id") { $id = (int) $in["key"]; }
else { $id = (int) wc_get_product_id_by_sku((string) $in["key"]); }
$p = $id > 0 ? wc_get_product($id) : null;
if (!$p) { fwrite(STDERR, "Produkt nicht gefunden"); exit(1); }
'''

_WC_STOCK_GET_PHP = _WC_STOCK_VIEW_PHP + r'''
echo json_encode(array("product" => plmcp_stock_view($p)));
'''

_WC_STOCK_UPDATE_PHP = _WC_STOCK_VIEW_PHP + r'''
if ($p->is_type("variable") && !$p->managing_stock()) { fwrite(STDERR, "Variables Produkt ohne eigenes Lager - bitte die Variante angeben, nichts geändert"); exit(1); }
if (!$p->managing_stock()) { fwrite(STDERR, "Lagerverwaltung für dieses Produkt nicht aktiv - nichts geändert"); exit(1); }
$m = wc_get_product($p->get_stock_managed_by_id());
$before = $m ? $m->get_stock_quantity() : null;
if ($in["expected"] !== null && (int) $before !== (int) $in["expected"]) { fwrite(STDERR, "Bestand ist " . var_export($before, true) . ", erwartet " . (int) $in["expected"] . " - nichts geändert"); exit(1); }
$new = wc_update_product_stock($p, abs((int) $in["value"]), $in["op"]);
if ($new === false || $new === null) { fwrite(STDERR, "WooCommerce hat den Bestand nicht angepasst"); exit(1); }
$p = wc_get_product($p->get_id());
echo json_encode(array("before" => $before, "after" => $new, "product" => plmcp_stock_view($p)));
'''


def _wc_check_product_ref(product: Any) -> tuple[str, str]:
    """Produkt-ID (bis 9 Ziffern) oder Artikelnummer (SKU, z.B. EAN-13)."""
    if isinstance(product, bool):
        raise ValueError("product muss eine Produkt-ID oder Artikelnummer sein.")
    s = str(product or "").strip()
    if s.isdigit() and int(s) == 0:
        raise ValueError("Produkt-ID 0 gibt es nicht.")
    if s.isdigit() and len(s) <= 9:
        return "id", s
    if _WC_SKU_RE.match(s):
        return "sku", s
    raise ValueError(f"Ungültige Produkt-ID oder Artikelnummer {product!r}.")


def _wc_check_stock_op(quantity: Any, change: Any) -> tuple[str, int]:
    """Genau eins von quantity (absolut) oder change (+/-). Rückgabe (op, wert)."""
    has_q = quantity is not None and quantity != ""
    has_c = change is not None and change != "" and change != 0
    if has_q == has_c:
        raise ValueError("Genau eines angeben: quantity (neuer Bestand) oder change (z.B. 5 oder -3).")
    if has_q:
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise ValueError("quantity muss eine ganze Zahl sein.")
        if not -_WC_STOCK_MAX < quantity < _WC_STOCK_MAX:
            raise ValueError(f"quantity ausserhalb von ±{_WC_STOCK_MAX}.")
        return "set", quantity
    if isinstance(change, bool) or not isinstance(change, int):
        raise ValueError("change muss eine ganze Zahl sein (positiv = erhöhen, negativ = verringern).")
    if abs(change) > _WC_STOCK_MAX_CHANGE:
        raise ValueError(f"change höchstens ±{_WC_STOCK_MAX_CHANGE} pro Aufruf.")
    return ("increase" if change > 0 else "decrease"), abs(change)


def _wc_check_note(note: str) -> str:
    s = str(note or "").strip()
    if len(s) > 200 or any(ord(ch) < 32 for ch in s):
        raise ValueError("note höchstens 200 Zeichen, ohne Steuerzeichen.")
    return s


@mcp.tool()
@_wp_reporting
def wc_stock_get(domain: str, wp_path: str, product: str) -> str:
    """Zeigt den Lagerbestand eines WooCommerce-Produkts oder einer Variante.

    product: Produkt-ID (Ziffern, bis 9 Stellen) oder Artikelnummer/SKU
    (z.B. EAN-13). Bei Varianten, deren Lager auf dem Hauptprodukt geführt
    wird, steht dessen ID unter stock_managed_by. Bei variablen Produkten
    werden alle Varianten mit Bestand aufgelistet. Rein lesend.
    """
    by, key = _wc_check_product_ref(product)
    client, ctx, wp_dir, uid, d = _wc_open(domain, wp_path)
    try:
        data = _wp_eval(client, ctx, wp_dir, _WC_STOCK_GET_PHP,
                        stdin_data=json.dumps({"by": by, "key": key}).encode("utf-8"))
    finally:
        client.close()
    if not isinstance(data, dict) or "product" not in data:
        raise SSHError("Unerwartete Antwort beim Lesen des Produkts.")
    return json.dumps({"domain": d, "wp_path": wp_dir, **data}, ensure_ascii=False, indent=2)


@mcp.tool()
@_wp_reporting
def wc_stock_update(
    domain: str,
    wp_path: str,
    product: str,
    quantity: int | None = None,
    change: int | None = None,
    expected: int | None = None,
    note: str = "",
    confirm: bool = False,
) -> str:
    """Ändert den Lagerbestand eines WooCommerce-Produkts oder einer Variante.

    product: Produkt-ID oder Artikelnummer/SKU (z.B. EAN-13).
    Genau eines von:
    - quantity: neuer Bestand (setzen, z.B. 25)
    - change: Differenz (positiv = erhöhen, negativ = verringern, max. ±10000)
    expected: optional der erwartete aktuelle Bestand; weicht er ab (z.B.
    weil inzwischen eine Bestellung abgebucht hat), wird nichts geändert.
    note: optionaler Grund fürs Audit-Log (z.B. "Inventur", "Lieferung").

    Gebucht wird über die WooCommerce-API (wc_update_product_stock, atomar),
    bei Varianten mit Lager auf dem Hauptprodukt dort. Produkte ohne aktive
    Lagerverwaltung werden nicht geändert. Ohne confirm=true nur Vorschau
    (aktueller und neuer Bestand). Mit confirm=true Ausführung als
    Subscription-User, Eintrag im Audit-Log und Kontrolle.
    """
    by, key = _wc_check_product_ref(product)
    op, value = _wc_check_stock_op(quantity, change)
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int)):
        raise ValueError("expected muss eine ganze Zahl sein.")
    why = _wc_check_note(note)
    client, ctx, wp_dir, uid, d = _wc_open(domain, wp_path)
    try:
        cur = _wp_eval(client, ctx, wp_dir, _WC_STOCK_GET_PHP,
                       stdin_data=json.dumps({"by": by, "key": key}).encode("utf-8"))
        if not isinstance(cur, dict) or "product" not in cur:
            raise SSHError("Unerwartete Antwort beim Lesen des Produkts.")
        prod = cur["product"]
        if prod.get("type") == "variable" and prod.get("stock_managed_by") in (None, 0):
            raise ValueError("Variables Produkt ohne eigenes Lager - bitte die Variante angeben. Nichts geändert.")
        if not prod.get("manage_stock"):
            raise ValueError("Lagerverwaltung für dieses Produkt ist nicht aktiv - nichts geändert.")
        before = prod.get("stock_quantity")
        base = int(before or 0)
        planned = value if op == "set" else base + value if op == "increase" else base - value
        result: dict[str, Any] = {
            "domain": d, "wp_path": wp_dir, "produkt": prod.get("name"), "produkt_id": prod.get("id"),
            "artikelnummer": prod.get("sku"), "lager_bei": prod.get("stock_managed_by"),
            "bestand_aktuell": before, "bestand_neu": planned,
            "aktion": {"set": f"setzen auf {value}", "increase": f"+{value}", "decrease": f"-{value}"}[op],
        }
        if expected is not None and before != expected:
            raise ValueError(f"Bestand ist {before}, erwartet {expected} - nichts geändert.")
        if op == "set" and before == value:
            result["ergebnis"] = "Nichts zu tun - Bestand ist bereits so."
            return json.dumps(result, ensure_ascii=False, indent=2)
        if not confirm:
            result["ergebnis"] = "Vorschau - nichts geändert. Mit confirm=true ausführen."
            return json.dumps(result, ensure_ascii=False, indent=2)
        try:
            res = _wp_eval(client, ctx, wp_dir, _WC_STOCK_UPDATE_PHP, stdin_data=json.dumps({
                "by": "id", "key": str(prod.get("id")), "op": op, "value": value,
                "expected": before if expected is None else expected,
            }).encode("utf-8"))
        except Exception as e:
            _audit("wc_stock_update", domain=d, product_id=prod.get("id"), sku=prod.get("sku"), op=op,
                   value=value, before=before, note=why, result="failed", error=str(e)[:200])
            raise
    finally:
        client.close()
    after = res.get("after") if isinstance(res, dict) else None
    ok = after is not None and int(after) == planned
    _audit("wc_stock_update", domain=d, product_id=prod.get("id"), sku=prod.get("sku"), op=op, value=value,
           before=before, after=after, note=why, result="ok" if ok else "mismatch")
    result["bestand_neu"] = after
    result["ergebnis"] = "Ausgeführt und kontrolliert." if ok else f"Ausgeführt, Bestand jetzt {after} statt {planned} - bitte prüfen."
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def delete_vhost_log(
    domain: str,
    filename: str = "",
    all_rotated: bool = False,
    confirm: bool = False,
) -> str:
    """Löscht Logdateien im Log-Verzeichnis der Domain
    (/var/www/vhosts/<domain>/logs/) - z.B. um Platz freizugeben.

    filename: Dateiname direkt im logs-Verzeichnis (ohne Pfad), z.B.
    "error_log.3.gz" oder "access_log".
    - Rotierte Logs (Endung .gz, .<Zahl> oder -JJJJMMTT) werden gelöscht.
    - Aktive Logs (z.B. "access_log", "error_log", "proxy_error_log") werden
      stattdessen auf 0 Bytes geleert: Der Webserver hält diese Dateien
      offen, ein echtes Löschen würde den Platz erst nach einem Reload
      freigeben und bis dahin weitergeschriebene Einträge gingen verloren.
    all_rotated: true löscht ALLE rotierten Logs im Verzeichnis auf einmal
    (filename dann leer lassen); aktive Logs bleiben dabei unangetastet.

    Symlinks und Verzeichnisse werden nie angefasst. Erfordert confirm=true
    pro Aufruf.
    """
    if not confirm:
        raise ValueError(
            "confirm=true erforderlich - dieses Tool löscht Dateien auf "
            "einem Produktivserver und braucht eine explizite Bestätigung pro Aufruf."
        )
    name = filename.strip()
    if all_rotated == bool(name):
        raise ValueError(
            "Entweder filename angeben oder all_rotated=true setzen (nicht beides)."
        )
    if name and not _LOG_FILENAME_RE.match(name):
        raise ValueError(
            f"Ungültiger Dateiname {filename!r} - erlaubt ist nur ein reiner "
            "Dateiname im logs-Verzeichnis (keine Pfade, kein '..')."
        )

    log_dir = _vhost_path(domain, "logs")

    client = _ssh_connect()
    try:
        sftp = client.open_sftp()
        try:
            _assert_real_within_vhost(sftp, log_dir, domain)
            if all_rotated:
                removed: list[str] = []
                freed = 0
                for entry in sftp.listdir_attr(log_dir):
                    if not stat.S_ISREG(entry.st_mode or 0):
                        continue
                    if not _ROTATED_LOG_RE.search(entry.filename):
                        continue
                    sftp.remove(f"{log_dir}/{entry.filename}")
                    removed.append(entry.filename)
                    freed += entry.st_size or 0
                if not removed:
                    return f"Keine rotierten Logs in {log_dir} gefunden."
                return (
                    f"{len(removed)} rotierte Logs in {log_dir} gelöscht "
                    f"({freed / 1024 / 1024:.1f} MB): " + ", ".join(sorted(removed))
                )

            full = f"{log_dir}/{name}"
            try:
                lst = sftp.lstat(full)
            except FileNotFoundError:
                raise ValueError(f"'{full}' existiert nicht.")
            if not stat.S_ISREG(lst.st_mode or 0):
                raise ValueError(
                    f"'{full}' ist keine reguläre Datei (Symlink/Verzeichnis) - "
                    "wird aus Sicherheitsgründen nicht angefasst."
                )
            size_mb = (lst.st_size or 0) / 1024 / 1024

            if _ROTATED_LOG_RE.search(name):
                sftp.remove(full)
                return f"Log gelöscht: {full} ({size_mb:.1f} MB)"
            sftp.truncate(full, 0)
            return (
                f"Aktives Log geleert (auf 0 Bytes gekürzt statt gelöscht): "
                f"{full} ({size_mb:.1f} MB freigegeben)"
            )
        finally:
            sftp.close()
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Hosting-Einstellungen (PHP-Handler, PHP-Einstellungen)
# ---------------------------------------------------------------------------
#
# Lesen: PHP-Handler aus der Plesk-DB, PHP-Einstellungen per
# `plesk bin site --show-php-settings`, Hosting-Block aus `plesk bin site --info`.
# Schreiben: nur der PHP-Handler per `plesk bin site --update <domain>
# -php_handler_id <id>` - nur auf einen auf dem Server aktivierten Handler,
# ohne confirm=true nur Vorschau, danach Kontrolle in der Plesk-DB.

_HOSTING_TIMEOUT = int(os.environ.get("PLESK_HOSTING_TIMEOUT", "180"))
_PHP_HANDLER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PHP_SETTING_LINE_RE = re.compile(r"^([A-Za-z0-9_.]+) = ?(.*)$")


def _php_handlers(client: paramiko.SSHClient) -> list[dict[str, Any]]:
    out, err, rc = _client_exec(client, "plesk bin php_handler --list -json true")
    try:
        handlers = json.loads(out)
    except ValueError:
        handlers = None
    if rc != 0 or not isinstance(handlers, list):
        raise SSHError(f"PHP-Handler-Liste nicht lesbar: {(err.strip() or out.strip())[:300]}")
    return [h for h in handlers if isinstance(h, dict) and isinstance(h.get("id"), str)]


def _php_handler_view(h: dict[str, Any] | None, handler_id: str) -> dict[str, Any]:
    if h is None:
        return {"id": handler_id, "hinweis": "Handler nicht in der Handler-Liste des Servers."}
    return {"id": h["id"], "version": h.get("fullVersion") or h.get("version"),
            "typ": h.get("type"), "status": h.get("status")}


def _hosting_handler_id(client: paramiko.SSHClient, domain: str) -> str:
    """Aktueller PHP-Handler der Domain (hosting.php_handler_id)."""
    sql = (
        "SELECT h.php_handler_id FROM domains d JOIN hosting h ON h.dom_id = d.id "
        f"WHERE d.name = {_sql_quote(domain)}"
    )
    out, err, rc = _client_exec(client, f"plesk db -Ne {shlex.quote(sql)}")
    rows = [r.strip() for r in out.strip().splitlines() if r.strip()]
    if rc != 0 or len(rows) != 1:
        raise ValueError(
            f"Domain '{domain}' nicht mit Webhosting in Plesk gefunden"
            + (f": {err.strip()}" if err.strip() else ".")
        )
    return rows[0]


def _parse_php_settings(text: str) -> dict[str, dict[str, str]]:
    """Ausgabe von `plesk bin site --show-php-settings` nach Abschnitten."""
    sections: dict[str, dict[str, str]] = {}
    current = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.endswith(":") and " = " not in line:
            current = sections.setdefault(line[:-1].strip(), {})
            continue
        m = _PHP_SETTING_LINE_RE.match(line)
        if m and current is not None:
            current[m.group(1)] = m.group(2)
    return sections


def _parse_site_hosting(text: str) -> dict[str, str]:
    """Hosting-Block aus `plesk bin site --info` (ohne FTP-Zugangsdaten)."""
    out: dict[str, str] = {}
    block = None
    for line in text.splitlines():
        if line.startswith("====="):
            continue
        if line and not line.startswith(" ") and ":" not in line:
            block = line.strip()
            continue
        if line.startswith("--WWW-Root--:"):
            out["WWW-Root"] = line.split(":", 1)[1].strip()
            continue
        if block != "Hosting" or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key.lower().startswith("ftp "):
            continue
        out[key] = value.strip()
    return out


@mcp.tool()
def hosting_settings(domain: str) -> str:
    """Zeigt die Hosting-Einstellungen einer Domain: aktueller PHP-Handler
    (ID, PHP-Version, Typ fpm/fastcgi/cgi), PHP-Einstellungen (memory_limit,
    max_execution_time, upload_max_filesize, PHP-FPM-Werte usw.), die
    Hosting-Angaben aus `plesk bin site --info` (SSL, HTTPS-Redirect,
    PHP-/CGI-Support, Docroot) und alle auf dem Server aktivierten
    PHP-Handler, die mit set_php_handler gesetzt werden können.
    Rein lesend - kein confirm nötig.
    """
    d = _hosting_domain(domain)
    client = _ssh_connect()
    try:
        handler_id = _hosting_handler_id(client, d)
        handlers = _php_handlers(client)
        out, err, rc = _client_exec(client, f"plesk bin site --show-php-settings {shlex.quote(d)}",
                                    timeout=_HOSTING_TIMEOUT)
        php_settings = _parse_php_settings(out) if rc == 0 else {"fehler": (err.strip() or out.strip())[:300]}
        out, err, rc = _client_exec(client, f"plesk bin site --info {shlex.quote(d)}", timeout=_HOSTING_TIMEOUT)
        hosting = _parse_site_hosting(out) if rc == 0 else {"fehler": (err.strip() or out.strip())[:300]}
    finally:
        client.close()
    current = next((h for h in handlers if h["id"] == handler_id), None)
    return _json({
        "domain": d,
        "php_handler": _php_handler_view(current, handler_id),
        "php_einstellungen": php_settings,
        "hosting": hosting,
        "verfuegbare_php_handler": [_php_handler_view(h, h["id"]) for h in handlers
                                    if h.get("status") == "enabled"],
    })


_PHP_HANDLER_MAX_DOMAINS = 50


def _hosting_domain(domain: str) -> str:
    """Domainname wie bei den anderen Tools; IDN in Unicode (z.B.
    "küche.example") wird in Punycode umgewandelt."""
    d = (domain or "").strip().lower().rstrip(".")
    if d and not d.isascii():
        try:
            d = d.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError(f"Ungültiger Domainname: {domain!r}")
    return _wp_domain(d)


@mcp.tool()
def set_php_handler(
    handler_id: str,
    domain: str = "",
    domains: list[str] | None = None,
    confirm: bool = False,
) -> str:
    """Stellt den PHP-Handler (PHP-Version und Ausführungsart) einer oder
    mehrerer Domains um, z.B. handler_id="plesk-php84-fpm-dedicated".
    Entspricht pro Domain `plesk bin site --update <domain> -php_handler_id
    <id>`. Entweder domain (eine Domain) oder domains (Liste, max. 50)
    angeben. IDN-Domains in Punycode (xn--...) oder Unicode. Gültig sind nur
    Handler, die auf dem Server aktiviert sind (Liste siehe hosting_settings
    bzw. die Fehlermeldung). Gilt nur für die angegebenen Domains (Haupt-,
    Sub- oder Zusatzdomain), nicht für weitere Domains der Subscription.

    Ohne confirm=true nur Vorschau (je Domain aktueller und neuer Handler,
    ob sich Version oder Typ ändern) - es wird nichts geändert.
    ACHTUNG - mit confirm=true wird die Webserver-/PHP-Konfiguration der
    Domains auf dem Produktivserver neu geschrieben (kurze Unterbrechung
    möglich, Plugins/Code müssen zur PHP-Version passen). Vorher werden alle
    Domains geprüft - ist eine unbekannt, wird nichts geändert. Danach wird
    der Handler je Domain in der Plesk-Datenbank kontrolliert; schlägt eine
    Domain fehl, werden die übrigen trotzdem umgestellt und der Fehler im
    Ergebnis gemeldet. Rücksetzen: set_php_handler mit dem je Domain
    genannten alten Handler.
    """
    hid = (handler_id or "").strip()
    if not _PHP_HANDLER_ID_RE.match(hid):
        raise ValueError(f"Ungültige handler_id {handler_id!r}.")
    raw = ([domain] if (domain or "").strip() else []) + list(domains or [])
    if not raw:
        raise ValueError("domain oder domains angeben.")
    if len(raw) > _PHP_HANDLER_MAX_DOMAINS:
        raise ValueError(f"Maximal {_PHP_HANDLER_MAX_DOMAINS} Domains pro Aufruf.")
    names = []
    for r in raw:
        d = _hosting_domain(r)
        if d not in names:
            names.append(d)

    client = _ssh_connect()
    try:
        handlers = _php_handlers(client)
        by_id = {h["id"]: h for h in handlers}
        target = by_id.get(hid)
        if target is None or target.get("status") != "enabled":
            enabled = sorted(h["id"] for h in handlers if h.get("status") == "enabled")
            raise ValueError(
                f"PHP-Handler {hid!r} ist auf dem Server "
                + ("nicht aktiviert" if target is not None else "nicht vorhanden")
                + ". Aktivierte Handler: " + ", ".join(enabled)
            )
        # Zuerst alle Domains prüfen - unbekannte Domain: nichts ändern.
        current = {d: _hosting_handler_id(client, d) for d in names}
        results = []
        for d in names:
            old_id = current[d]
            old = by_id.get(old_id)
            item: dict[str, Any] = {"domain": d, "alt": _php_handler_view(old, old_id)}
            if old is not None:
                item["versionswechsel"] = old.get("version") != target.get("version")
                item["typwechsel"] = old.get("type") != target.get("type")
            if old_id == hid:
                item["ergebnis"] = "unverändert (nutzt diesen Handler bereits)"
            elif not confirm:
                item["ergebnis"] = "Vorschau - würde umgestellt"
            else:
                out, err, rc = _client_exec(
                    client,
                    f"plesk bin site --update {shlex.quote(d)} -php_handler_id {shlex.quote(hid)}",
                    timeout=_HOSTING_TIMEOUT,
                )
                msg = (err.strip() or out.strip())[-500:]
                try:
                    now_id = _hosting_handler_id(client, d)
                except ValueError as e:
                    now_id = f"nicht lesbar: {e}"
                ok = rc == 0 and now_id == hid
                _audit("set_php_handler", domain=d, old=old_id, new=hid,
                       result="ok" if ok else "failed", exit_code=rc, now=now_id)
                if ok:
                    item["ergebnis"] = "umgestellt"
                else:
                    item["ergebnis"] = "FEHLER - nicht umgestellt"
                    item["fehler"] = f"Exit-Code {rc}, aktuell in Plesk: {now_id!r}: {msg}"
            results.append(item)
    finally:
        client.close()

    out_: dict[str, Any] = {"neu": _php_handler_view(target, hid), "domains": results}
    if not confirm and any(r["ergebnis"].startswith("Vorschau") for r in results):
        out_["hinweis"] = "Vorschau - nichts geändert. Zum Umstellen confirm=true setzen."
    failed = [r["domain"] for r in results if r["ergebnis"].startswith("FEHLER")]
    if failed:
        out_["fehlgeschlagen"] = failed
    return _json(out_)


# --- PHP-Einstellungen (php_settings_show / _update / _rollback) --------------
#
# Stand lesen: wörtlich aus der Plesk-Datenbank (`plesk db`): eigene Werte der
# Domain (PhpSettingsParameters über dom_param.phpSettingsId) und der Block
# "Additional directives" (PhpSettingsCustom) inkl. Abschnittsüberschriften wie
# [php-fpm-pool-settings] - `--show-php-settings` lässt diese weg und mischt
# eigene mit geerbten Werten, taugt also nur zur Anzeige und Kontrolle.
# Schreiben: `plesk bin site --update-php-settings <domain> -settings <datei>`
# bzw. `-additional-settings <datei>`; die Datei liegt nur kurz in einem
# root-only Verzeichnis (0600) und wird immer gelöscht. Danach Kontrolle
# (Datenbank, Show-Ausgabe, PHP-FPM-Dienst, Startseite) und bei einem Fehler
# automatisch zurück auf das vorher angelegte Backup.

_PHPS_SIZE = r"-1|\d{1,6}[KMGkmg]?"
_PHPS_UINT = r"\d{1,9}"
_PHPS_BOOL = r"(?i:on|off|true|false|1|0)"
_PHPS_OPEN_BASEDIR_DEFAULT = "{WEBSPACEROOT}{/}{:}{TMP}{/}"
_PHPS_RULES_DEFAULT: dict[str, str] = {
    "open_basedir": "open_basedir",  # Sonderregel, siehe _phps_check_value
    "memory_limit": _PHPS_SIZE,
    "max_execution_time": _PHPS_UINT,
    "max_input_time": r"-1|\d{1,9}",
    "max_input_vars": _PHPS_UINT,
    "post_max_size": _PHPS_SIZE,
    "upload_max_filesize": _PHPS_SIZE,
    "realpath_cache_size": _PHPS_SIZE,
    "realpath_cache_ttl": _PHPS_UINT,
    "opcache.memory_consumption": _PHPS_UINT,
    "opcache.interned_strings_buffer": _PHPS_UINT,
    "opcache.max_accelerated_files": _PHPS_UINT,
    "opcache.revalidate_freq": _PHPS_UINT,
    "opcache.validate_timestamps": _PHPS_BOOL,
    "opcache.enable_file_override": _PHPS_BOOL,
    "pm": r"static|dynamic|ondemand",
    "pm.max_children": _PHPS_UINT,
    "pm.max_requests": _PHPS_UINT,
    "pm.start_servers": _PHPS_UINT,
    "pm.min_spare_servers": _PHPS_UINT,
    "pm.max_spare_servers": _PHPS_UINT,
}
# Per ENV PHP_SETTINGS_ALLOWLIST ergänzte Direktiven: generische Wertprüfung.
_PHPS_GENERIC_VALUE = r"[A-Za-z0-9_.,:/{}+~&|^() -]{0,200}"
# Nie erlaubt - auch nicht per ENV.
_PHPS_NEVER = {
    "disable_functions", "auto_prepend_file", "auto_append_file", "allow_url_include",
    "extension", "zend_extension", "sendmail_path", "enable_dl", "include_path", "doc_root",
    "user_dir", "session.save_path", "error_log",
}
_PHPS_KEY_RE = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)*$")
_PHPS_BAD_CHARS = set("\n\r\0;[]\"'")
_PHPS_BACKUP_DIR = os.environ.get("PHP_SETTINGS_BACKUP_DIR", "/root/.plesk-mcp/php-settings-backups").strip()
_PHPS_TMP_DIR = "/root/.plesk-mcp/tmp"
_PHPS_HTTP_TIMEOUT = int(os.environ.get("PHP_SETTINGS_HTTP_TIMEOUT", "20"))
_PHPS_FPM_RETRY_DELAY = 3
_PHPS_SECTIONS = ("General settings", "Performance settings", "PHP-FPM settings")
_phps_locks: dict[str, threading.Lock] = {}
_phps_locks_guard = threading.Lock()


def _phps_rules() -> dict[str, str]:
    """Allowlist: Standardumfang plus PHP_SETTINGS_ALLOWLIST (kommagetrennt),
    nie die Einträge aus _PHPS_NEVER."""
    rules = dict(_PHPS_RULES_DEFAULT)
    for n in os.environ.get("PHP_SETTINGS_ALLOWLIST", "").split(","):
        n = n.strip().lower()
        if n and _PHPS_KEY_RE.match(n) and n not in _PHPS_NEVER and n not in rules:
            rules[n] = _PHPS_GENERIC_VALUE
    return rules


def _phps_check_value(key: Any, value: Any) -> tuple[str, str]:
    """Prüft Schlüssel und Wert; Ablehnung immer mit Grund im Klartext."""
    if not isinstance(key, str) or not key.strip():
        raise ValueError(f"Ungültiger Schlüssel {key!r}.")
    k = key.strip()
    if _PHPS_BAD_CHARS & set(k) or not _PHPS_KEY_RE.match(k):
        raise ValueError(f"Schlüssel {key!r} abgelehnt: nur Kleinbuchstaben, Ziffern, '_' und '.' erlaubt "
                         "(keine Zeilenumbrüche, ';', eckigen Klammern oder Anführungszeichen).")
    if k in _PHPS_NEVER:
        raise ValueError(f"'{k}' ist aus Sicherheitsgründen nie änderbar (auch nicht per ENV).")
    rules = _phps_rules()
    if k not in rules:
        raise ValueError(f"'{k}' ist nicht freigegeben. Erlaubt: {', '.join(sorted(rules))} "
                         "(erweiterbar per ENV PHP_SETTINGS_ALLOWLIST).")
    if isinstance(value, bool):
        value = "on" if value else "off"
    if not isinstance(value, (str, int)) or isinstance(value, float):
        raise ValueError(f"Wert für '{k}' muss ein String oder eine Zahl sein.")
    v = str(value).strip()
    if _PHPS_BAD_CHARS & set(v):
        raise ValueError(f"Wert für '{k}' abgelehnt: keine Zeilenumbrüche, ';', eckigen Klammern "
                         "oder Anführungszeichen erlaubt.")
    if k == "open_basedir":
        if v not in ("none", _PHPS_OPEN_BASEDIR_DEFAULT):
            raise ValueError(f"open_basedir: nur 'none' oder der Plesk-Standard "
                             f"'{_PHPS_OPEN_BASEDIR_DEFAULT}' erlaubt.")
    elif not re.fullmatch(rules[k], v):
        raise ValueError(f"Ungültiger Wert {value!r} für '{k}'.")
    return k, v


def _phps_lock(key: str, domain: str, was: str = "eine Änderung der PHP-Einstellungen") -> threading.Lock:
    """Sperre pro Domain und Aktionsart (key, z.B. "img:example.com"); die
    Meldung nennt nur Domain und Aktion, nicht den internen Schlüssel."""
    with _phps_locks_guard:
        lock = _phps_locks.setdefault(key, threading.Lock())
    if not lock.acquire(blocking=False):
        raise _UserError(f"Für {domain} läuft bereits {was} - bitte warten und erneut versuchen.")
    return lock


def _phps_db(client: paramiko.SSHClient, sql: str) -> list[list[str]]:
    out, err, rc = _client_exec(client, f"plesk db -Ne {shlex.quote(sql)}")
    if rc != 0:
        raise SSHError(f"Plesk-Datenbank nicht lesbar: {(err.strip() or out.strip())[:300]}")
    return [r.split("\t") for r in out.splitlines() if r.strip()]


def _phps_unhex(h: str) -> str:
    try:
        return bytes.fromhex(h.strip()).decode("utf-8")
    except ValueError:
        raise SSHError("Unerwartete Antwort der Plesk-Datenbank.")


def _phps_parse_show(text: str) -> dict[str, dict[str, str]]:
    return _parse_php_settings(text)


def _phps_state(client: paramiko.SSHClient, domain: str) -> dict[str, Any]:
    """Aktueller Stand: System-User, eigene Werte und Additional-Block (wörtlich,
    aus der DB) sowie die Show-Ausgabe (inkl. geerbter Werte)."""
    q = _sql_quote(domain)
    rows = _phps_db(client, (
        "SELECT s.login, IFNULL(p.val, '') FROM domains d JOIN hosting h ON h.dom_id = d.id "
        "JOIN sys_users s ON s.id = h.sys_user_id "
        "LEFT JOIN dom_param p ON p.dom_id = d.id AND p.param = 'phpSettingsId' "
        f"WHERE d.name = {q}"
    ))
    if len(rows) != 1 or len(rows[0]) != 2:
        raise ValueError(f"Domain '{domain}' nicht mit Webhosting in Plesk gefunden.")
    login, sid = (c.strip() for c in rows[0])
    if not _WP_SYSUSER_RE.match(login):
        raise ValueError(f"Unerwarteter System-User für '{domain}'.")
    own: dict[str, str] = {}
    additional = ""
    if sid:
        if not sid.isdigit():
            raise SSHError("Unerwartete phpSettingsId in der Plesk-Datenbank.")
        for r in _phps_db(client, f"SELECT HEX(name), HEX(value) FROM PhpSettingsParameters WHERE id = {int(sid)}"):
            if len(r) == 2:
                own[_phps_unhex(r[0])] = _phps_unhex(r[1])
        rows = _phps_db(client, f"SELECT HEX(value) FROM PhpSettingsCustom WHERE id = {int(sid)}")
        if rows and rows[0] and rows[0][0].strip():
            additional = _phps_unhex(rows[0][0])
    out, err, rc = _client_exec(client, f"plesk bin site --show-php-settings {shlex.quote(domain)}",
                                timeout=_HOSTING_TIMEOUT)
    if rc != 0:
        raise SSHError(f"PHP-Einstellungen von '{domain}' nicht lesbar: {(err.strip() or out.strip())[:300]}")
    show = _phps_parse_show(out)
    standard = {k: v for sec in _PHPS_SECTIONS for k, v in show.get(sec, {}).items()}
    if not standard:
        raise SSHError(f"PHP-Einstellungen von '{domain}' nicht lesbar (leere Ausgabe).")
    return {"login": login, "settings_id": sid or None, "own": own, "additional": additional,
            "show": show, "standard": standard}


def _phps_norm_block(text: str) -> str:
    lines = [l.rstrip() for l in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return "\n".join(lines)


def _phps_block_key(line: str) -> str | None:
    s = line.strip()
    if not s or s.startswith((";", "#", "[")) or "=" not in s:
        return None
    return s.split("=", 1)[0].strip().lower()


def _phps_block_values(text: str) -> dict[str, str]:
    """Schlüssel/Werte im php.ini-Teil des Blocks (vor dem ersten Abschnitt)."""
    out: dict[str, str] = {}
    for line in _phps_norm_block(text).split("\n"):
        if line.strip().startswith("["):
            break
        k = _phps_block_key(line)
        if k:
            out[k] = line.split("=", 1)[1].strip()
    return out


def _phps_merge_additional(text: str, changes: dict[str, str]) -> str:
    """Ändert/ergänzt Schlüssel nur im php.ini-Teil vor dem ersten Abschnitt;
    Abschnitte (z.B. [php-fpm-pool-settings]) und alle anderen Zeilen bleiben
    wörtlich erhalten."""
    lines = _phps_norm_block(text).split("\n") if _phps_norm_block(text) else []
    first_sec = next((i for i, l in enumerate(lines) if l.strip().startswith("[")), len(lines))
    head, tail = lines[:first_sec], lines[first_sec:]
    done: set[str] = set()
    new_head = []
    for line in head:
        k = _phps_block_key(line)
        if k in changes:
            if k not in done:
                new_head.append(f"{k} = {changes[k]}")
                done.add(k)
            continue
        new_head.append(line)
    while new_head and not new_head[-1].strip():
        new_head.pop()
    new_head += [f"{k} = {v}" for k, v in changes.items() if k not in done]
    return "\n".join(new_head + tail)


def _phps_write(client: paramiko.SSHClient, domain: str, option: str, content: str) -> tuple[int, str]:
    """Übergabedatei als root (0600) in einem root-only Verzeichnis anlegen,
    `plesk bin site --update-php-settings` aufrufen, Datei immer löschen."""
    d = shlex.quote(_PHPS_TMP_DIR)
    script = (
        f"umask 077; mkdir -p {d} && chmod 700 {d} || exit 96; "
        f"f=$(mktemp {d}/php.XXXXXXXX) || exit 97; trap 'rm -f \"$f\"' EXIT; "
        f'cat > "$f" && plesk bin site --update-php-settings {shlex.quote(domain)} {option} "$f"'
    )
    data = (content + "\n").encode("utf-8") if content else b""
    out, err, rc = _client_exec(client, script, timeout=_HOSTING_TIMEOUT, stdin_data=data)
    return rc, (err.strip() or out.strip())[-500:]


def _phps_fpm(client: paramiko.SSHClient, domain: str) -> tuple[bool | None, str]:
    """PHP-FPM-Dienst der Domain aktiv? None = kein FPM-Dienst (z.B. FastCGI)."""
    out, _, _ = _client_exec(
        client,
        "systemctl list-units --type=service --all --no-legend --plain | "
        f"grep -F -- {shlex.quote('_' + domain + '_')}",
    )
    units = [l.split()[0] for l in out.splitlines() if l.strip()]
    if not units:
        return None, "kein PHP-FPM-Dienst"
    unit = units[0]
    out, _, _ = _client_exec(client, f"systemctl is-active {shlex.quote(unit)}")
    state = out.strip() or "unbekannt"
    return state == "active", f"{unit}: {state}"


def _phps_http(domain: str) -> tuple[int | None, str]:
    """HTTP-Status der Startseite (ohne Redirects zu folgen)."""
    try:
        with httpx.Client(timeout=_PHPS_HTTP_TIMEOUT, follow_redirects=False) as c:
            r = c.get(f"https://{domain}/")
        return r.status_code, f"https://{domain}/ -> {r.status_code}"
    except Exception as e:  # noqa: BLE001 - nicht prüfbar, kein Fehler der Änderung
        return None, f"https://{domain}/ nicht abrufbar ({type(e).__name__})"


def _phps_new_backup_id(domain: str) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{ts}-{os.urandom(4).hex()}@{domain}"


def _phps_write_backup(client: paramiko.SSHClient, domain: str, state: dict[str, Any],
                       source: str) -> str:
    """Kompletter Stand als JSON (Schema wie die wp-option-backups, aber root-only
    unter PHP_SETTINGS_BACKUP_DIR: der Inhalt wird beim Rollback als root an
    Plesk übergeben und darf deshalb nicht im Vhost des Kunden liegen)."""
    backup_id = _phps_new_backup_id(domain)
    record = {
        "id": backup_id,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "domain": domain,
        "settings_id": state["settings_id"],
        "own_settings": state["own"],
        "additional_directives": state["additional"],
        "show_settings": state["standard"],
    }
    data = json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")
    bdir = shlex.quote(_PHPS_BACKUP_DIR)
    path = shlex.quote(f"{_PHPS_BACKUP_DIR}/{backup_id}.json")
    _, err, rc = _client_exec(
        client, f"umask 077 && mkdir -p {bdir} && chmod 700 {bdir} && set -C && cat > {path}",
        stdin_data=data,
    )
    if rc != 0:
        raise SSHError(f"Backup konnte nicht angelegt werden - nichts geändert: {err.strip()}")
    return backup_id


def _phps_read_backup(client: paramiko.SSHClient, backup_id: str) -> dict[str, Any]:
    m = _WP_BACKUP_ID_RE.match((backup_id or "").strip())
    if not m:
        raise ValueError(f"Ungültige backup_id: {backup_id!r}")
    bid = m.group(0)
    out, err, rc = _client_exec(client, f"cat -- {shlex.quote(f'{_PHPS_BACKUP_DIR}/{bid}.json')}")
    if rc != 0:
        raise ValueError(f"Backup '{bid}' nicht gefunden: {err.strip()}")
    try:
        rec = json.loads(out)
        own, add = rec["own_settings"], rec["additional_directives"]
        if (rec.get("domain") != m.group(3) or not isinstance(own, dict) or not isinstance(add, str)
                or not isinstance(rec.get("show_settings"), dict)):
            raise ValueError("Inhalt passt nicht zur backup_id")
        for k, v in list(own.items()) + list(rec["show_settings"].items()):
            if not isinstance(k, str) or not isinstance(v, str) or set("\n\r\0") & set(k + v):
                raise ValueError(f"ungültiger Eintrag {k!r}")
        if "\0" in add:
            raise ValueError("ungültiger Additional-Block")
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError(f"Backup '{bid}' ist ungültig: {e}")
    return rec


def _phps_apply(client: paramiko.SSHClient, domain: str, state: dict[str, Any],
                target_own: dict[str, str], target_add: str
                ) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """Bringt die Domain auf target_own/target_add. Zuerst nur die geänderten
    Werte; fehlen danach andere eigene Werte (Plesk ersetzt statt
    zusammenzuführen), wird der komplette Sollstand geschrieben. info hält
    fest, wie Plesk sich verhalten hat."""
    errors: list[str] = []
    info: dict[str, Any] = {}
    diff = {k: v for k, v in target_own.items() if state["own"].get(k) != v}
    if diff:
        rc, msg = _phps_write(client, domain, "-settings", "\n".join(f"{k} = {v}" for k, v in diff.items()))
        info["settings_schreibvorgaenge"] = 1
        if rc != 0:
            errors.append(f"-settings: Exit-Code {rc}: {msg}")
        else:
            now = _phps_state(client, domain)
            deviating = sorted(k for k, v in target_own.items() if now["own"].get(k) != v)
            info["abweichend_nach_teilschreiben"] = [
                {"wert": k, "soll": target_own[k], "ist": now["own"].get(k)} for k in deviating]
            if deviating:
                info["settings_schreibvorgaenge"] = 2
                rc, msg = _phps_write(client, domain, "-settings",
                                      "\n".join(f"{k} = {v}" for k, v in target_own.items()))
                if rc != 0:
                    errors.append(f"-settings (vollständig): Exit-Code {rc}: {msg}")
    if _phps_norm_block(state["additional"]) != _phps_norm_block(target_add):
        rc, msg = _phps_write(client, domain, "-additional-settings", _phps_norm_block(target_add))
        if rc != 0:
            errors.append(f"-additional-settings: Exit-Code {rc}: {msg}")
    return _phps_state(client, domain), errors, info


def _phps_verify(client: paramiko.SSHClient, domain: str, now: dict[str, Any],
                 target_own: dict[str, str], target_add: str, show_expect: dict[str, str],
                 baseline: dict[str, Any]) -> list[str]:
    """Prüfungen nach einer Änderung; Rückgabe: fehlgeschlagene Prüfungen."""
    failed = []
    bad = [k for k, v in target_own.items() if now["own"].get(k) != v]
    if bad:
        failed.append("Plesk-Datenbank: abweichend " + ", ".join(
            f"{k} (soll {target_own[k]!r}, ist {now['own'].get(k)!r})" for k in bad))
    if _phps_norm_block(now["additional"]) != _phps_norm_block(target_add):
        failed.append("Plesk-Datenbank: Block 'Additional directives' weicht vom Soll ab")
    add_now = _phps_block_values(now["additional"])
    show_add = now["show"].get("Additional directives", {})
    for k, v in show_expect.items():
        is_now = now["standard"].get(k, show_add.get(k, add_now.get(k)))
        if is_now != v:
            failed.append(f"Show-Ausgabe: {k} = {is_now!r} statt {v!r}")
    if baseline["fpm"] is True:
        ok, detail = _phps_fpm(client, domain)
        if ok is not True:
            time.sleep(_PHPS_FPM_RETRY_DELAY)
            ok, detail = _phps_fpm(client, domain)
        if ok is not True:
            failed.append(f"PHP-FPM-Dienst nicht aktiv ({detail})")
    if baseline["http"] is not None and baseline["http"] < 500:
        status, detail = _phps_http(domain)
        if status is not None and status >= 500:
            failed.append(f"Startseite liefert {status} ({detail})")
    return failed


def _phps_change(domain: str, target_fn, confirm: bool, source: str) -> str:
    """Gemeinsamer Ablauf für Update und Rollback: Stand lesen, Soll berechnen,
    Vorschau bzw. Backup + Schreiben + Prüfungen + automatisches Zurückstellen."""
    d = _hosting_domain(domain)
    lock = _phps_lock(d, d)
    try:
        client = _ssh_connect()
        try:
            state = _phps_state(client, d)
            target_own, target_add, changes, show_expect, notes = target_fn(client, state)
            result: dict[str, Any] = {"domain": d, "aenderungen": changes}
            if notes:
                result["hinweise"] = notes
            if (all(state["own"].get(k) == v for k, v in target_own.items())
                    and _phps_norm_block(state["additional"]) == _phps_norm_block(target_add)):
                result["ergebnis"] = "Keine Änderung nötig - alle Werte sind bereits gesetzt."
                return _json(result)
            if not confirm:
                result["ergebnis"] = "Vorschau - nichts geschrieben. Mit confirm=true ausführen."
                return _json(result)

            baseline = {"fpm": _phps_fpm(client, d)[0], "http": _phps_http(d)[0]}
            backup_id = _phps_write_backup(client, d, state, source)
            result["backup_id"] = backup_id
            now, errors, info = _phps_apply(client, d, state, target_own, target_add)
            if info:
                info["plesk_verhalten_settings"] = (
                    "zusammenführend (nur die geänderten Werte geschrieben, alle anderen eigenen Werte blieben)"
                    if not info.get("abweichend_nach_teilschreiben") else
                    "abweichend nach dem Teilschreiben - vollständiger Sollstand nachgeschrieben")
                result["schreibvorgang"] = info
            failed = errors + _phps_verify(client, d, now, target_own, target_add, show_expect, baseline)
            if not failed:
                result["ergebnis"] = "Geschrieben und kontrolliert."
                result["pruefungen"] = {"datenbank": "ok", "show_ausgabe": "ok",
                                        "php_fpm": "ok" if baseline["fpm"] else "nicht anwendbar",
                                        "startseite": "ok" if baseline["http"] is not None and baseline["http"] < 500
                                        else "vorher schon nicht prüfbar/5xx - übersprungen"}
                _audit("php_settings", domain=d, source=source, changes=changes, backup_id=backup_id,
                       write_info=info, result="ok")
                return _json(result)

            # Automatisch auf das Backup zurückstellen
            # Vorher geerbte Werte, die jetzt eigene sind, auf den damaligen Wert
            back_own = {k: state["standard"].get(k, v) for k, v in target_own.items()}
            back_own.update(state["own"])
            after, rerrors, _ = _phps_apply(client, d, now, back_own, state["additional"])
            rfailed = rerrors + _phps_verify(client, d, after, back_own, state["additional"], {}, baseline)
            result["ergebnis"] = ("FEHLER - automatisch auf das Backup zurückgestellt." if not rfailed
                                  else "FEHLER - Zurückstellen ebenfalls fehlgeschlagen, bitte sofort prüfen!")
            result["fehlgeschlagene_pruefungen"] = failed
            if rfailed:
                result["fehler_beim_zurueckstellen"] = rfailed
            _audit("php_settings", domain=d, source=source, changes=changes, backup_id=backup_id,
                   result="rolled_back" if not rfailed else "rollback_failed", failed=failed)
            return _json(result)
        finally:
            client.close()
    finally:
        lock.release()


@mcp.tool()
def php_settings_show(domain: str) -> str:
    """Zeigt die PHP-Einstellungen einer Domain (Plesk "PHP-Einstellungen"):
    die Abschnitte von `plesk bin site --show-php-settings` ("General
    settings", "Performance settings", "PHP-FPM settings", "Additional
    directives") sowie zusätzlich aus der Plesk-Datenbank die eigenen Werte
    der Domain (alles andere ist vom Service-Plan/Server geerbt) und den
    Block "Additional directives" wörtlich - inkl. Abschnittsüberschriften wie
    [php-fpm-pool-settings], die die Show-Ausgabe nicht anzeigt.
    Rein lesend - kein confirm nötig.
    """
    d = _hosting_domain(domain)
    client = _ssh_connect()
    try:
        state = _phps_state(client, d)
    finally:
        client.close()
    return _json({
        "domain": d,
        "abschnitte": state["show"],
        "eigene_werte": state["own"],
        "additional_directives_roh": state["additional"],
        "aenderbar": sorted(_phps_rules()),
    })


@mcp.tool()
def php_settings_update(
    domain: str,
    settings: dict[str, Any] | None = None,
    additional_directives: dict[str, Any] | None = None,
    confirm: bool = False,
) -> str:
    """Ändert PHP-Einstellungen einer Domain (wie Plesk "PHP-Einstellungen").
    settings: Plesk-Standardfelder, z.B. {"memory_limit": "512M",
    "max_execution_time": "120", "pm.max_children": "20"}.
    additional_directives: Zeilen im Block "Additional directives", z.B.
    {"max_input_vars": "5000", "opcache.revalidate_freq": "60"}.

    Freigegeben (Allowlist mit Wertprüfung, erweiterbar per ENV
    PHP_SETTINGS_ALLOWLIST): open_basedir, memory_limit, max_execution_time,
    max_input_time, max_input_vars, post_max_size, upload_max_filesize,
    realpath_cache_size, realpath_cache_ttl, opcache.memory_consumption,
    opcache.interned_strings_buffer, opcache.max_accelerated_files,
    opcache.revalidate_freq, opcache.validate_timestamps,
    opcache.enable_file_override, pm, pm.max_children, pm.max_requests,
    pm.start_servers, pm.min_spare_servers, pm.max_spare_servers. Nie
    änderbar: disable_functions, auto_prepend_file, auto_append_file,
    allow_url_include, extension, zend_extension, sendmail_path, enable_dl,
    include_path, doc_root, user_dir, session.save_path, error_log.
    open_basedir nur "none" (nur wenn der System-User in CageFS läuft) oder
    der Plesk-Standard "{WEBSPACEROOT}{/}{:}{TMP}{/}". Keine Zeilenumbrüche,
    ';', eckigen Klammern oder Anführungszeichen. Plesk-Standardfelder (inkl.
    pm.*) nur über settings, alles andere nur über additional_directives.
    Bestehende Zeilen und Abschnitte im Additional-Block bleiben erhalten.

    Ohne confirm=true nur Vorschau (alt/neu je Schlüssel, ob der alte Wert
    eigen oder geerbt ist) - nichts wird geschrieben. Mit confirm=true:
    Backup des kompletten Stands (backup_id für php_settings_rollback), dann
    `plesk bin site --update-php-settings`. Danach Prüfungen: Plesk-DB und
    Show-Ausgabe entsprechen dem Soll, PHP-FPM-Dienst aktiv, Startseite ohne
    5xx. Schlägt eine Prüfung fehl, wird automatisch auf das Backup
    zurückgestellt und das deutlich gemeldet. Pro Domain nur eine Änderung
    gleichzeitig.
    """
    s_in = settings or {}
    a_in = additional_directives or {}
    if not isinstance(s_in, dict) or not isinstance(a_in, dict) or not (s_in or a_in):
        raise ValueError("settings und/oder additional_directives als Objekt angeben, "
                         "z.B. settings={\"memory_limit\": \"512M\"}.")
    if len(s_in) + len(a_in) > 40:
        raise ValueError("Maximal 40 Werte pro Aufruf.")
    s_chk = dict(_phps_check_value(k, v) for k, v in s_in.items())
    a_chk = dict(_phps_check_value(k, v) for k, v in a_in.items())
    both = set(s_chk) & set(a_chk)
    if both:
        raise ValueError(f"{', '.join(sorted(both))} sowohl in settings als auch in additional_directives.")

    def target(client: paramiko.SSHClient, state: dict[str, Any]):
        for k in s_chk:
            if k not in state["standard"]:
                raise ValueError(
                    f"'{k}' ist für diese Domain kein Plesk-Standardfeld"
                    + (" (pm.* gibt es nur bei PHP-FPM-Handlern)" if k.startswith("pm") else
                       " - über additional_directives setzen") + ".")
        for k in a_chk:
            if k in state["standard"] or k.startswith("pm"):
                raise ValueError(f"'{k}' ist ein Plesk-Standardfeld - über settings setzen, "
                                 "nicht über additional_directives.")
        if s_chk.get("open_basedir") == "none" and state["own"].get("open_basedir",
                                                                    state["standard"].get("open_basedir")) != "none":
            out, _, rc = _client_exec(client, f"/usr/sbin/cagefsctl --user-status {shlex.quote(state['login'])}")
            if rc != 0 or out.strip().lower() != "enabled":
                raise ValueError(f"open_basedir = none abgelehnt: System-User '{state['login']}' läuft nicht "
                                 f"in CageFS (cagefsctl: {out.strip() or 'keine Antwort'}).")
        changes = []
        for k, v in s_chk.items():
            old = state["own"].get(k)
            if old != v:
                changes.append({"bereich": "settings", "wert": k,
                                "alt": old if old is not None else state["standard"].get(k), "neu": v,
                                "alt_war": "eigen" if old is not None else "geerbt"})
        add_old = _phps_block_values(state["additional"])
        for k, v in a_chk.items():
            if add_old.get(k) != v:
                changes.append({"bereich": "additional_directives", "wert": k, "alt": add_old.get(k), "neu": v})
        target_own = dict(state["own"])
        target_own.update(s_chk)
        target_add = _phps_merge_additional(state["additional"], a_chk) if a_chk else state["additional"]
        return target_own, target_add, changes, {**s_chk, **a_chk}, []

    return _phps_change(domain, target, confirm, "php_settings_update")


@mcp.tool()
def php_settings_rollback(domain: str, backup_id: str, confirm: bool = False) -> str:
    """Stellt die PHP-Einstellungen einer Domain auf den Stand eines Backups
    von php_settings_update zurück (eigene Werte und Block "Additional
    directives" wörtlich). Werte, die vorher geerbt waren und inzwischen
    eigene Werte sind, werden auf den damaligen Wert gesetzt (Plesk kann sie
    per CLI nicht wieder auf "geerbt" stellen - wird gemeldet).
    Ohne confirm=true nur Vorschau. Mit confirm=true wird vorher der
    aktuelle Stand selbst als Backup abgelegt (neue backup_id), danach
    dieselben Prüfungen wie beim Update inkl. automatischem Zurückstellen.
    """
    d = _hosting_domain(domain)
    m = _WP_BACKUP_ID_RE.match((backup_id or "").strip())
    if not m or m.group(3) != d:
        raise ValueError(f"backup_id {backup_id!r} gehört nicht zur Domain '{d}'.")

    def target(client: paramiko.SSHClient, state: dict[str, Any]):
        rec = _phps_read_backup(client, backup_id)
        target_own = dict(rec["own_settings"])
        notes = []
        for k in state["own"]:
            if k not in target_own:
                target_own[k] = rec["show_settings"].get(k, state["own"][k])
                notes.append(f"{k} war damals geerbt und bleibt ein eigener Wert ({target_own[k]!r}).")
        changes = [{"bereich": "settings", "wert": k, "alt": state["own"].get(k), "neu": v}
                   for k, v in target_own.items() if state["own"].get(k) != v]
        if _phps_norm_block(state["additional"]) != _phps_norm_block(rec["additional_directives"]):
            changes.append({"bereich": "additional_directives", "alt": state["additional"],
                            "neu": rec["additional_directives"]})
        show_expect = {k: v for k, v in target_own.items() if k in state["standard"]}
        return target_own, rec["additional_directives"], changes, show_expect, notes

    return _phps_change(d, target, confirm, f"php_settings_rollback:{m.group(0)}")


# ---------------------------------------------------------------------------
# Webserver-Einstellungen (Browser-Caching, nginx-Cache)
# ---------------------------------------------------------------------------
#
# Lesen/Schreiben pro Domain über `plesk bin domain --show-web-server-settings`
# bzw. `--update-web-server-settings <domain> -<option> <wert>`. Freigegeben sind
# nur Optionen mit einfachem, geprüftem Wertformat (Zahlen, true/false) und
# "nginx-directives" (zusätzliche nginx-Anweisungen, vhost_nginx.conf) mit
# eigener Whitelist (_web_nginx_check). Ablauf wie bei den PHP-Einstellungen:
# Vorschau, Backup (root-only), Schreiben, Kontrolle, bei Fehler automatisch
# zurück.

_WEB_BOOL = r"true|false"
_WEB_RULES: dict[str, str] = {
    "web-server-expires": r"\d{1,9}",
    "web-server-expires-static-only": _WEB_BOOL,
    "nginx-serve-static": _WEB_BOOL,
    "nginx-cache-enabled": _WEB_BOOL,
    "nginx-cache-size": r"\d{1,12}",
    "nginx-cache-timeout": r"\d{1,9}",
    "nginx-cache-bypass-header-nocache": _WEB_BOOL,
    "nginx-cache-bypass-header-auth": _WEB_BOOL,
    "nginx-cache-bypass-get-nocache": _WEB_BOOL,
    "nginx-cache-use-stale-5xx": _WEB_BOOL,
    "nginx-cache-use-stale-4xx": _WEB_BOOL,
    "nginx-cache-use-stale-updating": _WEB_BOOL,
}
# Freies Format, geprüft von _web_nginx_check statt per Regex. Geschrieben wird
# direkt in vhost_nginx.conf (siehe _web_nginx_change), Plesk zeigt den Inhalt
# unter "additional nginx settings" an.
_WEB_NGINX = "nginx-directives"
_WEB_NGINX_MAX = 20000
_WEB_NGINX_ALLOWED = ("add_header", "expires")
_WEB_OPTIONS = sorted([*_WEB_RULES, _WEB_NGINX])
_WEB_BACKUP_DIR = os.environ.get("WEB_SETTINGS_BACKUP_DIR", "/root/.plesk-mcp/web-server-backups").strip()


def _web_norm(text: str) -> str:
    """Wie Plesk den Inhalt anzeigt (siehe _web_parse): Zeilenenden ohne
    Leerzeichen, ohne Leerzeilen am Anfang und Ende."""
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return "\n".join(lines)


def _web_nginx_tokens(text: str) -> list[tuple[str, int]]:
    """Zerlegt nginx-Konfiguration in (Token, Zeile). Kommentare fallen weg,
    ";", "{" und "}" sind eigene Tokens, Strings in Anführungszeichen bleiben
    inklusive Anführungszeichen erhalten."""
    tokens: list[tuple[str, int]] = []
    i, n, line = 0, len(text), 1
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
        elif c in " \t\r":
            i += 1
        elif c == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif c in ";{}":
            tokens.append((c, line))
            i += 1
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c:
                if text[j] == "\n":
                    raise ValueError(f"Zeile {line}: Zeilenumbruch in einem String ist nicht erlaubt.")
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                raise ValueError(f"Zeile {line}: String ohne schliessendes {c}.")
            tokens.append((text[i:j + 1], line))
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in " \t\r\n;{}#\"'":
                j += 2 if text[j] == "\\" else 1
            tokens.append((text[i:j], line))
            i = j
    return tokens


def _web_nginx_check(text: str) -> None:
    """Whitelist für zusätzliche nginx-Anweisungen: nur add_header, expires
    und location-Blöcke, die ausschliesslich add_header/expires enthalten.
    Kommentare und Leerzeilen sind erlaubt, alles andere wird abgelehnt."""
    if len(text) > _WEB_NGINX_MAX:
        raise ValueError(f"nginx-directives ist zu lang (max. {_WEB_NGINX_MAX} Zeichen).")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text):
        raise ValueError("nginx-directives enthält Steuerzeichen.")
    allowed = ", ".join(_WEB_NGINX_ALLOWED) + " und location-Blöcke mit nur " + "/".join(_WEB_NGINX_ALLOWED)
    tokens = _web_nginx_tokens(text)
    i, depth = 0, 0
    while i < len(tokens):
        tok, line = tokens[i]
        if tok == "}":
            if depth == 0:
                raise ValueError(f"Zeile {line}: '}}' ohne offenen location-Block.")
            depth -= 1
            i += 1
            continue
        if tok in (";", "{"):
            raise ValueError(f"Zeile {line}: unerwartetes '{tok}'.")
        j = i + 1
        while j < len(tokens) and tokens[j][0] not in (";", "{", "}"):
            j += 1
        args = [t for t, _ in tokens[i + 1:j]]
        end = tokens[j][0] if j < len(tokens) else ""
        if tok == "location" and depth == 0:
            if end != "{":
                raise ValueError(f"Zeile {line}: location ohne '{{'.")
            if not 1 <= len(args) <= 2 or (len(args) == 2 and args[0] not in ("=", "~", "~*", "^~")):
                raise ValueError(f"Zeile {line}: ungültiger location-Ausdruck.")
            depth += 1
            i = j + 1
            continue
        if tok not in _WEB_NGINX_ALLOWED:
            what = "location in einem location-Block" if tok == "location" else f"'{tok}'"
            raise ValueError(f"Zeile {line}: {what} ist nicht erlaubt. Erlaubt: {allowed}.")
        if end != ";":
            raise ValueError(f"Zeile {line}: '{tok}' muss mit ';' enden.")
        if tok == "add_header":
            if len(args) not in (2, 3) or (len(args) == 3 and args[2] != "always"):
                raise ValueError(f"Zeile {line}: add_header erwartet <Name> <Wert> [always].")
            if not re.fullmatch(r"[A-Za-z0-9-]+", args[0]):
                raise ValueError(f"Zeile {line}: ungültiger Header-Name {args[0]!r}.")
        else:
            if len(args) not in (1, 2) or (len(args) == 2 and args[0] != "modified"):
                raise ValueError(f"Zeile {line}: expires erwartet [modified] <Zeit>.")
            if not re.fullmatch(r"off|epoch|max|@[0-9hm]+|[+-]?[0-9][0-9A-Za-z]*", args[-1]):
                raise ValueError(f"Zeile {line}: ungültige expires-Zeit {args[-1]!r}.")
        i = j + 1
    if depth:
        raise ValueError("location-Block ohne schliessendes '}'.")


def _web_append(old: str, add: str) -> str:
    """Hängt add an old an; ist add schon (als ganze Zeilen) enthalten, bleibt old."""
    if not add:
        return old
    if f"\n{add}\n" in f"\n{old}\n":
        return old
    return f"{old}\n{add}" if old else add


def _web_diff(old: str, new: str) -> str:
    import difflib
    return "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), "alt", "neu", lineterm=""))


def _web_check(key: Any, value: Any) -> tuple[str, str]:
    if not isinstance(key, str) or key.strip() not in _WEB_OPTIONS:
        raise ValueError(f"Option {key!r} ist nicht freigegeben. Erlaubt: {', '.join(_WEB_OPTIONS)}.")
    k = key.strip()
    if k == _WEB_NGINX:
        if not isinstance(value, str):
            raise ValueError(f"Wert für '{k}' muss ein String sein (kompletter Inhalt der nginx-Anweisungen).")
        v = _web_norm(value)
        _web_nginx_check(v)
        return k, v
    if isinstance(value, bool):
        value = "true" if value else "false"
    if not isinstance(value, (str, int)) or isinstance(value, float):
        raise ValueError(f"Wert für '{k}' muss ein String oder eine Zahl sein.")
    v = str(value).strip().lower()
    if not re.fullmatch(_WEB_RULES[k], v):
        raise ValueError(f"Ungültiger Wert {value!r} für '{k}' (erwartet: "
                         + ("true/false" if _WEB_RULES[k] == _WEB_BOOL else "Zahl") + ").")
    return k, v


def _web_parse(text: str) -> dict[str, str]:
    """Ausgabe von --show-web-server-settings: "[name]" gefolgt vom Wert."""
    out: dict[str, str] = {}
    key = None
    lines: list[str] = []
    for line in text.splitlines() + ["[__ende__]"]:
        m = re.match(r"^\[(.+)\]$", line.strip())
        if m:
            if key is not None:
                while lines and not lines[-1].strip():
                    lines.pop()
                while lines and not lines[0].strip():
                    lines.pop(0)
                out[key] = "\n".join(lines)
            key, lines = m.group(1), []
        elif key is not None:
            lines.append(line.rstrip())
    return out


def _web_read(client: paramiko.SSHClient, domain: str) -> dict[str, str]:
    out, err, rc = _client_exec(client, f"plesk bin domain --show-web-server-settings {shlex.quote(domain)}",
                                timeout=_HOSTING_TIMEOUT)
    settings = _web_parse(out) if rc == 0 else {}
    if not settings:
        raise ValueError(f"Webserver-Einstellungen von '{domain}' nicht lesbar"
                         + (f": {(err.strip() or out.strip())[:300]}" if (err.strip() or out.strip()) else "."))
    return settings


def _web_write(client: paramiko.SSHClient, domain: str, values: dict[str, str]) -> tuple[int, str]:
    args = " ".join(f"-{k} {shlex.quote(v)}" for k, v in values.items())
    out, err, rc = _client_exec(
        client, f"plesk bin domain --update-web-server-settings {shlex.quote(domain)} {args}",
        timeout=_HOSTING_TIMEOUT)
    return rc, (err.strip() or out.strip())[-500:]


def _root_json_write(client: paramiko.SSHClient, bdir: str, backup_id: str, record: dict[str, Any]) -> None:
    data = json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")
    d = shlex.quote(bdir)
    path = shlex.quote(f"{bdir}/{backup_id}.json")
    _, err, rc = _client_exec(client, f"umask 077 && mkdir -p {d} && chmod 700 {d} && set -C && cat > {path}",
                              stdin_data=data)
    if rc != 0:
        raise SSHError(f"Backup konnte nicht angelegt werden - nichts geändert: {err.strip()}")


def _root_json_read(client: paramiko.SSHClient, bdir: str, backup_id: str, domain: str) -> dict[str, Any]:
    m = _WP_BACKUP_ID_RE.match((backup_id or "").strip())
    if not m or m.group(3) != domain:
        raise ValueError(f"backup_id {backup_id!r} gehört nicht zur Domain '{domain}'.")
    out, err, rc = _client_exec(client, f"cat -- {shlex.quote(f'{bdir}/{m.group(0)}.json')}")
    if rc != 0:
        raise ValueError(f"Backup '{m.group(0)}' nicht gefunden: {err.strip()}")
    try:
        rec = json.loads(out)
        if rec.get("domain") != domain:
            raise ValueError("Inhalt passt nicht zur backup_id")
    except (ValueError, TypeError) as e:
        raise ValueError(f"Backup '{m.group(0)}' ist ungültig: {e}")
    return rec


def _web_change(domain: str, targets_fn, confirm: bool, source: str) -> str:
    d = _hosting_domain(domain)
    lock = _phps_lock(f"web:{d}", d, "eine Änderung der Webserver-Einstellungen")
    try:
        client = _ssh_connect()
        try:
            before = _web_read(client, d)
            targets = targets_fn(client, d, before)
            for k in targets:
                if k not in before:
                    raise ValueError(f"Option '{k}' wird von Plesk für '{d}' nicht angezeigt - nichts geändert.")
            changes = [{"option": k, "alt": before[k], "neu": v} for k, v in targets.items() if before[k] != v]
            result: dict[str, Any] = {"domain": d, "aenderungen": changes}
            if not changes:
                result["ergebnis"] = "Keine Änderung nötig - alle Werte sind bereits gesetzt."
                return _json(result)
            if not confirm:
                result["ergebnis"] = "Vorschau - nichts geschrieben. Mit confirm=true ausführen."
                return _json(result)
            todo = {c["option"]: c["neu"] for c in changes}
            old = {k: before[k] for k in todo}
            baseline = _phps_http(d)[0]
            backup_id = _phps_new_backup_id(d)
            _root_json_write(client, _WEB_BACKUP_DIR, backup_id, {
                "id": backup_id, "created": datetime.datetime.now().isoformat(timespec="seconds"),
                "source": source, "domain": d, "settings": before})
            result["backup_id"] = backup_id
            failed = []
            rc, msg = _web_write(client, d, todo)
            if rc != 0:
                failed.append(f"Plesk: Exit-Code {rc}: {msg}")
            now = _web_read(client, d)
            if rc != 0 and all(now.get(k) == v for k, v in old.items()):
                # Plesk hat abgelehnt und nichts übernommen: kein Rollback nötig.
                result["ergebnis"] = "FEHLER - nichts geändert."
                result["fehlgeschlagene_pruefungen"] = failed
                _audit("web_server_settings", domain=d, source=source, changes=changes, backup_id=backup_id,
                       result="unchanged", failed=failed)
                return _json(result)
            failed += [f"{k}: ist {now.get(k)!r} statt {v!r}" for k, v in todo.items() if now.get(k) != v]
            if not failed and baseline is not None and baseline < 500:
                status, detail = _phps_http(d)
                if status is not None and status >= 500:
                    failed.append(f"Startseite liefert {status} ({detail})")
            if not failed:
                result["ergebnis"] = "Geschrieben und kontrolliert."
                _audit("web_server_settings", domain=d, source=source, changes=changes, backup_id=backup_id,
                       result="ok")
                return _json(result)
            rc, msg = _web_write(client, d, old)
            after = _web_read(client, d)
            rfailed = ([f"Plesk: Exit-Code {rc}: {msg}"] if rc != 0 else []) + [
                f"{k}: ist {after.get(k)!r} statt {v!r}" for k, v in old.items() if after.get(k) != v]
            result["ergebnis"] = ("FEHLER - automatisch auf das Backup zurückgestellt." if not rfailed
                                  else "FEHLER - Zurückstellen ebenfalls fehlgeschlagen, bitte sofort prüfen!")
            result["fehlgeschlagene_pruefungen"] = failed
            if rfailed:
                result["fehler_beim_zurueckstellen"] = rfailed
            _audit("web_server_settings", domain=d, source=source, changes=changes, backup_id=backup_id,
                   result="rolled_back" if not rfailed else "rollback_failed", failed=failed)
            return _json(result)
        finally:
            client.close()
    finally:
        lock.release()


# Zusätzliche nginx-Anweisungen: Plesk kann sie per CLI nur anzeigen, nicht
# setzen. Deshalb wird vhost_nginx.conf direkt geschrieben (atomar per
# Temp-Datei + rename im selben Ordner) und danach per httpdmng in die
# Plesk-Konfiguration übernommen. Das Plesk-Template bindet die Datei nur ein,
# wenn sie beim Generieren existiert - nginx -t läuft deshalb vor UND nach dem
# Reconfigure.

_WEB_NGINX_ROOT = "/var/www/vhosts/system"
_WEB_NGINX_FILE = "vhost_nginx.conf"
_WEB_NGINX_OWNER = "root:nginx"   # so legt Plesk die Datei an
_WEB_NGINX_MODE = "600"
_WEB_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_WEB_OWNER_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}:[a-z_][a-z0-9_-]{0,31}")
_WEB_MODE_RE = re.compile(r"[0-7]{3,4}")


def _web_nginx_paths(domain: str) -> tuple[str, str, str]:
    """(System-Ordner der Domain, conf-Ordner, vhost_nginx.conf) - nur für
    strikt gültige Hostnamen."""
    labels = domain.split(".")
    if (len(domain) > 253 or len(labels) < 2 or "/" in domain or ".." in domain
            or not all(_WEB_LABEL_RE.fullmatch(lb) for lb in labels)):
        raise ValueError(f"Ungültiger Domainname: {domain!r}")
    base = f"{_WEB_NGINX_ROOT}/{domain}"
    conf = f"{base}/conf"
    path = f"{conf}/{_WEB_NGINX_FILE}"
    if posixpath.normpath(path) != path or posixpath.dirname(path) != conf:
        raise ValueError(f"Ungültiger Domainname: {domain!r}")
    return base, conf, path


def _web_nginx_read(client: paramiko.SSHClient, domain: str) -> dict[str, Any]:
    """Aktueller Stand der Datei: exists, owner, mode, content (roh)."""
    base, conf, path = _web_nginx_paths(domain)
    q = shlex.quote
    cmd = (f"b={q(base)}; c={q(conf)}; f={q(path)}; "
           'if [ -L "$b" ] || [ -L "$c" ] || [ ! -d "$c" ]; then echo "conf-Ordner fehlt oder ist ein Symlink" >&2; exit 3; fi; '
           'if [ -L "$f" ]; then echo "Datei ist ein Symlink" >&2; exit 4; fi; '
           'if [ -f "$f" ]; then stat -c "%U:%G %a" -- "$f" && cat -- "$f"; '
           'elif [ -e "$f" ]; then echo "keine normale Datei" >&2; exit 4; '
           "else echo ABSENT; fi")
    out, err, rc = _client_exec(client, cmd)
    if rc != 0:
        raise ValueError(f"{path} nicht lesbar - nichts geändert: {(err.strip() or out.strip())[:300]}")
    head, _, content = out.partition("\n")
    if head.strip() == "ABSENT":
        return {"exists": False, "owner": _WEB_NGINX_OWNER, "mode": _WEB_NGINX_MODE, "content": ""}
    owner, _, mode = head.strip().partition(" ")
    if not _WEB_OWNER_RE.fullmatch(owner) or not _WEB_MODE_RE.fullmatch(mode):
        raise ValueError(f"Besitzer/Rechte von {path} nicht lesbar: {head[:100]!r}")
    return {"exists": True, "owner": owner, "mode": mode, "content": content}


def _web_nginx_put(client: paramiko.SSHClient, domain: str, state: dict[str, Any]) -> tuple[int, str]:
    """Schreibt state (exists/owner/mode/content) atomar; exists=False entfernt die Datei."""
    _, conf, path = _web_nginx_paths(domain)
    q = shlex.quote
    if not state["exists"]:
        out, err, rc = _client_exec(client, f"f={q(path)}; [ -L \"$f\" ] && exit 4; rm -f -- \"$f\"")
        return rc, (err.strip() or out.strip())[-500:]
    owner, mode = state["owner"], state["mode"]
    if not _WEB_OWNER_RE.fullmatch(owner) or not _WEB_MODE_RE.fullmatch(mode):
        raise ValueError("Ungültiger Besitzer/Rechte für vhost_nginx.conf.")
    content = state["content"]
    data = (content if not content or content.endswith("\n") else content + "\n").encode("utf-8")
    cmd = (f"c={q(conf)}; f={q(path)}; "
           'umask 077 && t=$(mktemp "$c/.vhost_nginx.conf.mcp-XXXXXX") || exit 1; '
           f'if cat > "$t" && chown {q(owner)} "$t" && chmod {q(mode)} "$t" && mv -fT -- "$t" "$f"; '
           'then exit 0; else rm -f -- "$t"; exit 1; fi')
    out, err, rc = _client_exec(client, cmd, stdin_data=data)
    return rc, (err.strip() or out.strip())[-500:]


def _web_nginx_test(client: paramiko.SSHClient) -> tuple[bool, str]:
    out, err, rc = _client_exec(client, "nginx -t", timeout=_HOSTING_TIMEOUT)
    return rc == 0, (err.strip() or out.strip())[-500:]


def _web_reconfigure(client: paramiko.SSHClient, domain: str) -> tuple[int, str]:
    out, err, rc = _client_exec(client, f"plesk sbin httpdmng --reconfigure-domain {shlex.quote(domain)}",
                                timeout=_HOSTING_TIMEOUT)
    return rc, (err.strip() or out.strip())[-500:]


def _web_nginx_change(domain: str, target_fn, confirm: bool, source: str) -> str:
    """target_fn(stand) -> Zielstand {exists, content[, owner, mode]}; Ablauf:
    Vorschau, nginx -t vorher, Backup, Datei schreiben, nginx -t, Reconfigure,
    Datei/nginx -t/Startseite prüfen; bei Fehler alter Stand zurück."""
    d = _hosting_domain(domain)
    _web_nginx_paths(d)
    lock = _phps_lock(f"web:{d}", d, "eine Änderung der Webserver-Einstellungen")
    try:
        client = _ssh_connect()
        try:
            cur = _web_nginx_read(client, d)
            target = target_fn(cur)
            old_n, new_n = _web_norm(cur["content"]), _web_norm(target["content"])
            change: dict[str, Any] = {"option": _WEB_NGINX, "alt": old_n, "neu": new_n,
                                      "diff": _web_diff(old_n, new_n)}
            removed = [line for line in change["diff"].splitlines()[2:] if line.startswith("-")]
            if removed:
                change["hinweis"] = (f"{len(removed)} bestehende Zeile(n) werden entfernt bzw. ersetzt "
                                     "(siehe diff). Nur anhängen: mode=\"append\".")
            result: dict[str, Any] = {"domain": d, "datei": _web_nginx_paths(d)[2], "aenderungen": [change]}
            if old_n == new_n:
                result["aenderungen"] = []
                result["ergebnis"] = "Keine Änderung nötig - der Inhalt ist bereits gesetzt."
                return _json(result)
            if not confirm:
                result["ergebnis"] = "Vorschau - nichts geschrieben. Mit confirm=true ausführen."
                return _json(result)
            new_state = {"exists": target["exists"], "content": new_n,
                         "owner": target.get("owner") or cur["owner"],
                         "mode": target.get("mode") or cur["mode"]}
            ok, msg = _web_nginx_test(client)
            if not ok:
                raise ValueError(f"nginx -t schlägt schon vor der Änderung fehl - nichts geändert: {msg}")
            baseline = _phps_http(d)[0]
            backup_id = _phps_new_backup_id(d)
            _root_json_write(client, _WEB_BACKUP_DIR, backup_id, {
                "id": backup_id, "created": datetime.datetime.now().isoformat(timespec="seconds"),
                "source": source, "domain": d, "changed": [_WEB_NGINX], "nginx_file": cur})
            result["backup_id"] = backup_id

            def same(state: dict[str, Any], want: dict[str, Any]) -> bool:
                return state["exists"] == want["exists"] and _web_norm(state["content"]) == _web_norm(want["content"])

            failed: list[str] = []
            reconfigured = False
            rc, msg = _web_nginx_put(client, d, new_state)
            if rc != 0:
                failed.append(f"Schreiben: Exit-Code {rc}: {msg}")
                if same(_web_nginx_read(client, d), cur):
                    result["ergebnis"] = "FEHLER - nichts geändert."
                    result["fehlgeschlagene_pruefungen"] = failed
                    _audit("web_server_settings", domain=d, source=source, changes=[change],
                           backup_id=backup_id, result="unchanged", failed=failed)
                    return _json(result)
            if not failed and new_state["exists"]:
                # Beim Entfernen (Rollback auf "keine Datei") verweist die alte
                # nginx.conf bis zum Reconfigure noch per include auf die Datei -
                # nginx -t kann erst danach sinnvoll prüfen.
                ok, msg = _web_nginx_test(client)
                if not ok:
                    failed.append(f"nginx -t: {msg}")
            if not failed:
                rc, msg = _web_reconfigure(client, d)
                reconfigured = True
                if rc != 0:
                    failed.append(f"httpdmng --reconfigure-domain: Exit-Code {rc}: {msg}")
            if not failed:
                if not same(_web_nginx_read(client, d), new_state):
                    failed.append("vhost_nginx.conf hat nach dem Reconfigure nicht den geschriebenen Inhalt")
                ok, msg = _web_nginx_test(client)
                if not ok:
                    failed.append(f"nginx -t nach Reconfigure: {msg}")
            if not failed and baseline is not None and baseline < 500:
                status, detail = _phps_http(d)
                if status is not None and status >= 500:
                    failed.append(f"Startseite liefert {status} ({detail})")
            if not failed:
                result["ergebnis"] = "Geschrieben und kontrolliert."
                _audit("web_server_settings", domain=d, source=source, changes=[change], backup_id=backup_id,
                       result="ok")
                return _json(result)

            rfailed: list[str] = []
            rc, msg = _web_nginx_put(client, d, cur)
            if rc != 0:
                rfailed.append(f"Zurückschreiben: Exit-Code {rc}: {msg}")
            if reconfigured:
                rc, msg = _web_reconfigure(client, d)
                if rc != 0:
                    rfailed.append(f"httpdmng --reconfigure-domain: Exit-Code {rc}: {msg}")
            if not same(_web_nginx_read(client, d), cur):
                rfailed.append("vhost_nginx.conf hat nicht wieder den alten Inhalt")
            ok, msg = _web_nginx_test(client)
            if not ok:
                rfailed.append(f"nginx -t: {msg}")
            result["ergebnis"] = (("FEHLER - automatisch auf den alten Stand zurückgestellt"
                                   + ("." if reconfigured else ", nichts neu geladen.")) if not rfailed
                                  else "FEHLER - Zurückstellen ebenfalls fehlgeschlagen, bitte sofort prüfen!")
            result["fehlgeschlagene_pruefungen"] = failed
            if rfailed:
                result["fehler_beim_zurueckstellen"] = rfailed
            _audit("web_server_settings", domain=d, source=source, changes=[change], backup_id=backup_id,
                   result="rolled_back" if not rfailed else "rollback_failed", failed=failed)
            return _json(result)
        finally:
            client.close()
    finally:
        lock.release()


@mcp.tool()
def web_server_settings_show(domain: str) -> str:
    """Zeigt die Webserver-Einstellungen einer Domain (Plesk "Apache &
    nginx-Einstellungen"), u.a. Expires-Header (Browser-Caching),
    nginx-Auslieferung statischer Dateien und nginx-Cache. Entspricht
    `plesk bin domain --show-web-server-settings <domain>`. Die zusätzlichen
    nginx-Anweisungen stehen unter "additional nginx settings" (ändern über
    die Option "nginx-directives"). Rein lesend - kein confirm nötig.
    """
    d = _hosting_domain(domain)
    client = _ssh_connect()
    try:
        settings = _web_read(client, d)
    finally:
        client.close()
    return _json({"domain": d, "einstellungen": settings, "aenderbar": _WEB_OPTIONS})


@mcp.tool()
def web_server_settings_update(domain: str, settings: dict[str, Any], confirm: bool = False,
                               mode: str = "replace") -> str:
    """Ändert Webserver-Einstellungen einer Domain für Speed (Browser-Caching
    und nginx-Cache), z.B. settings={"web-server-expires": "2592000",
    "web-server-expires-static-only": "true", "nginx-serve-static": "true"}
    oder {"nginx-cache-enabled": "true", "nginx-cache-timeout": "60"}.
    Entspricht `plesk bin domain --update-web-server-settings <domain>
    -<option> <wert>`.

    Freigegeben: web-server-expires (Sekunden), web-server-expires-static-only,
    nginx-serve-static, nginx-cache-enabled, nginx-cache-size (Bytes),
    nginx-cache-timeout (Sekunden), nginx-cache-bypass-header-nocache,
    nginx-cache-bypass-header-auth, nginx-cache-bypass-get-nocache,
    nginx-cache-use-stale-5xx, nginx-cache-use-stale-4xx,
    nginx-cache-use-stale-updating (true/false).
    ACHTUNG nginx-Cache: kann bei dynamischen Seiten (Shop, Login,
    Warenkorb) veraltete oder fremde Inhalte ausliefern - nur bei eher
    statischen Seiten einsetzen und danach testen.

    nginx-directives: zusätzliche nginx-Anweisungen der Domain (Plesk
    "Zusätzliche nginx-Anweisungen", vhost_nginx.conf), z.B.
    {"nginx-directives": "add_header X-Robots-Tag \"noindex, nofollow\" always;"}.
    Nur allein, nicht zusammen mit anderen Optionen. Erlaubt sind nur
    add_header, expires und location-Blöcke, die nur add_header/expires
    enthalten, dazu Kommentare und Leerzeilen. mode="replace" (Standard)
    ersetzt den kompletten Inhalt, mode="append" hängt den Wert an. Die
    Vorschau zeigt alt/neu als diff. Geschrieben wird direkt in
    /var/www/vhosts/system/<domain>/conf/vhost_nginx.conf, dann nginx -t,
    erst danach `plesk sbin httpdmng --reconfigure-domain` (lädt nginx neu).

    Ohne confirm=true nur Vorschau (alt/neu). Mit confirm=true: Backup des
    alten Stands (backup_id für web_server_settings_rollback), dann
    Schreiben und Kontrolle (Werte, nginx -t, Startseite ohne 5xx); bei
    einem Fehler automatisch zurück auf den alten Stand.
    """
    if not isinstance(settings, dict) or not settings:
        raise ValueError("settings als Objekt angeben, z.B. {\"web-server-expires\": \"2592000\"}.")
    if mode not in ("replace", "append"):
        raise ValueError("mode muss \"replace\" oder \"append\" sein.")
    checked = dict(_web_check(k, v) for k, v in settings.items())
    if _WEB_NGINX in checked:
        if len(checked) > 1:
            raise ValueError("\"nginx-directives\" bitte allein setzen, nicht zusammen mit anderen Optionen.")
        add = checked[_WEB_NGINX]

        def target(cur: dict[str, Any]) -> dict[str, Any]:
            content = _web_append(_web_norm(cur["content"]), add) if mode == "append" else add
            return {"exists": True, "content": content}

        return _web_nginx_change(domain, target, confirm, "web_server_settings_update"
                                 + (":append" if mode == "append" else ""))
    if mode == "append":
        raise ValueError("mode=\"append\" gilt nur für die Option \"nginx-directives\".")
    return _web_change(domain, lambda client, d, before: checked, confirm, "web_server_settings_update")


@mcp.tool()
def web_server_settings_rollback(domain: str, backup_id: str, confirm: bool = False) -> str:
    """Stellt die freigegebenen Webserver-Einstellungen einer Domain auf den
    Stand eines Backups von web_server_settings_update zurück. Bei einem
    Backup einer nginx-directives-Änderung wird vhost_nginx.conf auf den
    damaligen Stand zurückgeschrieben (gleicher Ablauf mit nginx -t und
    Reconfigure). Ohne confirm=true nur Vorschau, mit confirm=true vorher
    neues Backup und dieselbe Kontrolle wie beim Update.
    """
    d = _hosting_domain(domain)
    client = _ssh_connect()
    try:
        rec = _root_json_read(client, _WEB_BACKUP_DIR, backup_id, d)
    finally:
        client.close()
    nf = rec.get("nginx_file")
    if nf is not None:
        if not (isinstance(nf, dict) and isinstance(nf.get("exists"), bool)
                and isinstance(nf.get("content"), str)
                and _WEB_OWNER_RE.fullmatch(str(nf.get("owner", "")))
                and _WEB_MODE_RE.fullmatch(str(nf.get("mode", "")))):
            raise ValueError("Backup ist ungültig (nginx_file).")
        # Keine Whitelist-Prüfung: es ist der damals gelesene Dateistand.
        return _web_nginx_change(d, lambda cur: dict(nf), confirm, f"web_server_settings_rollback:{backup_id}")

    def targets(client: paramiko.SSHClient, d: str, before: dict[str, str]) -> dict[str, str]:
        old = rec.get("settings")
        if not isinstance(old, dict):
            raise ValueError("Backup ist ungültig (keine Einstellungen).")
        out = {}
        for k in _WEB_RULES:
            v = old.get(k)
            if isinstance(v, str) and v != before.get(k):
                if v and not re.fullmatch(_WEB_RULES[k], v):
                    raise ValueError(f"Backup enthält ungültigen Wert für '{k}'.")
                out[k] = v
        return out

    return _web_change(d, targets, confirm, f"web_server_settings_rollback:{backup_id}")


# ---------------------------------------------------------------------------
# Bilder verlustfrei optimieren (optimize_images / image_backups)
# ---------------------------------------------------------------------------
#
# Der Worker (_IMG_WORKER_SRC) läuft per runuser als System-User der
# Subscription (nie root): Er kann damit nur Dateien ändern, die der Kunde
# selbst ändern darf. Pro Bild: Arbeitskopie unter .plesk-mcp/tmp, GPS-Daten und
# Seriennummern (EXIF, XMP) entfernen, verlustfrei optimieren (jpegoptim ohne
# Qualitätsstufe = nur Huffman-Optimierung; optipng), nur ersetzen, wenn
# kleiner oder Datenschutzdaten entfernt wurden. Farbprofil, Ausrichtung und
# übrige Metadaten bleiben. Backup der Originale per Hardlink unter
# <vhost>/.plesk-mcp/image-backups/<run_id>/ (ausserhalb jedes Docroots).

_IMG_WORKER_SRC = r'''# plesk-mcp image worker - läuft per runuser als System-User der Subscription
# (nie root) auf dem Plesk-Server (Python >= 3.6, nur Standardbibliothek).
import errno
import grp
import json
import os
import pwd
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib

TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
GPS_TAG = 0x8825
EXIF_TAG = 0x8769
# CameraOwnerName, BodySerialNumber, LensSerialNumber, MakerNote (Herstellerdaten inkl. Seriennummern)
PRIVATE_EXIF_TAGS = (0xA430, 0xA431, 0xA435, 0x927C)
XMP_PRIVATE = r"(?:GPS[A-Za-z]*|SerialNumber|LensSerialNumber|BodySerialNumber|CameraOwnerName|OwnerName)"
XMP_ATTR_RE = re.compile(r"\s+[\w.-]+:" + XMP_PRIVATE + r"\s*=\s*(?:\"[^\"]*\"|'[^']*')")
XMP_EMPTY_RE = re.compile(r"<([\w.-]+:" + XMP_PRIVATE + r")\b[^>]*/>")
XMP_ELEM_RE = re.compile(r"<([\w.-]+:" + XMP_PRIVATE + r")\b[^>]*>.*?</\1\s*>", re.S)
XMP_ID = b"http://ns.adobe.com/xap/1.0/\x00"
SKIP_DIRS = (".mcp-trash", ".plesk-mcp")
# Arbeitskopien früherer Versionen (bis 0.18) lagen neben dem Original:
# ".<name>.mcp-<8 hex>.<ext>", optipng legt beim Abbruch zusätzlich "<...>.bak" an.
OWN_TMP_RE = re.compile(r"^\..+\.mcp-[0-9a-f]{8}\.(?:png|jpe?g)(?:\.bak)?$", re.I)
ORPHAN_MIN_AGE = 3600
PNG_FAST_BYTES = 1024 * 1024
IONICE_BIN = "/usr/bin/ionice"
BG_NICE = 10

# Hintergrundmodus: Abbruch kommt als Zeile "cancel" über stdin, bei
# Verbindungsverlust (Container-Neustart, SSH-Abbruch) als EOF auf stdin,
# sonst per SIGTERM/SIGHUP. Alle Wege setzen nur dieses Event; die Schleife
# beendet das laufende Werkzeug, räumt die Arbeitskopie weg und liefert das
# Ergebnis mit "abgebrochen" - nie bleibt eine halb geschriebene Datei zurück.
CANCEL = threading.Event()
CANCEL_REASON = {"grund": None}
IONICE = None


def request_cancel(reason):
    if not CANCEL.is_set():
        CANCEL_REASON["grund"] = reason
        CANCEL.set()


def watch_stdin():
    """Liest im Hintergrundmodus stdin weiter: "cancel" oder EOF = Abbruch."""
    try:
        while True:
            line = sys.stdin.readline()
            if not line:
                request_cancel("verbindung_verloren")
                return
            if line.strip() == "cancel":
                request_cancel("cancel")
                return
    except Exception:  # noqa
        request_cancel("verbindung_verloren")


def emit(obj):
    """Fortschrittszeile an den MCP-Server; Schreibfehler = Verbindung weg."""
    try:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        request_cancel("verbindung_verloren")


class Bad(Exception):
    pass


def scrub_tiff(buf, start, end):
    """Entfernt GPS-Daten und Seriennummern in einem TIFF/EXIF-Block (gleiche
    Länge, Werte werden mit Nullbytes überschrieben). Rückgabe: Liste der
    entfernten Gruppen."""
    if end - start < 8:
        raise Bad("exif")
    bo = {b"II": "<", b"MM": ">"}.get(bytes(buf[start:start + 2]))
    if bo is None:
        raise Bad("exif")

    def u16(o):
        if o < start or o + 2 > end:
            raise Bad("exif")
        return struct.unpack(bo + "H", bytes(buf[o:o + 2]))[0]

    def u32(o):
        if o < start or o + 4 > end:
            raise Bad("exif")
        return struct.unpack(bo + "I", bytes(buf[o:o + 4]))[0]

    def entries(ifd_off):
        base = start + ifd_off
        n = u16(base)
        if n > 1000:
            raise Bad("exif")
        out = []
        for i in range(n):
            p = base + 2 + 12 * i
            if p + 12 > end:
                raise Bad("exif")
            out.append((p, u16(p), u16(p + 2), u32(p + 4)))
        return base, n, out

    def zero_value(p, typ, count):
        size = TYPE_SIZES.get(typ, 1) * count
        if size <= 4:
            buf[p + 8:p + 12] = b"\x00" * 4
        else:
            off = start + u32(p + 8)
            if off < start or off + size > end:
                raise Bad("exif")
            buf[off:off + size] = b"\x00" * size

    removed = []
    _, _, ifd0 = entries(u32(start + 4))
    for p, tag, typ, count in ifd0:
        if tag == GPS_TAG:
            gbase, gn, gents = entries(u32(p + 8))
            if gn:
                for gp, _, gtyp, gcount in gents:
                    zero_value(gp, gtyp, gcount)
                buf[gbase + 2:gbase + 2 + 12 * gn] = b"\x00" * (12 * gn)
                buf[gbase:gbase + 2] = struct.pack(bo + "H", 0)
                removed.append("gps")
        elif tag == EXIF_TAG:
            _, _, eents = entries(u32(p + 8))
            for ep, etag, etyp, ecount in eents:
                if etag in PRIVATE_EXIF_TAGS:
                    zero_value(ep, etyp, ecount)
                    if "seriennummern" not in removed:
                        removed.append("seriennummern")
    return removed


def scrub_xmp(data):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Bad("xmp")
    new = XMP_ELEM_RE.sub("", XMP_EMPTY_RE.sub("", XMP_ATTR_RE.sub("", text)))
    return (new.encode("utf-8"), True) if new != text else (data, False)


def scrub_jpeg(data):
    if data[:2] != b"\xff\xd8":
        raise Bad("jpeg")
    out = [data[:2]]
    removed = []
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            raise Bad("jpeg")
        marker = data[i + 1]
        if marker == 0xFF:
            out.append(data[i:i + 1])
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            out.append(data[i:i + 2])
            i += 2
            continue
        if marker in (0xDA, 0xD9):
            out.append(data[i:])
            break
        if i + 4 > len(data):
            raise Bad("jpeg")
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        seg_end = i + 2 + length
        if length < 2 or seg_end > len(data):
            raise Bad("jpeg")
        payload = data[i + 4:seg_end]
        if marker == 0xE1 and payload[:6] == b"Exif\x00\x00":
            buf = bytearray(payload)
            for r in scrub_tiff(buf, 6, len(buf)):
                if r not in removed:
                    removed.append(r)
            out.append(data[i:i + 4] + bytes(buf))
        elif marker == 0xE1 and payload.startswith(XMP_ID):
            new, changed = scrub_xmp(payload[len(XMP_ID):])
            if changed:
                seg = XMP_ID + new
                if len(seg) + 2 > 65535:
                    raise Bad("xmp")
                out.append(b"\xff\xe1" + struct.pack(">H", len(seg) + 2) + seg)
                if "xmp" not in removed:
                    removed.append("xmp")
            else:
                out.append(data[i:seg_end])
        else:
            out.append(data[i:seg_end])
        i = seg_end
    return b"".join(out), removed


def png_chunk(typ, body):
    return struct.pack(">I", len(body)) + typ + body + struct.pack(">I", zlib.crc32(typ + body) & 0xFFFFFFFF)


def scrub_png(data):
    sig = b"\x89PNG\r\n\x1a\n"
    if data[:8] != sig:
        raise Bad("png")
    out = [sig]
    removed = []
    i = 8
    while i < len(data):
        if i + 8 > len(data):
            raise Bad("png")
        length = struct.unpack(">I", data[i:i + 4])[0]
        typ = data[i + 4:i + 8]
        end = i + 12 + length
        if end > len(data):
            raise Bad("png")
        body = data[i + 8:i + 8 + length]
        if typ == b"eXIf":
            buf = bytearray(body)
            rem = scrub_tiff(buf, 0, len(buf))
            removed += [r for r in rem if r not in removed]
            out.append(png_chunk(typ, bytes(buf)) if rem else data[i:end])
        elif typ in (b"iTXt", b"tEXt") and body.startswith(b"XML:com.adobe.xmp\x00"):
            head = b"XML:com.adobe.xmp\x00"
            rest = body[len(head):]
            if typ == b"iTXt":
                if len(rest) < 2 or rest[0] != 0:
                    out.append(data[i:end])  # komprimiertes XMP: unverändert
                    i = end
                    continue
                parts = rest[2:].split(b"\x00", 2)
                if len(parts) != 3:
                    raise Bad("png")
                new, changed = scrub_xmp(parts[2])
                nb = head + rest[:2] + parts[0] + b"\x00" + parts[1] + b"\x00" + new
            else:
                new, changed = scrub_xmp(rest)
                nb = head + new
            if changed:
                out.append(png_chunk(typ, nb))
                if "xmp" not in removed:
                    removed.append("xmp")
            else:
                out.append(data[i:end])
        else:
            out.append(data[i:end])
        i = end
        if typ == b"IEND":
            break
    return b"".join(out), removed


def valid(path, kind):
    with open(path, "rb") as f:
        head = f.read(8)
        f.seek(-12, 2)
        tail = f.read(12)
    if kind == "jpeg":
        return head[:2] == b"\xff\xd8" and tail.rstrip(b"\x00")[-2:] == b"\xff\xd9"
    return head == b"\x89PNG\r\n\x1a\n" and tail[4:8] == b"IEND"


def lpath(base, parts):
    """Pfad unter base, ohne Symlinks in irgendeinem Teil."""
    p = base
    for c in parts:
        p = os.path.join(p, c)
        st = os.lstat(p)
        if stat.S_ISLNK(st.st_mode):
            raise Bad("symlink:" + "/".join(parts))
    return p


def collect(start, rel, a, skipped, orphans):
    files = []
    st = os.lstat(start)
    if stat.S_ISREG(st.st_mode):
        cands = [(start, rel)]
    else:
        cands = []
        for root, dirs, names in os.walk(start, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS
                             and not os.path.islink(os.path.join(root, d)))
            if not a["recursive"]:
                dirs[:] = []
            r = os.path.relpath(root, start)
            for n in sorted(names):
                cands.append((os.path.join(root, n), rel + ([] if r == "." else r.split(os.sep)) + [n]))
    for full, parts in cands:
        name = parts[-1]
        if ".mcp-" in name:
            orphans.append((full, parts))
            continue
        check_candidate(full, parts, a, skipped, files)
    return files


def image_kind(name):
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return "jpeg" if ext in ("jpg", "jpeg") else "png" if ext == "png" else None


def check_candidate(full, parts, a, skipped, files):
    """Prüft eine Datei und hängt sie an files an; Rückgabe: Grund, warum
    nicht (None = übernommen)."""
    kind = image_kind(parts[-1])
    if kind is None:
        return "kein_bild"
    st = os.lstat(full)
    if not stat.S_ISREG(st.st_mode):
        skipped["symlink_oder_kein_file"] = skipped.get("symlink_oder_kein_file", 0) + 1
        return "symlink_oder_kein_file"
    if st.st_nlink > 1:
        skipped["hardlink"] = skipped.get("hardlink", 0) + 1
        return "hardlink"
    if st.st_size > a["max_bytes"]:
        skipped["zu_gross"] = skipped.get("zu_gross", 0) + 1
        return "zu_gross"
    files.append((full, parts, kind, st))
    return None


def collect_list(base, a, skipped, details):
    """Gezielte Dateiliste (Parameter dateien): jede Datei einzeln prüfen,
    ohne Symlink in irgendeinem Pfadteil; Probleme landen mit Grund unter
    details, der Lauf geht mit den übrigen Dateien weiter."""
    files = []
    seen = set()
    texts = {"kein_bild": "keine .jpg/.jpeg/.png-Datei", "nicht_gefunden": "Datei nicht gefunden",
             "symlink": "Pfad enthält einen Symlink", "symlink_oder_kein_file": "Symlink oder keine normale Datei",
             "hardlink": "Datei hat weitere Hardlinks", "zu_gross": "Datei über dem Grössenlimit"}
    for parts in a["dateien"]:
        rel = "/".join(parts)
        if rel in seen:
            continue
        seen.add(rel)
        msg = None
        try:
            reason = check_candidate(lpath(base, parts), parts, a, skipped, files)
        except Bad:
            reason = "symlink"
        except FileNotFoundError:
            reason = "nicht_gefunden"
        except OSError as e:
            # nur den Fehlertext, nie den absoluten Serverpfad
            reason, msg = "nicht_gefunden", "%s: %s" % (type(e).__name__, e.strerror or "Fehler beim Zugriff")
        if reason is None:
            continue
        if reason in ("kein_bild", "nicht_gefunden", "symlink"):
            skipped[reason] = skipped.get(reason, 0) + 1
        details.append({"datei": rel, "grund": reason, "meldung": msg or texts.get(reason, reason)})
    return files


class ToolTimeout(Exception):
    pass


class Cancelled(Exception):
    pass


class Rejected(Exception):
    """Datei wird bewusst nicht bearbeitet (kein Fehler unseres Codes)."""
    def __init__(self, reason, msg):
        Exception.__init__(self, msg)
        self.reason = reason


def run_tool(cmd, timeout):
    """Ohne -q/-quiet: sonst schreiben jpegoptim und optipng bei abgelehnten
    Dateien gar nichts und es bleibt nur der Exit-Code. Läuft in kurzen
    Wartezyklen, damit ein Abbruch das Werkzeug sofort beendet."""
    if IONICE:
        cmd = [IONICE, "-c2", "-n7"] + cmd
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    end = time.time() + timeout
    while True:
        try:
            out, err = p.communicate(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            if CANCEL.is_set() or time.time() > end:
                p.kill()
                p.communicate()
                raise Cancelled() if CANCEL.is_set() else ToolTimeout()
    lines = [" ".join(l.split()) for l in (err + out).decode("utf-8", "replace").splitlines()]
    lines = [l for l in lines if l]
    hits = [l for l in lines if re.search(r"error|warning|invalid|not a|unrecognized|corrupt|premature", l, re.I)
            and not re.search(r"status report|have been processed|have been encountered", l, re.I)]
    return p.returncode, " | ".join(hits or lines)[-300:]


def sniff(head):
    """Tatsächliches Format anhand der ersten Bytes."""
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"GIF8":
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:4] in (b"\x00\x00\x01\x00", b"\x00\x00\x02\x00"):
        return "ico"
    if head[:2] == b"BM":
        return "bmp"
    if head[4:8] == b"ftyp":
        return "heif/avif"
    if head.lstrip()[:5] in (b"<?xml", b"<svg "):
        return "svg"
    return None


def exc_text(e):
    """Fehlermeldung, die nie leer ist: eigene Fehler (Bad) mit Text, sonst Typ und Text."""
    text = str(e).strip()
    if isinstance(e, Bad):
        return (text or "unbekannter Fehler")[:300]
    return ("%s: %s" % (type(e).__name__, text or repr(e)))[:300]


def owner_of(st):
    try:
        u = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        u = str(st.st_uid)
    try:
        g = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        g = str(st.st_gid)
    return "%s:%s" % (u, g)


def handle_orphans(orphans, dry_run):
    """Verwaiste Temp-Dateien (".mcp-" im Namen) im Zielpfad melden. Gelöscht
    (nur im echten Lauf) wird eine Datei nur, wenn sie genau dem Namensschema
    der Arbeitskopien dieses Workers entspricht, eine normale Datei des
    Subscription-Users ohne weitere Hardlinks ist und älter als 1 Stunde.
    Begründung: Die Sperre pro Domain verhindert parallele Läufe, ein Lauf
    dauert höchstens wenige Minuten - eine ältere Datei gehört sicher zu
    keinem laufenden Vorgang. Andere ".mcp-"-Dateien (z.B. von Dateitools)
    werden nur gemeldet."""
    out = []
    now = time.time()
    for full, parts in orphans:
        rel = "/".join(parts)
        try:
            st = os.lstat(full)
        except OSError:
            continue
        age = now - st.st_mtime
        e = {"datei": rel, "bytes": st.st_size, "alter_min": int(age // 60)}
        if not OWN_TMP_RE.match(parts[-1]):
            e["aktion"] = "behalten: nicht das Namensschema der Bildoptimierung"
        elif not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid():
            e["aktion"] = "behalten: keine normale Datei des Subscription-Users"
        elif age < ORPHAN_MIN_AGE:
            e["aktion"] = "behalten: jünger als 1 Stunde"
        elif dry_run:
            e["aktion"] = "wird im echten Lauf gelöscht"
        else:
            try:
                os.unlink(full)
                e["aktion"] = "gelöscht"
            except OSError as ex:
                e["aktion"] = "behalten: " + exc_text(ex)
        out.append(e)
    return out


def clean_tmp(root, keep):
    """Arbeitsverzeichnisse abgebrochener Läufe (älter als 1 Tag) entfernen."""
    if not os.path.isdir(root) or os.path.islink(root):
        return
    for n in os.listdir(root):
        p = os.path.join(root, n)
        try:
            if n != keep and not os.path.islink(p) and os.path.isdir(p) and time.time() - os.lstat(p).st_mtime > 86400:
                shutil.rmtree(p)
        except OSError:
            pass


def optimize(a):
    global IONICE
    t0 = time.time()
    base = a["base"]
    background = bool(a.get("background"))
    priority = None
    if background:
        # Fremde Site soll nicht leiden: niedrige CPU- und IO-Priorität, vererbt
        # an jpegoptim/optipng. Läuft per runuser ausserhalb der CloudLinux-LVE
        # des Accounts (pam_lve hängt nur an su/sshd), daher ist das die
        # einzige Bremse.
        try:
            if os.nice(0) < BG_NICE:
                os.nice(BG_NICE - os.nice(0))
        except OSError:
            pass
        IONICE = IONICE_BIN if os.path.exists(IONICE_BIN) else None
        priority = {"nice": os.nice(0), "ionice": bool(IONICE)}
    skipped = {}
    orphans = []
    done, errors, details = [], [], []
    if a.get("dateien"):
        files = collect_list(base, a, skipped, details)
    else:
        start = lpath(base, a["parts"])
        files = collect(start, a["parts"], a, skipped, orphans)
    orphan_report = handle_orphans(orphans, a["dry_run"])
    files.sort(key=lambda f: "/".join(f[1]))
    if a.get("start_after"):
        files = [f for f in files if "/".join(f[1]) > a["start_after"]]
    if a.get("max_files") and len(files) > a["max_files"]:
        skipped["limit_max_files"] = len(files) - a["max_files"]
        files = files[:a["max_files"]]
    tools = {"jpeg": a["jpegoptim"], "png": a["optipng"]}
    budget = a.get("time_budget", 30)
    deadline = None if budget is None else t0 + budget
    tool_timeout = a.get("tool_timeout", 15)
    next_after = None
    checked = 0
    dir_ok = {}
    no_write = {}
    total_before = total_after = 0
    backup_root = os.path.join(base, ".plesk-mcp", "image-backups", a["run_id"])
    # Arbeitskopien ausserhalb der Webordner (nicht per Web erreichbar, auch
    # in Ordnern ohne Schreibrecht möglich)
    tmp_root = os.path.join(base, ".plesk-mcp", "tmp")
    tmp_dir = os.path.join(tmp_root, a["run_id"])
    try:
        os.makedirs(tmp_dir, 0o700, exist_ok=True)
        clean_tmp(tmp_root, a["run_id"])
    except OSError:
        # .plesk-mcp nicht beschreibbar: privates Temp-Verzeichnis des Users
        tmp_dir = tempfile.mkdtemp(prefix="plesk-mcp-img-")
    for idx, (full, parts, kind, st) in enumerate(files):
        if CANCEL.is_set():
            break
        if deadline is not None and time.time() > deadline:
            next_after = "/".join(files[idx - 1][1]) if idx else None
            skipped["zeitbudget_rest"] = len(files) - idx
            break
        checked += 1
        rel = "/".join(parts)
        if background:
            emit({"fortschritt": {"bearbeitet": idx, "gesamt": len(files), "datei": rel, "optimiert": len(done),
                                  "ersparnis_bytes": total_before - total_after,
                                  "uebersprungen": sum(skipped.values()), "fehler": len(errors)}})
        else:
            sys.stdout.write(".\n")
            sys.stdout.flush()
        if not tools[kind] or not os.path.exists(tools[kind]):
            skipped["kein_werkzeug_" + kind] = skipped.get("kein_werkzeug_" + kind, 0) + 1
            continue
        d, name = os.path.split(full)
        ext = name.rsplit(".", 1)[-1]
        tmp = os.path.join(tmp_dir, "%d.%s" % (idx, ext))
        local = None
        if d not in dir_ok:
            dir_ok[d] = os.access(d, os.W_OK | os.X_OK)
        if not dir_ok[d]:
            # gleiche Prüfung im Probelauf, damit er das Ergebnis des echten Laufs vorhersagt
            skipped["keine_schreibrechte"] = skipped.get("keine_schreibrechte", 0) + 1
            if d not in no_write:
                try:
                    own = owner_of(os.lstat(d))
                except OSError:
                    own = "?"
                no_write[d] = {"ordner": "/".join(parts[:-1]), "besitzer": own, "dateien": 0}
            no_write[d]["dateien"] += 1
            continue
        try:
            with open(full, "rb") as f:
                data = f.read()
            real = sniff(data[:16])
            if real != kind:
                raise Rejected("format_passt_nicht_zur_endung",
                               "Inhalt ist %s, Endung .%s" % (real or "kein bekanntes Bildformat", ext))
            try:
                clean, removed = (scrub_jpeg if kind == "jpeg" else scrub_png)(data)
            except Bad:
                clean, removed = data, ["metadaten_nicht_lesbar"]
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(clean)
            if kind == "jpeg":
                rc, msg = run_tool([tools[kind], "-p", tmp], tool_timeout)
            else:
                # grosse PNGs mit -o1 (ebenfalls verlustfrei, deutlich schneller), sonst Timeouts
                level = "-o1" if st.st_size >= PNG_FAST_BYTES else "-o2"
                rc, msg = run_tool([tools[kind], level, "-preserve", tmp], tool_timeout)
            if rc != 0:
                raise Rejected("werkzeug_abgelehnt", "%s Exit-Code %d: %s" % (
                    os.path.basename(tools[kind]), rc, msg or "keine Ausgabe"))
            new_size = os.path.getsize(tmp)
            if not valid(tmp, kind):
                raise Bad("ergebnis_ungueltig: %s lieferte keine gültige Datei" % os.path.basename(tools[kind]))
            privacy = [r for r in removed if r != "metadaten_nicht_lesbar"]
            if new_size >= st.st_size and not privacy:
                os.unlink(tmp)
                skipped["nicht_kleiner"] = skipped.get("nicht_kleiner", 0) + 1
                continue
            entry = {"datei": rel, "vorher": st.st_size, "nachher": new_size, "entfernt": removed}
            if not a["dry_run"]:
                now = os.lstat(full)
                if (now.st_ino, now.st_size, now.st_mtime_ns) != (st.st_ino, st.st_size, st.st_mtime_ns):
                    raise Bad("datei_waehrend_lauf_geaendert")
                os.chmod(tmp, stat.S_IMODE(st.st_mode))
                if a["keep_backup"]:
                    bpath = os.path.join(backup_root, *parts)
                    os.makedirs(os.path.dirname(bpath), 0o700, exist_ok=True)
                    try:
                        os.link(full, bpath)
                    except OSError as e:
                        # EXDEV: anderes Dateisystem; EPERM/EACCES: fremde Datei (protected_hardlinks)
                        if e.errno not in (errno.EXDEV, errno.EPERM, errno.EACCES):
                            raise
                        shutil.copy2(full, bpath)
                try:
                    os.replace(tmp, full)
                except OSError as e:
                    if e.errno != errno.EXDEV:
                        raise
                    # anderes Dateisystem: nur für den Moment des Ersetzens eine Kopie im
                    # Zielordner (wird im finally entfernt, falls etwas schiefgeht)
                    local = os.path.join(d, ".%s.mcp-%s.%s" % (name[:150], os.urandom(4).hex(), ext))
                    shutil.copyfile(tmp, local)
                    os.chmod(local, stat.S_IMODE(st.st_mode))
                    os.replace(local, full)
                    local = None
                    os.unlink(tmp)
                os.utime(full, ns=(st.st_atime_ns, st.st_mtime_ns))
            else:
                os.unlink(tmp)
            total_before += st.st_size
            total_after += new_size
            done.append(entry)
        except Cancelled:
            # Datei zählt nicht als geprüft: Arbeitskopie wird im finally entfernt,
            # das Original bleibt unverändert
            checked -= 1
        except ToolTimeout:
            skipped["timeout"] = skipped.get("timeout", 0) + 1
            details.append({"datei": rel, "grund": "timeout", "meldung": "%s nach %d s abgebrochen" % (
                os.path.basename(tools[kind]), tool_timeout)})
        except Rejected as e:
            skipped[e.reason] = skipped.get(e.reason, 0) + 1
            details.append({"datei": rel, "grund": e.reason, "meldung": str(e)[:300]})
        except Exception as e:  # noqa
            errors.append({"datei": rel, "fehler": exc_text(e)})
        finally:
            for p in (tmp, local):
                if p:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
    shutil.rmtree(tmp_dir, ignore_errors=True)
    aborted = CANCEL_REASON["grund"] if CANCEL.is_set() else None
    if aborted and len(files) > checked:
        skipped["abgebrochen_rest"] = len(files) - checked
    if next_after is None and skipped.get("limit_max_files") and checked:
        next_after = "/".join(files[checked - 1][1])
    done.sort(key=lambda x: x["vorher"] - x["nachher"], reverse=True)
    # Listenlimit: Vordergrund 50 (Antwort bleibt klein), Hintergrund 0 = alles
    # (der MCP-Server blättert selbst)
    lim = a.get("list_limit", 50) or None
    return {"geprueft": checked, "optimiert": len(done), "fortsetzen_ab": next_after, "bytes_vorher": total_before,
            "bytes_nachher": total_after, "dateien": done[:lim], "fehler": errors[:lim],
            "uebersprungen": skipped, "details": details[:lim], "ohne_schreibrechte": list(no_write.values())[:lim],
            "verwaiste_temp_dateien": orphan_report[:lim],
            "anzahl": {"dateien": len(done), "fehler": len(errors), "details": len(details),
                       "ohne_schreibrechte": len(no_write), "verwaiste_temp_dateien": len(orphan_report)},
            "abgebrochen": aborted, "prioritaet": priority, "laufzeit_s": round(time.time() - t0, 1),
            "backup": (".plesk-mcp/image-backups/" + a["run_id"]) if done and a["keep_backup"]
            and not a["dry_run"] else None}


def backups_root(a):
    return os.path.join(a["base"], ".plesk-mcp", "image-backups")


def list_backups(a):
    root = backups_root(a)
    out = []
    if os.path.isdir(root) and not os.path.islink(root):
        for run in sorted(os.listdir(root)):
            p = os.path.join(root, run)
            if os.path.islink(p) or not os.path.isdir(p):
                continue
            n = size = 0
            for r, ds, fs in os.walk(p, followlinks=False):
                for f in fs:
                    st = os.lstat(os.path.join(r, f))
                    n += 1
                    size += st.st_size if st.st_nlink == 1 else 0
            out.append({"run_id": run, "dateien": n, "belegt_bytes": size})
    return {"backups": out}


def run_dir(a):
    if not re.match(r"^\d{14}-[0-9a-f]{8}$", a["run_id"]):
        raise Bad("run_id")
    p = os.path.join(backups_root(a), a["run_id"])
    if os.path.islink(p) or not os.path.isdir(p):
        raise Bad("backup_nicht_gefunden")
    return p


def restore(a):
    p = run_dir(a)
    restored, errors = [], []
    for r, ds, fs in os.walk(p, followlinks=False):
        for f in fs:
            src = os.path.join(r, f)
            parts = os.path.relpath(src, p).split(os.sep)
            try:
                target = lpath(a["base"], parts[:-1])
                tgt = os.path.join(target, parts[-1])
                if os.path.islink(tgt) or os.path.islink(src):
                    raise Bad("symlink")
                if not a["dry_run"]:
                    os.replace(src, tgt)
                restored.append("/".join(parts))
            except Exception as e:  # noqa
                errors.append({"datei": "/".join(parts), "fehler": str(e)[:200]})
    if not a["dry_run"] and not errors:
        shutil.rmtree(p)
    return {"wiederhergestellt": len(restored), "dateien": restored[:50], "fehler": errors[:50]}


def delete(a):
    p = run_dir(a)
    info = [b for b in list_backups(a)["backups"] if b["run_id"] == a["run_id"]]
    if not a["dry_run"]:
        shutil.rmtree(p)
    return {"geloescht": not a["dry_run"], "backup": info[0] if info else None}


def main():
    os.umask(0o077)
    try:
        # Auftrag ist genau eine JSON-Zeile; im Hintergrundmodus bleibt stdin
        # danach offen (Abbruchkanal), sonst folgt sofort EOF.
        a = json.loads(sys.stdin.readline())
        if a.get("background"):
            for sig in (signal.SIGTERM, signal.SIGHUP):
                signal.signal(sig, lambda s, f: request_cancel("signal"))
            t = threading.Thread(target=watch_stdin)
            t.daemon = True
            t.start()
        res = {"optimize": optimize, "list": list_backups, "restore": restore, "delete": delete}[a["op"]](a)
        res["ok"] = True
    except Bad as e:
        res = {"ok": False, "error": str(e)}
    except Exception as e:  # noqa
        res = {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:200])}
    try:
        sys.stdout.write(json.dumps(res) + "\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


main()
'''
_IMG_JPEGOPTIM = os.environ.get("IMAGE_JPEGOPTIM", "/usr/bin/jpegoptim").strip()
_IMG_OPTIPNG = os.environ.get("IMAGE_OPTIPNG", "/usr/bin/optipng").strip()
_IMG_TIMEOUT = int(os.environ.get("IMAGE_OPT_TIMEOUT", "300"))
_IMG_MAX_FILES = 2000
# Unter dem Tool-Timeout des MCP-Clients (60 s) bleiben: Budget + eine
# laufende Datei (je Werkzeug höchstens _IMG_TOOL_TIMEOUT) + SSH/DB-Overhead.
_IMG_TIME_BUDGET = int(os.environ.get("IMAGE_OPT_TIME_BUDGET", "30"))
_IMG_TOOL_TIMEOUT = int(os.environ.get("IMAGE_OPT_TOOL_TIMEOUT", "15"))
# Hintergrundmodus: kein Zeitbudget, dafür ein grosszügiges Limit pro Werkzeugaufruf
_IMG_BG_TOOL_TIMEOUT = int(os.environ.get("IMAGE_OPT_BG_TOOL_TIMEOUT", "300"))
_IMG_MAX_BYTES = 50 * 1024 * 1024
_IMG_MAX_DATEIEN = 200
_IMG_LIST_LIMIT = 50          # Listen im Vordergrund
_IMG_JOB_PAGE = 200           # Listen in image_job(action="status")
_IMG_JOB_PAGE_MAX = 500       # Maximum beim Blättern
_IMG_JOB_LISTS = ("groesste_ersparnisse", "uebersprungen_details", "fehler")
_IMG_MAX_JOBS = 4             # gleichzeitig laufende Hintergrundjobs (alle Domains)
_IMG_JOB_KEEP = 86400         # abgeschlossene Jobs bleiben 24 h abrufbar
_IMG_CANCEL_WAIT = 15
_IMG_RUN_ID_RE = re.compile(r"^\d{14}-[0-9a-f]{8}$")
_img_jobs: dict[str, dict[str, Any]] = {}
_img_jobs_lock = threading.Lock()


def _img_cmd(ctx: dict[str, Any]) -> str:
    return (
        f"cd {shlex.quote(ctx['base'])} && runuser -u {shlex.quote(ctx['login'])} -- "
        f"env -i HOME={shlex.quote(ctx['base'])} PATH=/usr/bin:/bin "
        f"{shlex.quote(_HELPER_PYTHON)} -I -c {shlex.quote(_IMG_WORKER_SRC)}"
    )


def _img_run(ctx: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Startet den Worker als System-User der Subscription; Fortschritt kommt
    als Punkte, das Ergebnis als letzte JSON-Zeile."""
    payload = json.dumps({**args, "base": ctx["base"]}).encode("utf-8")
    client = _ssh_connect()
    try:
        out, err, rc = _client_exec(client, _img_cmd(ctx), timeout=_IMG_TIMEOUT, stdin_data=payload)
    finally:
        client.close()
    for line in reversed(out.strip().splitlines()):
        try:
            res = json.loads(line)
        except ValueError:
            continue
        if isinstance(res, dict) and "ok" in res:
            if not res["ok"]:
                raise _UserError(f"Bildoptimierung abgebrochen: {res.get('error')}")
            return res
    raise SSHError(f"Bildoptimierung: keine Antwort vom Worker (Exit-Code {rc}): {(err.strip() or out.strip())[-300:]}")


def _img_progress(job: dict[str, Any], p: Any) -> None:
    """Fortschrittszeile des Workers übernehmen. Der Worker läuft als
    Subscription-User, die Werte werden deshalb wie Fremddaten geprüft."""
    if not isinstance(p, dict):
        return
    prog = job["fortschritt"]
    for k in ("bearbeitet", "gesamt", "optimiert", "ersparnis_bytes", "uebersprungen", "fehler"):
        v = p.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10 ** 12:
            prog[k] = v
    d = p.get("datei")
    if isinstance(d, str):
        prog["aktuelle_datei"] = d[:300]


def _img_consume(job: dict[str, Any], lines: Any) -> dict[str, Any] | None:
    """Liest die Ausgabe des Hintergrund-Workers zeilenweise: Fortschritt in
    den Job, Rückgabe ist die Ergebniszeile (None, wenn keine kam)."""
    res = None
    for raw in lines:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        if len(raw) > 4 * 1024 * 1024:
            continue
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if "fortschritt" in obj:
            _img_progress(job, obj["fortschritt"])
        elif "ok" in obj:
            res = obj
    return res


def _img_run_bg(ctx: dict[str, Any], args: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """Wie _img_run, aber die SSH-Sitzung bleibt für die ganze Laufzeit offen:
    stdin ist der Abbruchkanal ("cancel"), stdout liefert Fortschrittszeilen."""
    payload = json.dumps({**args, "base": ctx["base"]}).encode("utf-8") + b"\n"
    client = _ssh_connect()
    try:
        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(30)
        timeout = max(_IMG_TIMEOUT, _IMG_BG_TOOL_TIMEOUT + 60)
        stdin, stdout, stderr = client.exec_command(_img_cmd(ctx), timeout=timeout)
        stdin.write(payload)
        stdin.flush()

        def cancel() -> None:
            try:
                stdin.write(b"cancel\n")
                stdin.flush()
            except (OSError, EOFError):
                pass

        job["_cancel"] = cancel
        if job["_cancel_requested"]:
            cancel()
        try:
            res = _img_consume(job, stdout)
        except socket.timeout:
            raise SSHError(f"Bildoptimierung: keine Ausgabe vom Worker seit {timeout} s - Job abgebrochen.")
        rc = stdout.channel.recv_exit_status()
        err = stderr.read().decode("utf-8", "replace")
    finally:
        job["_cancel"] = None
        client.close()
    if res is None:
        raise SSHError(f"Bildoptimierung: keine Antwort vom Worker (Exit-Code {rc}): {err.strip()[-300:]}")
    if not res["ok"]:
        raise _UserError(f"Bildoptimierung abgebrochen: {res.get('error')}")
    return res


def _img_ctx_and_parts(domain: str, path: str) -> tuple[dict[str, Any], list[str]]:
    """Leerer path = Docroot der Hauptdomain; sonst muss path ein Docroot oder
    darunter sein."""
    ctx = _vhost_ctx(domain)
    if not (path or "").strip():
        main = ctx.get("main_docroot") or ctx["docroots"][0]
        return ctx, list(main)
    try:
        parts = _strict_parts(path)
    except ValueError as e:
        raise _UserError(str(e))
    if parts not in ctx["docroots"] and _docroot_of(parts, ctx["docroots"]) is None:
        raise _UserError(
            f"'{path}' liegt in keinem Docroot der Subscription - nur Bilder in Webordnern werden "
            f"optimiert. Gültige Docroots: {', '.join('/'.join(d) for d in ctx['docroots'])}."
        )
    return ctx, parts


def _img_dateien_parts(ctx: dict[str, Any], dateien: Any) -> list[list[str]]:
    """Parameter dateien: jede Angabe so streng wie path (relativ, kein "..",
    keine Wildcards, in einem Docroot); ein ungültiger Eintrag lehnt den
    ganzen Aufruf ab. Symlinks prüft der Worker pro Pfadteil."""
    if not isinstance(dateien, list) or not all(isinstance(d, str) for d in dateien):
        raise _UserError("dateien muss eine Liste relativer Pfade sein (z.B. [\"httpdocs/bild.jpg\"]).")
    if not dateien:
        raise _UserError("dateien ist leer.")
    if len(dateien) > _IMG_MAX_DATEIEN:
        raise _UserError(f"dateien: höchstens {_IMG_MAX_DATEIEN} Einträge pro Aufruf ({len(dateien)} angegeben).")
    out: list[list[str]] = []
    seen: set[str] = set()
    for d in dateien:
        try:
            parts = _strict_parts(d)
        except ValueError as e:
            raise _UserError(f"dateien: {e}")
        if _docroot_of(parts, ctx["docroots"]) is None:
            raise _UserError(
                f"dateien: '{d}' liegt in keinem Docroot der Subscription - nur Bilder in Webordnern werden "
                f"optimiert. Gültige Docroots: {', '.join('/'.join(x) for x in ctx['docroots'])}.")
        rel = "/".join(parts)
        if rel not in seen:
            seen.add(rel)
            out.append(parts)
    return out


def _img_format(ctx: dict[str, Any], pfad: str, dry_run: bool, res: dict[str, Any], run_id: str,
                hintergrund: bool) -> dict[str, Any]:
    """Ergebnis des Workers in die Rückgabe von optimize_images bzw. image_job."""
    saved = res["bytes_vorher"] - res["bytes_nachher"]
    out: dict[str, Any] = {
        "domain": ctx["domain"],
        "pfad": pfad,
        "modus": "Probelauf - nichts geändert" if dry_run else "ausgeführt",
        "bilder_geprueft": res["geprueft"],
        "bilder_optimiert": res["optimiert"],
        "ersparnis_bytes": saved,
        "ersparnis_mb": round(saved / 1048576, 2),
        "ersparnis_prozent": round(100 * saved / res["bytes_vorher"], 1) if res["bytes_vorher"] else 0,
        "groesste_ersparnisse": res["dateien"],
        "uebersprungen": res["uebersprungen"],
    }
    if res["fehler"]:
        out["fehler"] = res["fehler"]
    if res.get("details"):
        out["uebersprungen_details"] = res["details"]
    if res.get("ohne_schreibrechte"):
        out["ohne_schreibrechte"] = res["ohne_schreibrechte"]
        out["schreibrechte_hinweis"] = (
            "In diesen Ordnern darf der System-User der Subscription nicht schreiben - die Bilder werden "
            + ("auch im echten Lauf übersprungen" if dry_run else "übersprungen")
            + ". Besitzer bzw. Rechte des Ordners korrigieren und erneut aufrufen.")
    if res.get("verwaiste_temp_dateien"):
        out["verwaiste_temp_dateien"] = res["verwaiste_temp_dateien"]
    if res.get("backup"):
        out["backup_run_id"] = run_id
        out["backup_hinweis"] = ("Originale unter " + res["backup"] + " - Speicher wird erst frei, wenn das "
                                 "Backup mit image_backups(action=\"delete\") gelöscht ist.")
    # Listen sind gekürzt? Immer die Gesamtzahl nennen (Vordergrund: 50 pro
    # Liste; Hintergrund: image_job blättert mit liste/ab/limit).
    anzahl = res.get("anzahl") or {}
    gesamt = {"groesste_ersparnisse": anzahl.get("dateien", len(res["dateien"])),
              "uebersprungen_details": anzahl.get("details", len(res.get("details") or [])),
              "fehler": anzahl.get("fehler", len(res["fehler"]))}
    for k in ("ohne_schreibrechte", "verwaiste_temp_dateien"):
        if res.get(k):
            gesamt[k] = anzahl.get(k, len(res[k]))
    out["listen_gesamt"] = gesamt
    out["gekuerzt"] = any(n > len(out.get(k) or []) for k, n in gesamt.items())
    if out["gekuerzt"] and not hintergrund:
        out["gekuerzt_hinweis"] = (f"Listen auf {_IMG_LIST_LIMIT} Einträge gekürzt (Gesamtzahl unter listen_gesamt) - "
                                   "vollständige Listen liefert ein Lauf mit hintergrund=true über image_job.")
    if hintergrund:
        out["unvollstaendig"] = bool(res.get("abgebrochen"))
        if res.get("abgebrochen"):
            out["abgebrochen"] = res["abgebrochen"]
            out["hinweis"] = ("Job abgebrochen (" + str(res["abgebrochen"]) + ") - bereits ersetzte Bilder bleiben "
                              "optimiert, die laufende Datei wurde verworfen, "
                              f"{res['uebersprungen'].get('abgebrochen_rest', 0)} Dateien nicht mehr bearbeitet.")
        if res.get("prioritaet"):
            out["prioritaet"] = res["prioritaet"]
        if res.get("laufzeit_s") is not None:
            out["laufzeit_s"] = res["laufzeit_s"]
        return out
    out["unvollstaendig"] = bool(res.get("fortsetzen_ab") or res["uebersprungen"].get("zeitbudget_rest")
                                 or res["uebersprungen"].get("limit_max_files"))
    if res.get("fortsetzen_ab"):
        out["weiter_ab"] = res["fortsetzen_ab"]
        grund = "Zeitbudget" if res["uebersprungen"].get("zeitbudget_rest") else "max_files"
        out["hinweis"] = (f"{grund} erreicht - mit weiter_ab=\"{res['fortsetzen_ab']}\" und sonst gleichen "
                          "Parametern erneut aufrufen.")
    elif res["uebersprungen"].get("zeitbudget_rest"):
        out["hinweis"] = ("Zeitbudget schon beim Einlesen aufgebraucht (sehr viele Dateien) - einen "
                          "Unterordner angeben, z.B. einen Jahresordner unter wp-content/uploads.")
    if res["uebersprungen"].get("timeout"):
        timeouts = [d["datei"] for d in (res.get("details") or []) if d.get("grund") == "timeout"]
        n = res["uebersprungen"]["timeout"]
        out["timeout_hinweis"] = (
            f"{n} Datei(en) im Timeout ({_IMG_TOOL_TIMEOUT} s pro Werkzeugaufruf) - erneut versuchen mit "
            f"hintergrund=true und dateien={json.dumps(timeouts, ensure_ascii=False)}"
            + (" (Liste gekürzt, weitere unter uebersprungen_details)" if len(timeouts) < n else "")
            + f"; im Hintergrund gilt {_IMG_BG_TOOL_TIMEOUT} s pro Datei.")
    return out


def _img_jobs_gc(now: float) -> None:
    for jid, job in list(_img_jobs.items()):
        if job["ended"] is not None and now - job["ended"] > _IMG_JOB_KEEP:
            del _img_jobs[jid]


def _img_job_thread(job: dict[str, Any], ctx: dict[str, Any], args: dict[str, Any],
                    lock: threading.Lock) -> None:
    """Hintergrundjob: hält die Domain-Sperre bis zum Ende, Ergebnis bleibt
    im Speicher (image_job)."""
    try:
        res = _img_run_bg(ctx, args, job)
        out = _img_format(ctx, job["pfad"], job["dry_run"], res, job["job_id"], hintergrund=True)
        job["result"] = out
        job["status"] = "abgebrochen" if res.get("abgebrochen") else "fertig"
        if not job["dry_run"]:
            _audit("image_job_done", domain=ctx["domain"], path=job["pfad"], job_id=job["job_id"],
                   status=job["status"], optimized=res["optimiert"], saved=out["ersparnis_bytes"],
                   errors=len(res["fehler"]), aborted=res.get("abgebrochen"))
    except Exception as e:  # noqa - Fehler gehören in den Jobstatus, nicht in den Thread
        job["status"] = "fehler"
        job["fehler_meldung"] = str(e)[:500] or type(e).__name__
        _audit("image_job_failed", domain=ctx["domain"], path=job["pfad"], job_id=job["job_id"],
               error=job["fehler_meldung"][:200])
    finally:
        job["ended"] = time.time()
        job["_cancel"] = None
        lock.release()
        job["_done"].set()


@mcp.tool()
def optimize_images(
    domain: str,
    path: str = "",
    dry_run: bool = True,
    confirm: bool = False,
    max_files: int = 500,
    recursive: bool = True,
    keep_backup: bool = True,
    weiter_ab: str = "",
    hintergrund: bool = False,
    dateien: list[str] | None = None,
) -> str:
    """Optimiert JPG- und PNG-Bilder einer Domain verlustfrei (gleiche Pixel)
    für weniger Speicher und schnellere Seiten. domain: Hauptdomain der
    Subscription; path: Ordner oder Datei relativ zu /var/www/vhosts/<domain>/,
    muss in einem Docroot liegen (z.B. "httpdocs/wp-content/uploads"). Leer
    (Standard) = Docroot der Hauptdomain; die Rückgabe nennt ihn unter "pfad".
    Alternativ dateien=[...]: Liste einzelner Bilder (relative Pfade wie path,
    max. 200, path dann leer), z.B. die Timeout-Dateien eines früheren Laufs;
    nicht gefundene oder nicht-Bild-Einträge stehen unter uebersprungen_details.

    Pro Bild: GPS-Daten, Kamera-/Objektiv-Seriennummern, Besitzername und
    Herstellerdaten (MakerNote) aus EXIF/XMP entfernen (Datenschutz);
    Farbprofil, Ausrichtung und übrige Metadaten bleiben. Danach jpegoptim
    (nur verlustfreie Huffman-Optimierung, keine Qualitätsstufe) bzw.
    optipng -o2 (PNGs ab 1 MB: -o1, schneller). Ersetzt wird nur, wenn die Datei kleiner wird oder
    Datenschutzdaten entfernt wurden; Änderungsdatum und Rechte bleiben.
    Läuft als System-User der Subscription (nie root), folgt keinen
    Symlinks, überspringt Hardlinks, Dateien > 50 MB und den Papierkorb.
    Ebenfalls unter "uebersprungen" (nicht "fehler"), mit Datei und Meldung
    unter "uebersprungen_details": format_passt_nicht_zur_endung (z.B. JPEG
    mit Endung .png), werkzeug_abgelehnt (defekte/abgeschnittene Datei,
    Meldung mit Werkzeug und Exit-Code), timeout, keine_schreibrechte (Ordner
    und Besitzer unter "ohne_schreibrechte", auch im Probelauf).
    Verwaiste Arbeitskopien früherer Läufe (".<name>.mcp-xxxxxxxx.<ext>[.bak]")
    im Zielpfad stehen unter "verwaiste_temp_dateien"; der echte Lauf löscht
    sie, wenn sie älter als 1 Stunde sind.

    dry_run=true (Standard): rechnet die Ersparnis aus, ändert nichts und
    legt in den Webordnern nichts an (Arbeitskopien unter .plesk-mcp/tmp).
    Zum Ausführen dry_run=false UND confirm=true. Mit keep_backup=true
    (Standard) bleiben die Originale per Hardlink unter
    .plesk-mcp/image-backups/<run_id>/ erhalten (ausserhalb des Webroots) -
    der Speicher wird erst frei, wenn das Backup mit image_backups gelöscht
    wird. Max. max_files Bilder pro Aufruf (Standard 500, höchstens 2000).

    Auch der Probelauf führt jpegoptim und optipng tatsächlich aus (auf
    Kopien) und braucht deshalb ähnlich viel Zeit wie ein echter Lauf.
    Ein Aufruf dauert höchstens etwa 30 Sekunden Bearbeitung (Zeitbudget,
    IMAGE_OPT_TIME_BUDGET) plus die Datei, die gerade läuft (jpegoptim/optipng
    je höchstens 15 s, sonst unter uebersprungen.timeout; "timeout_hinweis"
    nennt die Dateien für einen erneuten Versuch im Hintergrund). Ist danach
    noch nicht alles bearbeitet, enthält die Rückgabe "unvollstaendig": true und
    "weiter_ab" - mit weiter_ab=<dieser Wert> und sonst gleichen Parametern
    erneut aufrufen, bis "unvollstaendig": false. Bilder werden in fester,
    alphabetischer Pfad-Reihenfolge bearbeitet, jedes genau einmal; echte
    Teilläufe bekommen je eine eigene backup_run_id. Die Listen
    groesste_ersparnisse, uebersprungen_details und fehler zeigen höchstens
    50 Einträge; "listen_gesamt" nennt die Gesamtzahl, "gekuerzt" ob gekürzt.

    hintergrund=true: kehrt sofort mit "job_id" zurück, der
    ganze Pfad läuft als EIN Job ohne Zeitbudget, ohne max_files und
    weiter_ab, mit EINEM Backup (backup_run_id = job_id) und 300 s pro
    Werkzeugaufruf (IMAGE_OPT_BG_TOOL_TIMEOUT); jpegoptim/optipng laufen mit
    nice/ionice. Fortschritt, Ergebnis (gleiches Format, Listen blätterbar)
    und Abbruch über image_job(domain, action="status"|"cancel", job_id).
    Solange der Job läuft, sind weitere Bildoptimierungen dieser Domain
    gesperrt. Nach einem Neustart des MCP-Servers ist der Job unbekannt;
    das Backup steht dann weiter unter image_backups(action="list").
    """
    if not dry_run and not confirm:
        raise _UserError("Zum Ausführen dry_run=false UND confirm=true setzen - ändert Dateien auf dem "
                         "Produktivserver.")
    if hintergrund and (weiter_ab or "").strip():
        raise _UserError("weiter_ab gilt nur im Vordergrund - ein Hintergrundjob bearbeitet den ganzen Pfad.")
    if not hintergrund and (not isinstance(max_files, int) or not 1 <= max_files <= _IMG_MAX_FILES):
        raise _UserError(f"max_files muss zwischen 1 und {_IMG_MAX_FILES} liegen.")
    if dateien is not None:
        if (path or "").strip():
            raise _UserError("Entweder path oder dateien angeben, nicht beides.")
        ctx = _vhost_ctx(domain)
        parts_list = _img_dateien_parts(ctx, dateien)
        parts: list[str] = []
        pfad = f"dateien ({len(parts_list)} Einträge)"
    else:
        ctx, parts = _img_ctx_and_parts(domain, path)
        parts_list = []
        pfad = "/".join(parts)
    run_id = f"{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}-{os.urandom(4).hex()}"
    args: dict[str, Any] = {"op": "optimize", "parts": parts, "dateien": parts_list, "dry_run": bool(dry_run),
                            "recursive": bool(recursive), "keep_backup": bool(keep_backup),
                            "max_bytes": _IMG_MAX_BYTES, "run_id": run_id,
                            "jpegoptim": _IMG_JPEGOPTIM, "optipng": _IMG_OPTIPNG}
    if hintergrund:
        args.update({"background": True, "max_files": 0, "start_after": "", "time_budget": None,
                     "tool_timeout": _IMG_BG_TOOL_TIMEOUT, "list_limit": 0})
        with _img_jobs_lock:
            _img_jobs_gc(time.time())
            running = sum(1 for j in _img_jobs.values() if j["ended"] is None)
            if running >= _IMG_MAX_JOBS:
                raise _UserError(f"Es laufen bereits {running} Hintergrundjobs - bitte warten, bis einer fertig ist "
                                 "(image_job).")
            lock = _phps_lock(f"img:{ctx['domain']}", ctx["domain"], "eine Bildoptimierung")
            job: dict[str, Any] = {
                "job_id": run_id, "domain": ctx["domain"], "pfad": pfad, "dry_run": bool(dry_run),
                "status": "laeuft", "started": time.time(), "ended": None,
                "fortschritt": {"bearbeitet": 0, "gesamt": None, "aktuelle_datei": None, "optimiert": 0,
                                "ersparnis_bytes": 0, "uebersprungen": 0, "fehler": 0},
                "result": None, "fehler_meldung": None,
                "_cancel": None, "_cancel_requested": False, "_done": threading.Event(),
            }
            _img_jobs[run_id] = job
        _audit("image_job_start", domain=ctx["domain"], path=pfad, job_id=run_id, dry_run=bool(dry_run))
        threading.Thread(target=_img_job_thread, args=(job, ctx, args, lock), daemon=True).start()
        return _json({
            "domain": ctx["domain"], "pfad": pfad, "job_id": run_id, "status": "laeuft",
            "modus": "Hintergrund - Probelauf, nichts wird geändert" if dry_run else "Hintergrund - ausgeführt",
            "hinweis": (f"Läuft im Hintergrund. Status: image_job(domain=\"{ctx['domain']}\", action=\"status\", "
                        f"job_id=\"{run_id}\"); abbrechen: action=\"cancel\" mit confirm=true. "
                        + ("Backup des ganzen Laufs unter backup_run_id = job_id. " if not dry_run and keep_backup else "")
                        + "Bis zum Ende sind weitere Bildoptimierungen dieser Domain gesperrt."),
        })
    args.update({"max_files": max_files, "start_after": (weiter_ab or "").strip(),
                 "time_budget": _IMG_TIME_BUDGET, "tool_timeout": _IMG_TOOL_TIMEOUT, "list_limit": _IMG_LIST_LIMIT})
    lock = _phps_lock(f"img:{ctx['domain']}", ctx["domain"], "eine Bildoptimierung")
    try:
        res = _img_run(ctx, args)
    finally:
        lock.release()
    out = _img_format(ctx, pfad, dry_run, res, run_id, hintergrund=False)
    if not dry_run:
        _audit("optimize_images", domain=ctx["domain"], path=pfad, run_id=run_id,
               optimized=res["optimiert"], saved=out["ersparnis_bytes"], errors=len(res["fehler"]))
    return _json(out)


def _img_job_view(job: dict[str, Any], liste: str, ab: int, limit: int) -> dict[str, Any]:
    now = time.time()
    out: dict[str, Any] = {
        "job_id": job["job_id"], "domain": job["domain"], "pfad": job["pfad"], "status": job["status"],
        "modus": "Probelauf - nichts geändert" if job["dry_run"] else "ausgeführt",
        "gestartet": datetime.datetime.fromtimestamp(job["started"]).isoformat(timespec="seconds"),
        "laufzeit_s": round((job["ended"] or now) - job["started"], 1),
    }
    if job["status"] == "laeuft":
        out["fortschritt"] = dict(job["fortschritt"])
        if job["_cancel_requested"]:
            out["fortschritt"]["abbruch_angefordert"] = True
        return out
    if job["status"] == "fehler":
        out["fehler_meldung"] = job["fehler_meldung"]
        out["hinweis"] = ("Job mit Fehler beendet - bereits ersetzte Bilder bleiben optimiert; vorhandene Backups "
                          "mit image_backups(action=\"list\") prüfen.")
        return out
    res = job["result"]
    if liste:
        items = res.get(liste) or []
        page = items[ab:ab + limit]
        out.update({"liste": liste, "ab": ab, "limit": limit, "gesamt": len(items), "eintraege": page,
                    "weitere": ab + len(page) < len(items)})
        if ab + len(page) < len(items):
            out["hinweis"] = f"Weitere Einträge mit ab={ab + len(page)}."
        return out
    res = dict(res)
    cut = {}
    for k in _IMG_JOB_LISTS:
        items = res.get(k)
        if items and len(items) > _IMG_JOB_PAGE:
            res[k] = items[:_IMG_JOB_PAGE]
            cut[k] = len(items)
    res["gekuerzt"] = bool(cut)
    if cut:
        res["gekuerzt_hinweis"] = (f"Listen auf {_IMG_JOB_PAGE} Einträge gekürzt ({', '.join(f'{k}: {v}' for k, v in cut.items())}) - "
                                   f"blättern mit liste=\"<name>\", ab=<n>, limit=<1..{_IMG_JOB_PAGE_MAX}>.")
    out.update(res)
    return out


@mcp.tool()
def image_job(domain: str, action: str = "status", job_id: str = "", confirm: bool = False,
              liste: str = "", ab: int = 0, limit: int = 200) -> str:
    """Status und Abbruch eines Hintergrundjobs von optimize_images(hintergrund=true).
    action="status": während des Laufs "fortschritt" (bearbeitet/gesamt,
    aktuelle_datei, optimiert, ersparnis_bytes, uebersprungen, fehler) und
    laufzeit_s; nach Abschluss ("fertig", "abgebrochen" oder "fehler") dasselbe
    Ergebnis wie optimize_images. Die Listen groesste_ersparnisse,
    uebersprungen_details und fehler zeigen die ersten 200 Einträge
    ("gekuerzt": true, Gesamtzahl unter listen_gesamt); blättern mit
    liste="<name>", ab=<Start>, limit=<1..500>.
    action="cancel": ohne confirm=true nur Vorschau. Mit confirm=true wird der
    Job sauber beendet: das laufende Werkzeug wird gestoppt, die Arbeitskopie
    verworfen, bereits ersetzte Bilder bleiben optimiert (Backup unter
    backup_run_id = job_id). Jobs bleiben 24 h abrufbar; nach einem Neustart
    des MCP-Servers sind sie unbekannt - Backups dann über image_backups.
    """
    if action not in ("status", "cancel"):
        raise _UserError("action muss 'status' oder 'cancel' sein.")
    jid = (job_id or "").strip()
    if not _IMG_RUN_ID_RE.match(jid):
        raise _UserError(f"Ungültige job_id {job_id!r} (Format wie 20260101120000-1a2b3c4d).")
    if liste and liste not in _IMG_JOB_LISTS:
        raise _UserError(f"liste muss eine von {', '.join(_IMG_JOB_LISTS)} sein.")
    if not isinstance(ab, int) or isinstance(ab, bool) or ab < 0:
        raise _UserError("ab muss eine Zahl >= 0 sein.")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _IMG_JOB_PAGE_MAX:
        raise _UserError(f"limit muss zwischen 1 und {_IMG_JOB_PAGE_MAX} liegen.")
    ctx = _vhost_ctx(domain)
    with _img_jobs_lock:
        _img_jobs_gc(time.time())
        job = _img_jobs.get(jid)
    if job is None or job["domain"] != ctx["domain"]:
        raise _UserError(f"Unbekannte job_id {jid!r} für {ctx['domain']} - älter als 24 Stunden oder der MCP-Server "
                         "wurde neu gestartet (der Job ist dann beendet; bereits ersetzte Bilder bleiben optimiert). "
                         "Vorhandene Backups: image_backups(action=\"list\").")
    if action == "status":
        return _json(_img_job_view(job, liste, ab, limit))
    if job["status"] != "laeuft":
        raise _UserError(f"Job {jid} läuft nicht mehr (Status: {job['status']}) - nichts abzubrechen.")
    if not confirm:
        out = _img_job_view(job, "", 0, limit)
        out["modus"] = "Vorschau - Job läuft weiter"
        out["hinweis"] = ("Mit confirm=true abbrechen: das laufende Werkzeug wird gestoppt und die Arbeitskopie "
                          "verworfen; bereits ersetzte Bilder bleiben optimiert"
                          + ("" if job["dry_run"] else " (Backup unter backup_run_id = job_id)") + ".")
        return _json(out)
    job["_cancel_requested"] = True
    cancel = job["_cancel"]
    if cancel is not None:
        cancel()
    _audit("image_job_cancel", domain=ctx["domain"], path=job["pfad"], job_id=jid)
    job["_done"].wait(_IMG_CANCEL_WAIT)
    out = _img_job_view(job, "", 0, limit)
    out["modus"] = "Abbruch ausgeführt" if job["status"] != "laeuft" else "Abbruch angefordert"
    if job["status"] == "laeuft":
        out["hinweis"] = (f"Der Worker hat den Abbruch nach {_IMG_CANCEL_WAIT} s noch nicht bestätigt (die laufende "
                          "Datei wird beendet) - Status erneut abfragen.")
    return _json(out)


@mcp.tool()
def image_backups(domain: str, action: str = "list", run_id: str = "", confirm: bool = False) -> str:
    """Verwaltet die Backups von optimize_images einer Domain.
    action="list": vorhandene Backups mit Anzahl Dateien und belegtem Speicher.
    action="restore": stellt die Originale eines Laufs (run_id) wieder her und
    entfernt danach das Backup. action="delete": löscht das Backup eines
    Laufs endgültig (gibt den Speicher frei). restore/delete ohne
    confirm=true nur Vorschau.
    """
    if action not in ("list", "restore", "delete"):
        raise _UserError("action muss 'list', 'restore' oder 'delete' sein.")
    ctx = _vhost_ctx(domain)
    if action == "list":
        return _json({"domain": ctx["domain"], **{k: v for k, v in _img_run(ctx, {"op": "list"}).items()
                                                  if k != "ok"}})
    if not _IMG_RUN_ID_RE.match((run_id or "").strip()):
        raise _UserError(f"Ungültige run_id {run_id!r} (siehe action=\"list\").")
    lock = _phps_lock(f"img:{ctx['domain']}", ctx["domain"], "eine Bildoptimierung")
    try:
        res = _img_run(ctx, {"op": action, "run_id": run_id.strip(), "dry_run": not confirm})
    finally:
        lock.release()
    res.pop("ok", None)
    if confirm:
        _audit("image_backups", domain=ctx["domain"], op=action, run_id=run_id.strip(),
               result="failed" if res.get("fehler") else "ok")
    return _json({"domain": ctx["domain"], "action": action, "run_id": run_id.strip(),
                  "modus": "ausgeführt" if confirm else "Vorschau - nichts geändert", **res})


# ---------------------------------------------------------------------------
# Imunify360 - Malware-Treffer und Malware-Ignore-Liste
#
# Aufbau der Kommandos gemäss den CLI-Schemas von imunify360-agent
# (imav/malwarelib/rpc/schema/ignore.yaml + malicious.yaml auf dem Server):
#   malware malicious list [--search S] [--by-status ST ...] [--limit N] [--offset N]
#   malware ignore list    [--search S] [--limit N] [--offset N]
#   malware ignore add     PATH ...   (absolute Pfade, keine Wildcards)
#   malware ignore delete  ID ...     (nur IDs aus "malware ignore list")
# Mit --json gibt die CLI bei Erfolg das data-Objekt ({"items": ...}) aus,
# bei Fehlern {"error"|"warnings": <messages>} mit Exit-Code 11 bzw. 3.
#
# imunify360-agent ist bewusst NICHT in run_diagnostic freigegeben - nur
# diese fest verdrahteten Unterbefehle sind erreichbar.
# ---------------------------------------------------------------------------

_IMUNIFY_BIN = "/usr/bin/imunify360-agent"

# Erlaubte Zeichen in Imunify-Pfaden: bewusst konservativ (keine Leerzeichen,
# keine Shell-Metazeichen, keine Wildcards - die CLI unterstützt laut Schema
# ohnehin nur absolute Pfade ohne Glob-Muster).
_IMUNIFY_PATH_CHARS_RE = re.compile(r"^[A-Za-z0-9._/+@=,~-]+$")

_IMUNIFY_MALICIOUS_STATUSES = {
    "found", "cleanup_pending", "cleanup_started", "cleanup_done",
    "cleanup_removed", "cleanup_requires_myimunify_protection",
    "cleanup_restore_pending", "cleanup_restore_started",
    "restore_from_backup_started", "restored_from_backup",
}


def _imunify_search_term(term: str, name: str) -> str:
    """Suchbegriff für --search: gleiche Zeichen-Whitelist wie Pfade, und
    kein führendes "-" (würde die CLI sonst als Option interpretieren)."""
    if not _IMUNIFY_PATH_CHARS_RE.match(term) or term.startswith("-"):
        raise ValueError(f"{name} {term!r} enthält unerlaubte Zeichen.")
    return term


def _imunify_validate_path(path: str) -> str:
    """Statische Prüfung eines Pfads für die Imunify-Ignore-Liste (ohne
    Serverzugriff, daher separat testbar):
    - absolut und unterhalb von /var/www/vhosts/<domain>/ (mind. eine Ebene
      unter dem Domain-Verzeichnis - nie ganze Domains oder vhosts selbst)
    - bereits normalisiert (kein "..", ".", "//", kein abschliessendes "/")
    - nur unkritische Zeichen (keine Leerzeichen, Shell-Metazeichen, Wildcards)
    Gibt den unveränderten Pfad zurück oder wirft ValueError.
    """
    if not isinstance(path, str) or not path:
        raise ValueError("path darf nicht leer sein.")
    if not _IMUNIFY_PATH_CHARS_RE.match(path):
        raise ValueError(
            f"Pfad {path!r} enthält unerlaubte Zeichen (erlaubt: A-Z a-z 0-9 "
            "und . _ / + @ = , ~ -; keine Leerzeichen, Wildcards oder "
            "Shell-Sonderzeichen)."
        )
    if not path.startswith("/"):
        raise ValueError(f"Pfad {path!r} muss absolut sein.")
    if posixpath.normpath(path) != path or "//" in path:
        raise ValueError(
            f"Pfad {path!r} ist nicht normalisiert ('..', '.', '//' oder "
            "abschliessendes '/' sind nicht erlaubt)."
        )
    if not path.startswith(_VHOST_BASE + "/"):
        raise ValueError(
            f"Pfad {path!r} liegt nicht unter {_VHOST_BASE}/ - nicht erlaubt."
        )
    parts = path[len(_VHOST_BASE) + 1:].split("/")
    # /var/www/vhosts/<domain>/<x>  bzw.  /var/www/vhosts/system/<domain>/<x>
    min_parts = 3 if parts[0] == "system" else 2
    if len(parts) < min_parts:
        raise ValueError(
            f"Pfad {path!r} ist zu allgemein - es muss eine Datei bzw. ein "
            "Pfad innerhalb eines Domain-Verzeichnisses angegeben werden."
        )
    return path


def _imunify_check_on_server(path: str, must_exist: bool) -> None:
    """Prüft per SFTP auf dem Server, dass der Pfad keine Symlink-Tricks
    enthält: Das Ziel selbst darf kein Symlink sein, und der kanonische Pfad
    (alle Symlinks in Elternverzeichnissen aufgelöst) muss exakt dem
    angegebenen Pfad entsprechen. Existiert der Pfad nicht, wird bei
    must_exist=True abgebrochen, sonst nichts weiter geprüft.
    """
    client = _ssh_connect()
    try:
        sftp = client.open_sftp()
        try:
            try:
                lst = sftp.lstat(path)
            except FileNotFoundError:
                if must_exist:
                    raise ValueError(f"'{path}' existiert auf dem Server nicht.")
                return
            if stat.S_ISLNK(lst.st_mode or 0):
                raise ValueError(
                    f"'{path}' ist ein Symlink - aus Sicherheitsgründen nicht erlaubt."
                )
            real = sftp.normalize(path)
        finally:
            sftp.close()
    finally:
        client.close()
    if real != path:
        raise ValueError(
            f"'{path}' zeigt über einen Symlink auf '{real}'. Bitte den "
            "kanonischen Pfad verwenden (dieser wird ebenfalls geprüft)."
        )


def _imunify_run(args: list[str]) -> dict[str, Any]:
    """Führt imunify360-agent mit fest vorgegebenen Argumenten aus. Die
    Argumente werden als Liste übergeben und einzeln per shlex.join
    gequotet (SSH exec kennt nur einen Kommando-String, eine Liste wird so
    ohne Shell-Interpretation der Einzelwerte übertragen). Liefert
    {"ok": True, "data": ...} oder {"ok": False, "error": ..., ...}.
    """
    argv = [_IMUNIFY_BIN, *args, "--json"]
    out, err, code = _ssh_exec(shlex.join(argv), timeout=max(_SSH_TIMEOUT, 60))
    parsed: Any = None
    if out.strip():
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError:
            parsed = None

    if code == 0 and isinstance(parsed, dict):
        return {"ok": True, "data": parsed}

    if isinstance(parsed, dict) and ("error" in parsed or "warnings" in parsed):
        msg = parsed.get("error", parsed.get("warnings"))
    elif isinstance(parsed, dict) and isinstance(parsed.get("items"), str):
        msg = parsed["items"]  # z.B. Socket-Fehler: {"items": "ERROR: ..."}
    else:
        msg = (err.strip() or out.strip() or "unbekannter Fehler")
    return {
        "ok": False,
        "error": msg,
        "exit_code": code,
        "command": " ".join(argv[1:]),
        "stderr": err.strip() or None,
    }


def _ts_iso(ts: Any) -> str | None:
    try:
        return datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


@mcp.tool()
def imunify_malware_list(
    domain: str = "",
    path: str = "",
    status: str = "",
    limit: int = 50,
    offset: int = 0,
) -> str:
    """Listet die aktuellen Malware-Treffer von Imunify360 auf
    (imunify360-agent malware malicious list) - read-only.
    Rückgabe als JSON: id, file, created (Unix + ISO), type (Signatur),
    status, username, scan_type, resource_type.

    domain: nur Treffer unter /var/www/vhosts/<domain>/ (Suche über Pfad).
    path: Suchbegriff für den Dateipfad (Teilstring, hat Vorrang vor domain).
    status: optional, kommagetrennt, z.B. "found" oder
    "found,cleanup_done" (Werte laut CLI-Hilfe).
    limit/offset: Paging (Standard 50/0, max. limit 500).
    """
    args = ["malware", "malicious", "list"]
    search = ""
    if path:
        search = _imunify_search_term(path, "path")
    elif domain:
        d = _imunify_search_term(_domain_arg(domain), "domain")
        search = f"{_VHOST_BASE}/{d}/"
    if search:
        args += ["--search", search]
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        bad = [s for s in statuses if s not in _IMUNIFY_MALICIOUS_STATUSES]
        if bad:
            raise ValueError(
                f"Unbekannter status {bad}. Erlaubt: "
                f"{', '.join(sorted(_IMUNIFY_MALICIOUS_STATUSES))}"
            )
        args += ["--by-status", *statuses]
    args += ["--limit", str(max(1, min(int(limit), 500))), "--offset", str(max(0, int(offset)))]

    res = _imunify_run(args)
    if not res["ok"]:
        return _json(res)
    data = res["data"]
    items = [
        {
            "id": it.get("id"),
            "file": it.get("file"),
            "created": it.get("created"),
            "created_iso": _ts_iso(it.get("created")),
            "type": it.get("type"),
            "status": it.get("status"),
            "username": it.get("username"),
            "scan_type": it.get("scan_type"),
            "resource_type": it.get("resource_type"),
        }
        for it in (data.get("items") or [])
    ]
    return _json({
        "ok": True,
        "total": data.get("max_count"),
        "malicious_count": data.get("malicious_count"),
        "returned": len(items),
        "offset": int(offset),
        "items": items,
    })


def _imunify_ignore_entries(search: str, limit: int = 500, offset: int = 0) -> dict[str, Any]:
    args = ["malware", "ignore", "list"]
    if search:
        args += ["--search", search]
    args += ["--limit", str(limit), "--offset", str(offset)]
    return _imunify_run(args)


@mcp.tool()
def imunify_ignore_list(search: str = "", limit: int = 50, offset: int = 0) -> str:
    """Listet die Einträge der Imunify360-Malware-Ignore-Liste auf
    (imunify360-agent malware ignore list) - read-only.
    Rückgabe als JSON: id, path, added_date (Unix + ISO), resource_type.
    search: optionaler Suchbegriff für den Pfad (Teilstring).
    limit/offset: Paging (Standard 50/0, max. limit 500).
    """
    if search:
        _imunify_search_term(search, "search")
    res = _imunify_ignore_entries(
        search, limit=max(1, min(int(limit), 500)), offset=max(0, int(offset))
    )
    if not res["ok"]:
        return _json(res)
    data = res["data"]
    items = [
        {
            "id": it.get("id"),
            "path": it.get("path"),
            "added_date": it.get("added_date"),
            "added_date_iso": _ts_iso(it.get("added_date")),
            "resource_type": it.get("resource_type"),
        }
        for it in (data.get("items") or [])
    ]
    return _json({
        "ok": True,
        "total": data.get("max_count"),
        "returned": len(items),
        "offset": int(offset),
        "items": items,
    })


@mcp.tool()
def imunify_ignore_add(path: str, confirm: bool = False) -> str:
    """Fügt eine Datei zur Imunify360-Malware-Ignore-Liste hinzu
    (imunify360-agent malware ignore add <path>) - z.B. für False Positives
    in rotierten Logs wie /var/www/vhosts/example.com/logs/error_log.1.gz.

    Nur absolute, normalisierte Pfade unter /var/www/vhosts/<domain>/...,
    keine Wildcards (die CLI unterstützt laut Hilfe nur absolute Pfade).
    Die Datei muss existieren, darf kein Symlink sein und nicht über einen
    Symlink erreicht werden. Erfordert confirm=true pro Aufruf.
    """
    if not confirm:
        raise ValueError(
            "confirm=true erforderlich - dieses Tool ändert die Imunify360-"
            "Ignore-Liste auf einem Produktivserver und braucht eine explizite "
            "Bestätigung pro Aufruf."
        )
    p = _imunify_validate_path(path)
    _imunify_check_on_server(p, must_exist=True)

    res = _imunify_run(["malware", "ignore", "add", p])
    if not res["ok"]:
        return _json(res)
    return _json({"ok": True, "path": p, "added": res["data"].get("items")})


@mcp.tool()
def imunify_ignore_remove(path: str, confirm: bool = False, skip_rescan: bool = False) -> str:
    """Entfernt einen Pfad wieder aus der Imunify360-Malware-Ignore-Liste.
    Die CLI löscht nur per ID (imunify360-agent malware ignore delete <id>),
    daher wird die ID vorher über "malware ignore list --search <path>"
    ermittelt - entfernt werden nur Einträge mit exakt diesem Pfad.
    Pfadprüfung wie bei imunify_ignore_add (die Datei muss aber nicht mehr
    existieren). Laut CLI wird die Datei nach dem Entfernen standardmässig
    sofort neu gescannt; skip_rescan=true unterdrückt das.
    Erfordert confirm=true pro Aufruf.
    """
    if not confirm:
        raise ValueError(
            "confirm=true erforderlich - dieses Tool ändert die Imunify360-"
            "Ignore-Liste auf einem Produktivserver und braucht eine explizite "
            "Bestätigung pro Aufruf."
        )
    p = _imunify_validate_path(path)
    _imunify_check_on_server(p, must_exist=False)

    ids: list[int] = []
    offset = 0
    page = 500
    while True:
        res = _imunify_ignore_entries(p, limit=page, offset=offset)
        if not res["ok"]:
            return _json(res)
        items = res["data"].get("items") or []
        ids += [int(it["id"]) for it in items if it.get("path") == p and "id" in it]
        if len(items) < page:
            break
        offset += page

    if not ids:
        return _json({
            "ok": False,
            "error": f"Kein Eintrag mit exakt diesem Pfad in der Ignore-Liste: {p}",
        })

    args = ["malware", "ignore", "delete", *[str(i) for i in ids]]
    if skip_rescan:
        args.append("--skip-rescan")
    res = _imunify_run(args)
    if not res["ok"]:
        return _json(res)
    return _json({
        "ok": True,
        "path": p,
        "removed_ids": ids,
        "removed": res["data"].get("items"),
        "rescan": not skip_rescan,
    })


@mcp.tool()
def db_list(domain: str = "") -> str:
    """Listet MySQL/MariaDB-Datenbanken auf dem Server auf (SHOW DATABASES,
    authentifiziert über den Plesk-internen Admin-DB-Zugang aus
    /etc/psa/.psa.shadow - keine eigenen Datenbank-Zugangsdaten nötig).

    Mit domain werden nur Datenbanken gefiltert, deren Name die Domain
    (Punkte durch Unterstriche ersetzt) enthält - Plesk benennt Datenbanken
    nicht immer exakt nach der Domain, bei Bedarf ohne domain-Parameter alle
    auflisten und selbst zuordnen. Für jede gefundene (Nicht-System-)
    Datenbank werden zusätzlich deren Tabellen mit Zeilenanzahl und Grösse
    in MB angezeigt (aus information_schema.tables).
    """
    out = _mysql_exec("SHOW DATABASES", skip_column_names=True)
    all_dbs = [line.strip() for line in out.splitlines() if line.strip()]

    if domain:
        d = _domain_arg(domain)
        needle = d.replace(".", "_")
        matches = [db for db in all_dbs if needle in db or d in db]
        if not matches:
            return (
                f"Keine Datenbank mit '{domain}' im Namen gefunden.\n"
                f"Vorhandene Datenbanken: {', '.join(all_dbs)}"
            )
        detail_dbs = matches
    else:
        detail_dbs = [db for db in all_dbs if db not in _DB_SYSTEM_SCHEMAS]

    lines = [f"Datenbanken: {', '.join(all_dbs)}", ""]
    for db in detail_dbs:
        if db in _DB_SYSTEM_SCHEMAS:
            continue
        tables = _mysql_exec(
            "SELECT TABLE_NAME, TABLE_ROWS, "
            "ROUND((DATA_LENGTH + INDEX_LENGTH) / 1024 / 1024, 2) AS size_mb "
            f"FROM information_schema.tables WHERE table_schema = {_sql_quote(db)} "
            "ORDER BY TABLE_NAME"
        )
        lines.append(f"--- {db} ---")
        lines.append(tables or "(keine Tabellen)")
        lines.append("")
    return "\n".join(lines).strip()


@mcp.tool()
def db_query(database: str, sql: str) -> str:
    """Führt eine einzelne, reine Lese-Query gegen eine MySQL/MariaDB-
    Datenbank auf dem Server aus (SELECT/SHOW/EXPLAIN/DESCRIBE). Nutzt
    automatisch den Plesk-internen Admin-DB-Zugang (siehe db_list) - kein
    eigener Datenbank-Zugang pro Domain nötig.

    database: Datenbankname (siehe db_list für die vorhandenen Namen).
    sql: genau ein Statement, kein ';' zum Verketten mehrerer Statements.
    Schreibende oder dateisystemnahe Befehle (INSERT/UPDATE/DELETE/DROP/...,
    LOAD_FILE, INTO OUTFILE/DUMPFILE, SLEEP/BENCHMARK etc.) werden per
    Blacklist blockiert - siehe README für bekannte Einschränkungen dieser
    Prüfung (z.B. eine Spalte namens exakt "start"). Ausgabe auf 200 Zeilen
    begrenzt.
    """
    db = _validate_db_name(database)
    checked_sql = _validate_select_sql(sql)
    out = _mysql_exec(checked_sql, database=db, timeout=30)
    result_lines = out.splitlines()
    if len(result_lines) > 200:
        out = "\n".join(result_lines[:200]) + (
            f"\n... ({len(result_lines) - 200} weitere Zeilen abgeschnitten)"
        )
    return out or "(keine Ausgabe)"


@mcp.tool()
def db_search(
    database: str,
    term: str,
    max_tables: int = 30,
    limit_per_table: int = 5,
) -> str:
    """Durchsucht alle Text-Spalten (char/varchar/text/tinytext/mediumtext/
    longtext) aller Tabellen einer Datenbank nach term (einfacher
    LIKE '%term%'-Vergleich, kein Volltextindex nötig) - praktisch um z.B.
    eine E-Mail-Adresse oder Bestellnummer zu finden, ohne die
    Tabellenstruktur zu kennen. Scannt aus Performancegründen maximal
    max_tables Tabellen und zeigt maximal limit_per_table Treffer pro
    Tabelle - bei sehr grossen Datenbanken ggf. mehrfach mit gezielterem
    term oder db_query direkt verwenden. Nutzt den Plesk-internen
    Admin-DB-Zugang, strikt read-only.
    """
    db = _validate_db_name(database)
    term = term.strip()
    if not term:
        raise ValueError("term darf nicht leer sein.")
    if not (1 <= max_tables <= 100):
        raise ValueError("max_tables muss zwischen 1 und 100 liegen.")
    if not (1 <= limit_per_table <= 50):
        raise ValueError("limit_per_table muss zwischen 1 und 50 liegen.")

    cols_out = _mysql_exec(
        "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.columns "
        f"WHERE table_schema = {_sql_quote(db)} AND DATA_TYPE IN "
        "('char','varchar','text','tinytext','mediumtext','longtext') "
        "ORDER BY TABLE_NAME",
        database=db,
        skip_column_names=True,
    )
    by_table: dict[str, list[str]] = {}
    for line in cols_out.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        table, col = parts
        by_table.setdefault(table, []).append(col)

    if not by_table:
        return f"Keine Text-Spalten in '{db}' gefunden (oder Datenbank existiert nicht)."

    escaped_term = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    needle = _sql_quote(f"%{escaped_term}%")

    results = []
    scanned = 0
    for table, cols in by_table.items():
        if scanned >= max_tables:
            break
        scanned += 1
        where = " OR ".join(f"{_quote_ident(c)} LIKE {needle}" for c in cols)
        query = f"SELECT * FROM {_quote_ident(table)} WHERE {where} LIMIT {int(limit_per_table)}"
        try:
            out = _mysql_exec(query, database=db, timeout=15)
        except Exception as e:
            results.append(f"--- {table}: Fehler ({e}) ---")
            continue
        if out and out != "(keine Ausgabe)":
            results.append(f"--- {table} ---\n{out}")

    if not results:
        return f"Kein Treffer für '{term}' in {scanned} durchsuchten Tabellen von '{db}'."
    header = f"Treffer für '{term}' in '{db}' ({scanned} von {len(by_table)} Tabellen durchsucht):\n\n"
    return header + "\n\n".join(results)


@mcp.tool()
def server_load() -> str:
    """Zeigt allgemeine Serverlast: uptime/Load-Average, freier Speicher,
    Anzahl laufender Prozesse - für Checks, ob der ganze Server unter
    Last steht statt nur eine einzelne Domain."""
    return ssh_run("uptime && echo --- && free -h && echo --- && ps aux | wc -l")


@mcp.tool()
def run_diagnostic(command: str) -> str:
    """Führt einen read-only Diagnose-Befehl auf dem Server aus, für Fälle,
    die von den anderen Tools nicht abgedeckt sind. Nur eine Whitelist an
    Programmen ist erlaubt (plesk, lveinfo, lveps, systemctl, journalctl,
    grep, tail, head, cat, find, ls, du, df, dmesg, uptime, top, free, ps,
    hostname, wc, zcat, zgrep), je Utility gilt eine Positiv-Liste:
    - plesk: nur "plesk version", "plesk bin <utility> -i|--info|-l|--list|
      --show-*" und "plesk ext <name> --list|--info" (kein --wp-cli, dafür
      die wp_*-Tools; schreibende Aktionen über dns_*/php_settings_*/...)
    - systemctl: status, is-active, list-units, show, cat
    - journalctl ohne --vacuum-*/--rotate/--flush/--sync/-f; dmesg ohne
      -C/-c/-n; tail ohne -f/-F; hostname ohne Argumente
    - find, du und grep -r nur mit absolutem Pfad unterhalb von
      /var/www/vhosts, /var/log, /etc/nginx, /etc/httpd, /usr/local/psa;
      keine find-Aktionen (-exec/-delete/-fprint...), kein -L
    - keine Geräte wie /dev/zero, /dev/urandom, /proc/kcore
    Shell-Konstrukte (;, &&, >, $( ...) sind gesperrt, Pipes erlaubt. Läuft
    unter "timeout" (DIAG_TIMEOUT, Standard 60 s), Ausgabe auf 4 MB begrenzt.
    Beispiel: "journalctl -k --since '2026-09-17 21:00'"
    """
    return ssh_run_whitelisted(command)


# ---------------------------------------------------------------------------
# Google PageSpeed Insights (Lighthouse) - externe, read-only Analyse
# ---------------------------------------------------------------------------
#
# Ruft die öffentliche PageSpeed-Insights-API v5 von Google auf. Die Seite wird
# von Google geladen und analysiert, nicht von diesem Container oder dem
# Plesk-Server - es gibt also keinen SSH-/Plesk-Zugriff und keinen Request vom
# Container auf die Ziel-URL (kein SSRF-Risiko). Ohne API-Key teilt man sich
# das anonyme Kontingent mit allen anderen Nutzern und bekommt praktisch immer
# HTTP 429 - PAGESPEED_API_KEY sollte daher gesetzt sein (kostenlos, Google
# Cloud Console -> "PageSpeed Insights API" aktivieren -> API-Key, idealerweise
# per API-Restriction auf genau diese API beschränken).

_PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
_PSI_API_KEY = os.environ.get("PAGESPEED_API_KEY", "").strip()
_PSI_TIMEOUT = int(os.environ.get("PAGESPEED_TIMEOUT", "120"))
_PSI_CATEGORIES = {"performance", "accessibility", "best-practices", "seo"}
_PSI_STRATEGIES = {"mobile", "desktop"}
_PSI_LAB_METRICS = [
    "first-contentful-paint",
    "largest-contentful-paint",
    "total-blocking-time",
    "cumulative-layout-shift",
    "speed-index",
    "interactive",
]
_PSI_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", re.IGNORECASE)


def _psi_normalize_url(url: str) -> str:
    """Akzeptiert eine volle URL oder nur eine Domain (-> https://<domain>/).
    Nur http/https, öffentlicher Hostname (kein localhost/IP), keine
    Zugangsdaten in der URL (würden sonst an Google übermittelt)."""
    from urllib.parse import urlsplit

    raw = (url or "").strip()
    if not raw or len(raw) > 2048 or any(c.isspace() for c in raw):
        raise ValueError("Ungültige URL (leer, zu lang oder enthält Leerzeichen).")
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ValueError("Nur http:// oder https:// URLs sind erlaubt.")
    if parts.username or parts.password or "@" in parts.netloc:
        raise ValueError("URLs mit Zugangsdaten (user:pass@host) sind nicht erlaubt.")
    host = (parts.hostname or "").rstrip(".")
    if not _PSI_HOST_RE.match(host) or host.replace(".", "").isdigit():
        raise ValueError(
            f"Ungültiger Hostname {host!r} - nur öffentliche Domainnamen, keine IPs/localhost."
        )
    return parts._replace(path=parts.path or "/").geturl()


def _psi_parse_list(value: str, allowed: set[str], name: str) -> list[str]:
    items = [v.strip().lower() for v in (value or "").split(",") if v.strip()]
    if not items:
        raise ValueError(f"{name} darf nicht leer sein.")
    bad = [v for v in items if v not in allowed]
    if bad:
        raise ValueError(f"Ungültige Werte für {name}: {bad}. Erlaubt: {sorted(allowed)}")
    return list(dict.fromkeys(items))


def _psi_field_data(exp: dict[str, Any] | None) -> dict[str, Any] | None:
    """CrUX-Felddaten (echte Chrome-Nutzer, 28-Tage-Fenster) kompakt."""
    if not exp or not exp.get("metrics"):
        return None
    metrics = {}
    for key, m in exp["metrics"].items():
        metrics[key.replace("_MS", "").lower()] = {
            "p75": m.get("percentile"),
            "rating": m.get("category"),
        }
    return {
        "scope": exp.get("id"),
        "overall": exp.get("overall_category"),
        "origin_fallback": exp.get("origin_fallback", False),
        "metrics": metrics,
    }


def _psi_savings(audit: dict[str, Any]) -> tuple[float, float]:
    details = audit.get("details") or {}
    ms = details.get("overallSavingsMs") or 0
    ms_metric = max(
        (v for k, v in (audit.get("metricSavings") or {}).items() if k != "CLS" and isinstance(v, (int, float))),
        default=0,
    )
    return float(max(ms, ms_metric)), float(details.get("overallSavingsBytes") or 0)


_PSI_MISSING = "nicht im Lighthouse-Ergebnis enthalten"
# Teilchecks des Insights "document-latency-insight" (Lighthouse 12.6+/13)
_PSI_LATENCY_CHECKS = ["noRedirects", "serverResponseIsFast", "usesCompression"]
# Hauptdokument kleiner als das (unkomprimiert) -> mögliche Challenge-/Zwischenseite
_PSI_MIN_DOC_BYTES = 20 * 1024
# Textmuster typischer Bot-Schutz-/Challenge-Seiten (Vergleich ohne Gross-/Kleinschreibung)
_PSI_CHALLENGE_PATTERNS = [
    "one moment, please",
    "just a moment",
    "checking your browser",
    "checking if the site connection is secure",
    "verifying you are human",
    "please wait while we verify",
    "attention required",
    "ddos protection by",
]


def _psi_num(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(value, 2) if isinstance(value, float) and not value.is_integer() else int(value)


def _psi_audit_base(audit: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "title": audit.get("title"),
        "score": audit.get("score"),
        "score_display_mode": audit.get("scoreDisplayMode"),
        "display_value": audit.get("displayValue"),
    }
    if audit.get("numericValue") is not None:
        out["numeric_value"] = _psi_num(audit.get("numericValue"))
        out["numeric_unit"] = audit.get("numericUnit")
    if audit.get("errorMessage"):
        out["error"] = audit["errorMessage"]
    return out


def _psi_origin_table(audit: dict[str, Any], key: str, out_key: str) -> list[dict[str, Any]] | str:
    items = (audit.get("details") or {}).get("items")
    if not isinstance(items, list):
        return _PSI_MISSING
    return [
        {"origin": it.get("origin"), out_key: _psi_num(it.get(key))}
        for it in items
        if isinstance(it, dict)
    ]


def _psi_server_timing(audits: dict[str, Any]) -> dict[str, Any]:
    """TTFB/Server-Antwortzeit aus allen dafür vorhandenen Audits. Fehlende
    Audits werden explizit als fehlend markiert, nichts wird geschätzt."""
    out: dict[str, Any] = {}

    a = audits.get("server-response-time")
    if a is None:
        out["server_response_time"] = _PSI_MISSING
    else:
        entry = _psi_audit_base(a)
        items = (a.get("details") or {}).get("items")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            entry["url"] = items[0].get("url")
            entry["response_time_ms"] = _psi_num(items[0].get("responseTime"))
        out["server_response_time"] = entry

    a = audits.get("document-latency-insight")
    if a is None:
        out["document_latency_insight"] = _PSI_MISSING
    else:
        entry = _psi_audit_base(a)
        details = a.get("details") or {}
        checklist = details.get("items") if details.get("type") == "checklist" else None
        if isinstance(checklist, dict):
            checks = {}
            for key in list(dict.fromkeys(_PSI_LATENCY_CHECKS + list(checklist))):
                c = checklist.get(key)
                checks[key] = (
                    {"passed": c.get("value"), "label": c.get("label")} if isinstance(c, dict) else _PSI_MISSING
                )
            entry["checks"] = checks
        else:
            entry["checks"] = _PSI_MISSING
        debug = details.get("debugData")
        if isinstance(debug, dict):
            for src, dst in (
                ("redirectDuration", "redirect_duration_ms"),
                ("serverResponseTime", "server_response_time_ms"),
                ("uncompressedResponseBytes", "uncompressed_response_bytes"),
            ):
                entry[dst] = _psi_num(debug[src]) if src in debug else _PSI_MISSING
        ms, _ = _psi_savings(a)
        entry["est_savings_ms"] = round(ms) or None
        out["document_latency_insight"] = entry

    for audit_id, key, out_key in (
        ("network-server-latency", "serverResponseTime", "server_response_time_ms"),
        ("network-rtt", "rtt", "rtt_ms"),
    ):
        a = audits.get(audit_id)
        if a is None:
            out[audit_id.replace("-", "_")] = _PSI_MISSING
        else:
            entry = _psi_audit_base(a)
            entry["origins"] = _psi_origin_table(a, key, out_key)
            out[audit_id.replace("-", "_")] = entry
    return out


def _psi_node_text(node: Any) -> dict[str, Any] | None:
    if not isinstance(node, dict) or node.get("type") != "node":
        return None
    out = {k: str(node[k])[:200] for k in ("nodeLabel", "selector", "snippet") if node.get(k)}
    return out or None


def _psi_finding_details(audit: dict[str, Any], max_items: int) -> dict[str, Any]:
    """Top-N Einträge aus details.items eines Findings (URL + Einsparung)."""
    details = audit.get("details") or {}
    items = details.get("items")
    if details.get("type") not in ("table", "opportunity") or not isinstance(items, list):
        return {"items_total": 0, "items": [], "note": f"keine Einzel-Ressourcen im Lighthouse-Ergebnis "
                                                        f"(details.type={details.get('type')!r})"}
    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        url = it.get("url")
        if isinstance(url, dict):
            url = url.get("url") or url.get("value")
        if url is None and isinstance(it.get("source"), dict):
            url = it["source"].get("url")
        row: dict[str, Any] = {"url": url if isinstance(url, str) else None}
        for key in ("wastedMs", "wastedBytes", "totalBytes", "wastedPercent", "cacheLifetimeMs", "responseTime"):
            val = _psi_num(it.get(key))
            if val is not None:
                row[key] = val
        if isinstance(it.get("entity"), str):
            row["entity"] = it["entity"]
        node = _psi_node_text(it.get("node"))
        if node:
            row["node"] = node
        sub = (it.get("subItems") or {}).get("items") if isinstance(it.get("subItems"), dict) else None
        if isinstance(sub, list):
            reasons = [
                {k: (_psi_num(v) if k == "wastedBytes" else v) for k, v in si.items() if k in ("reason", "wastedBytes")}
                for si in sub
                if isinstance(si, dict) and si.get("reason")
            ]
            if reasons:
                row["reasons"] = reasons
        if len(row) > 1 or row["url"]:
            rows.append(row)
    rows.sort(key=lambda r: (r.get("wastedMs") or 0, r.get("wastedBytes") or 0, r.get("totalBytes") or 0), reverse=True)
    return {"items_total": len(rows), "items": rows[:max_items]}


def _psi_iter_nodes(obj: Any, depth: int = 0):
    if depth > 8:
        return
    if isinstance(obj, dict):
        if obj.get("type") == "node":
            yield obj
        for v in obj.values():
            yield from _psi_iter_nodes(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _psi_iter_nodes(v, depth + 1)


def _psi_page_check(lr: dict[str, Any]) -> dict[str, Any]:
    """Plausibilitätscheck: hat Google wirklich die Zielseite analysiert oder
    eine Bot-Schutz-/Challenge-Zwischenseite?"""
    audits = lr.get("audits") or {}
    out: dict[str, Any] = {
        "requested_url": lr.get("requestedUrl"),
        "final_displayed_url": lr.get("finalDisplayedUrl") or _PSI_MISSING,
        # Lighthouse liefert den <title>-Text nicht im Ergebnis (document-title
        # ist nur ein Pass/Fail-Audit ohne Text)
        "title": _PSI_MISSING,
    }
    warnings: list[str] = []

    items = ((audits.get("network-requests") or {}).get("details") or {}).get("items")
    doc = None
    if isinstance(items, list):
        main_url = lr.get("mainDocumentUrl") or lr.get("finalUrl")
        docs = [i for i in items if isinstance(i, dict) and i.get("resourceType") == "Document"]
        doc = next((i for i in docs if main_url and i.get("url") == main_url), None) or (docs[0] if docs else None)
    if doc is None:
        out["main_document"] = _PSI_MISSING
    else:
        out["main_document"] = {
            "url": doc.get("url"),
            "status_code": doc.get("statusCode"),
            "mime_type": doc.get("mimeType"),
            "transfer_size_bytes": _psi_num(doc.get("transferSize")),
            "resource_size_bytes": _psi_num(doc.get("resourceSize")),
        }
        size = _psi_num(doc.get("resourceSize"))
        size_basis = "unkomprimiert"
        if size is None:
            size, size_basis = _psi_num(doc.get("transferSize")), "übertragen"
        if size is not None and size < _PSI_MIN_DOC_BYTES:
            warnings.append(
                f"moegliche Challenge-/Zwischenseite: Hauptdokument nur {size} Bytes ({size_basis}, "
                f"Schwelle {_PSI_MIN_DOC_BYTES} Bytes)"
            )
        status = doc.get("statusCode")
        if isinstance(status, int) and status >= 400:
            warnings.append(f"Hauptdokument lieferte HTTP {status}")

    # Challenge-Texte: <title> fehlt im Ergebnis, daher Abgleich mit den
    # Element-Texten (nodeLabel/snippet), die Lighthouse in den Audits liefert
    # (z.B. LCP-Element)
    hits: list[str] = []
    for audit_id, a in audits.items():
        for node in _psi_iter_nodes((a or {}).get("details")):
            text = f"{node.get('nodeLabel') or ''} {node.get('snippet') or ''}".lower()
            for pat in _PSI_CHALLENGE_PATTERNS:
                if pat in text and f"{pat}|{audit_id}" not in hits:
                    hits.append(f"{pat}|{audit_id}")
    for hit in hits:
        pat, audit_id = hit.split("|", 1)
        warnings.append(f"moegliche Challenge-/Zwischenseite: Text {pat!r} in Seitenelement (Audit {audit_id})")
    out["challenge_patterns_checked"] = "Titel nicht verfügbar - geprüft gegen Element-Texte aus den Audits"
    out["warnings"] = warnings
    return out


def _psi_summarize(data: dict[str, Any], categories: list[str], max_findings: int,
                   include_details: bool = False, max_items: int = 10) -> dict[str, Any]:
    lr = data.get("lighthouseResult") or {}
    audits = lr.get("audits") or {}
    cats = lr.get("categories") or {}
    out: dict[str, Any] = {
        "final_url": lr.get("finalDisplayedUrl") or lr.get("finalUrl"),
        "analyzed_at": lr.get("fetchTime"),
        "lighthouse_version": lr.get("lighthouseVersion"),
        "scores": {
            c: (round(cats[c]["score"] * 100) if cats.get(c, {}).get("score") is not None else None)
            for c in categories
        },
    }
    if lr.get("runtimeError"):
        out["runtime_error"] = lr["runtimeError"]
    if lr.get("runWarnings"):
        out["run_warnings"] = lr["runWarnings"]
    out["page_check"] = _psi_page_check(lr)

    if "performance" in cats:
        out["lab_metrics"] = {
            m: {"value": audits[m].get("displayValue"), "score": audits[m].get("score")}
            for m in _PSI_LAB_METRICS
            if m in audits
        }
        out["lab_metrics"]["server_timing"] = _psi_server_timing(audits)
        out["field_data_url"] = _psi_field_data(data.get("loadingExperience"))
        out["field_data_origin"] = _psi_field_data(data.get("originLoadingExperience"))

        findings = []
        for ref in cats["performance"].get("auditRefs", []):
            if ref.get("group") in ("metrics", "hidden", "budgets"):
                continue
            a = audits.get(ref.get("id")) or {}
            score = a.get("score")
            if score is None or score >= 0.9 or a.get("scoreDisplayMode") in ("informative", "notApplicable", "manual"):
                continue
            ms, by = _psi_savings(a)
            finding = {
                "id": ref.get("id"),
                "title": a.get("title"),
                "display_value": a.get("displayValue"),
                "score": score,
                "est_savings_ms": round(ms) or None,
                "est_savings_kib": round(by / 1024) or None,
            }
            if include_details:
                finding["details"] = _psi_finding_details(a, max_items)
            findings.append(finding)
        findings.sort(key=lambda f: (f["est_savings_ms"] or 0, f["est_savings_kib"] or 0), reverse=True)
        out["performance_findings"] = findings[:max_findings]

    for c in categories:
        if c == "performance" or c not in cats:
            continue
        failed = []
        for ref in cats[c].get("auditRefs", []):
            a = audits.get(ref.get("id")) or {}
            if a.get("scoreDisplayMode") == "binary" and a.get("score") == 0:
                failed.append({"id": ref.get("id"), "title": a.get("title")})
        out[f"{c}_failed_audits"] = failed[:max_findings]
    return out


def _psi_run(url: str, strategy: str, categories: list[str], locale: str) -> dict[str, Any]:
    params: list[tuple[str, str]] = [("url", url), ("strategy", strategy), ("locale", locale)]
    params += [("category", c.upper().replace("-", "_")) for c in categories]
    # API-Key als Header statt Query-Parameter: so steht er nie in der
    # Request-URL und damit auch nicht in Logs (httpx loggt die volle URL)
    headers = {"X-goog-api-key": _PSI_API_KEY} if _PSI_API_KEY else {}
    try:
        resp = httpx.get(_PSI_ENDPOINT, params=params, headers=headers, timeout=_PSI_TIMEOUT)
    except httpx.TimeoutException:
        return {"error": f"Timeout nach {_PSI_TIMEOUT}s - Google hat die Analyse nicht rechtzeitig geliefert."}
    except httpx.HTTPError as exc:
        # Bewusst ohne Details/URL - nichts aus dem Request zurückgeben
        return {"error": f"Verbindungsfehler zur PageSpeed-API: {type(exc).__name__}"}
    if resp.status_code != 200:
        try:
            msg = resp.json().get("error", {}).get("message", "")
        except Exception:
            msg = resp.text[:300]
        if _PSI_API_KEY:
            msg = msg.replace(_PSI_API_KEY, "***")
        hint = ""
        if resp.status_code == 429 and not _PSI_API_KEY:
            hint = " (Kein PAGESPEED_API_KEY gesetzt - anonymes Kontingent erschöpft.)"
        return {"error": f"HTTP {resp.status_code}: {msg}{hint}"}
    return resp.json()


@mcp.tool()
def pagespeed_insights(
    url: str,
    strategy: str = "mobile",
    categories: str = "performance",
    max_findings: int = 10,
    locale: str = "de",
    include_details: bool = False,
    max_items: int = 10,
) -> str:
    """Analysiert eine Website mit Google PageSpeed Insights (Lighthouse) und
    liefert eine kompakte Zusammenfassung als JSON.

    Read-only und extern: Google lädt die Seite selbst, der Plesk-Server wird
    nicht angefasst. Eine Analyse dauert je Strategie ca. 10-60 Sekunden.

    Parameter:
    - url: volle URL (https://www.example.com/seite) oder nur die Domain
      (example.com -> https://example.com/). Nur öffentliche Hostnamen.
    - strategy: "mobile" (Standard), "desktop" oder "both" (beide parallel).
    - categories: kommagetrennt aus performance, accessibility,
      best-practices, seo (Standard: performance). "all" = alle vier.
    - max_findings: max. Anzahl Verbesserungspunkte pro Kategorie (1-50).
    - locale: Sprache der Audit-Titel (Standard "de").
    - include_details: true = je Performance-Finding die betroffenen
      Ressourcen (URL mit wastedMs / wastedBytes / totalBytes) mitliefern,
      z.B. für render-blocking-insight, unused-css-rules, unused-javascript,
      image-delivery-insight, cache-insight, unsized-images (Standard false).
    - max_items: max. Anzahl Ressourcen je Finding bei include_details
      (Standard 10, max. 50).

    Rückgabe pro Strategie: Scores (0-100), Labor-Messwerte (FCP, LCP, TBT,
    CLS, Speed Index, TTI), Felddaten echter Nutzer aus dem Chrome UX Report
    (URL- und Origin-Ebene, falls genug Traffic vorhanden), die wichtigsten
    Performance-Verbesserungen sortiert nach geschätzter Zeitersparnis sowie
    fehlgeschlagene Audits der übrigen Kategorien.
    Zusätzlich lab_metrics.server_timing (server-response-time,
    document-latency-insight inkl. Teilchecks Redirects/Serverantwort/
    Textkomprimierung, network-server-latency, network-rtt - fehlende Audits
    werden als "nicht im Lighthouse-Ergebnis enthalten" markiert) und
    page_check (finale URL, HTTP-Status und Grösse des Hauptdokuments,
    Warnung bei möglicher Challenge-/Zwischenseite).

    Tipp für Plesk-Diagnose: bei schlechter TTFB ("server-response-time")
    zusätzlich server_load, lve_stats und fpm_service_status der Domain prüfen.
    """
    from concurrent.futures import ThreadPoolExecutor

    try:
        target = _psi_normalize_url(url)
        strat = strategy.strip().lower()
        strategies = ["mobile", "desktop"] if strat == "both" else _psi_parse_list(strat, _PSI_STRATEGIES, "strategy")
        cats = sorted(_PSI_CATEGORIES) if categories.strip().lower() == "all" else _psi_parse_list(
            categories, _PSI_CATEGORIES, "categories"
        )
    except ValueError as exc:
        return _json({"error": str(exc)})
    max_findings = max(1, min(int(max_findings), 50))
    max_items = max(1, min(int(max_items), 50))
    loc = locale.strip() if re.fullmatch(r"[A-Za-z]{2}([-_][A-Za-z]{2})?", locale.strip() or "") else "de"

    with ThreadPoolExecutor(max_workers=len(strategies)) as pool:
        results = dict(zip(strategies, pool.map(lambda s: _psi_run(target, s, cats, loc), strategies)))

    out: dict[str, Any] = {"url": target, "api_key_configured": bool(_PSI_API_KEY)}
    for s, data in results.items():
        out[s] = data if "error" in data else _psi_summarize(data, cats, max_findings, bool(include_details), max_items)
    return _json(out)


# ---------------------------------------------------------------------------
# HTTP-Transport (Cloud/Docker) mit Bearer-Auth
# ---------------------------------------------------------------------------


def _build_http_app():
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.middleware.cors import CORSMiddleware
    from starlette.responses import JSONResponse

    # transport_security: DNS-Rebinding-Schutz deaktiviert, analog zu
    # bexio-mcp - sonst blockt das SDK jeden Request mit einem Host-Header,
    # der nicht "localhost"/eine IP ist (421 "Invalid Host header"), auch
    # wenn der Server bewusst über einen Reverse-Proxy unter einer echten
    # Domain erreichbar gemacht wird. Die eigentliche Absicherung übernimmt
    # ohnehin MCP_API_KEY/_BearerAuthMiddleware unten.
    # json_response=True: einfache application/json-Antworten statt SSE-
    # Stream - robuster hinter Reverse-Proxies (siehe bexio-mcp).
    starlette_app = mcp.streamable_http_app(
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host=_HTTP_HOST,
    )

    class _BearerAuthMiddleware:
        """Minimalistische ASGI-Middleware: prüft 'Authorization: Bearer <token>'."""

        def __init__(self, app, token: str):
            self.app = app
            self.expected = f"Bearer {token}".encode("latin-1", errors="replace")

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return
            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"")
            # Zeitkonstanter Vergleich (kein Abbruch beim ersten abweichenden Byte)
            if not hmac.compare_digest(auth_header, self.expected):
                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
            await self.app(scope, receive, send)

    secured_app = _BearerAuthMiddleware(starlette_app, _MCP_API_KEY)
    # CORS aussen um die Auth-Middleware: Browser-basierte MCP-Clients (z.B.
    # Claude.ai) rufen den Endpoint per Cross-Origin-JS-Fetch auf. Preflight-
    # OPTIONS-Requests (ohne Authorization-Header) werden von CORSMiddleware
    # direkt beantwortet, bevor sie die Bearer-Pruefung erreichen.
    cors_app = CORSMiddleware(
        secured_app,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["mcp-session-id"],
    )

    async def app(scope, receive, send):
        # /healthz: Docker-Healthcheck ohne Auth, nur {"status": "ok"} - keine Version,
        # keine Konfiguration. Prüft nur, ob der Prozess antwortet, nicht Plesk/SSH
        # (sonst würde ein Ausfall des Plesk-Servers den Container unhealthy machen).
        if scope["type"] == "http" and scope["path"] == "/healthz" and scope["method"] in ("GET", "HEAD"):
            await JSONResponse({"status": "ok"})(scope, receive, send)
            return
        # /upload/{id}: eigenes Einmal-Token statt MCP_API_KEY (siehe upload_begin),
        # ohne gültiges Token immer 401 - gleich stark wie der MCP-Endpunkt.
        if scope["type"] == "http" and scope["path"].startswith("/upload/"):
            await _upload_asgi(scope, receive, send)
            return
        await cors_app(scope, receive, send)

    return app


async def _run_http_server() -> None:
    import uvicorn

    app = _build_http_app()
    config = uvicorn.Config(app, host=_HTTP_HOST, port=_HTTP_PORT, log_level="info")
    srv = uvicorn.Server(config)
    print(f"plesk-mcp HTTP server running on {_HTTP_HOST}:{_HTTP_PORT}", flush=True)
    if _TRASH_AUTOCLEAN_DAYS > 0:
        threading.Thread(target=_trash_autoclean, daemon=True).start()
    await srv.serve()


def _check_startup_config() -> list[str]:
    """Fail-safe-Pruefungen vor dem Start (RuntimeError = kein Start) und
    Warnungen (Rueckgabe) fuer Konfigurationen, die funktionieren, aber
    Schutz abschwaechen."""
    if not _MCP_API_KEY:
        raise RuntimeError(
            "MCP_API_KEY (statisches Bearer-Token) fehlt - aus Sicherheitsgruenden kein Start ohne Token."
        )
    if len(_MCP_API_KEY) < _MCP_API_KEY_MIN_LEN:
        raise RuntimeError(
            f"MCP_API_KEY ist zu kurz ({len(_MCP_API_KEY)} Zeichen, mindestens {_MCP_API_KEY_MIN_LEN}) - "
            "ein zufaelliges Token erzeugen, z.B. mit: python -c "
            "\"import secrets; print(secrets.token_urlsafe(32))\""
        )
    if not _SSH_HOST_KEY and not _SSH_KNOWN_HOSTS:
        raise RuntimeError(
            "PLESK_SSH_HOST_KEY oder PLESK_SSH_KNOWN_HOSTS fehlt - ohne gepinnten SSH-Host-Key "
            "(MITM-Schutz) kein Start. Host-Key ermitteln mit: ssh-keyscan -t ed25519 <plesk-host>"
        )
    warnings: list[str] = []
    if _PUBLIC_BASE_URL and not _UPLOAD_TRUSTED_PROXIES:
        warnings.append(
            "WARNUNG: PUBLIC_BASE_URL ist gesetzt, aber UPLOAD_TRUSTED_PROXIES ist leer - das "
            "Upload-Rate-Limit sieht nur die IP des Reverse-Proxys (ein gemeinsamer Bucket fuer "
            "alle Clients). IP(s) des Proxys, wie der Container sie sieht, eintragen."
        )
    return warnings


def main() -> None:
    for warning in _check_startup_config():
        print(warning, file=sys.stderr, flush=True)
    import asyncio

    asyncio.run(_run_http_server())


if __name__ == "__main__":
    main()
