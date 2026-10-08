#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp[cli]>=2.0.0,<3.0.0",
#   "paramiko>=3.4.0",
#   "httpx>=0.27.0",
#   "starlette>=0.27.0",
#   "uvicorn>=0.23.0",
# ]
# ///
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
replace_in_vhost_file, replace_in_vhost_files, delete_vhost_backup, delete_vhost_log, dns_add_record, dns_delete_record,
dns_update_record, imunify_ignore_add, imunify_ignore_remove, wp_option_update
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

Transport wird über die Umgebungsvariable MCP_TRANSPORT gesteuert:
- "stdio" (Standard) - für die lokale Nutzung via uv/Claude Desktop.
- "http" - startet den Server als HTTP-Dienst (Streamable HTTP) für den Cloud-
  Einsatz, z.B. hinter einem Reverse-Proxy. Erfordert MCP_API_KEY.

Alle Zugangsdaten (SSH, Plesk-API-Key, MCP_API_KEY) werden ausschliesslich
über Umgebungsvariablen gesetzt - es sind keine echten Zugangsdaten im Code
hinterlegt.
"""

from __future__ import annotations

import base64
import datetime
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
import stat
import struct
import sys
import threading
import time
from typing import Any

import paramiko
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

# Force UTF-8 on Windows where default may be cp1252
if sys.platform == "win32":
    if hasattr(sys.stdin, "reconfigure"):
        try:
            sys.stdin.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

# HTTP mode: MCP_TRANSPORT=http (Cloud/Docker) statt stdio (lokal, Standard)
_HTTP_MODE = os.environ.get("MCP_TRANSPORT", "stdio").lower() in ("http", "streamable-http")
_HTTP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
_HTTP_PORT = int(os.environ.get("MCP_PORT", "8000"))
_MCP_API_KEY = os.environ.get("MCP_API_KEY", "")

if _HTTP_MODE and not _MCP_API_KEY:
    raise RuntimeError(
        "MCP_TRANSPORT=http erfordert MCP_API_KEY (statisches Bearer-Token) - "
        "aus Sicherheitsgruenden kein Start ohne Token."
    )

# Version = <Major.Minor>.<Patch>. Major.Minor wird hier von Hand gepflegt, der
# Patch-Teil zaehlt automatisch: Anzahl Commits, die eine der build-relevanten
# Dateien (server.py, Dockerfile, requirements.txt, Workflow) geaendert haben.
# Im Docker-Image setzt GitHub Actions die fertige Version als APP_VERSION,
# lokal (Git-Checkout) wird sie direkt aus der Git-Historie berechnet.
_VERSION_BASE = "0.11"
_VERSION_PATHS = ["server.py", "Dockerfile", "requirements.txt", ".github/workflows/docker-publish.yml"]


def _read_version() -> str:
    env_version = os.environ.get("APP_VERSION", "").strip()
    if env_version:
        return env_version
    try:
        import subprocess
        count = subprocess.run(
            ["git", "rev-list", "--count", "HEAD", "--", *_VERSION_PATHS],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        if count.isdigit():
            return f"{_VERSION_BASE}.{count}"
    except Exception:
        pass
    return f"{_VERSION_BASE}.0-dev"


__version__ = _read_version()

mcp = MCPServer("plesk-mcp", version=__version__)

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
# Ist einer von beiden gesetzt, wird der Host-Key strikt geprueft (RejectPolicy
# bei unbekanntem Key). Ist keiner gesetzt, wird der Key beim ersten Connect
# automatisch akzeptiert (AutoAdd) - bequem, aber ohne MITM-Schutz; es wird
# einmalig auf stderr gewarnt.
_SSH_KNOWN_HOSTS = os.environ.get("PLESK_SSH_KNOWN_HOSTS", "").strip()
_SSH_HOST_KEY = os.environ.get("PLESK_SSH_HOST_KEY", "").strip()
_ssh_hostkey_warned = False

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
# das schreibende Unterbefehle kennt. Nur lesende Operationen. Fuer Programme,
# die hier NICHT auftauchen (grep, tail, cat, du, df, ...), gilt weiter nur die
# generische Wort-Blacklist. Eine Positiv-Liste ist hier noetig, weil eine
# Blacklist bei "plesk"/"systemctl" die Schreib-Subcommands nicht dicht
# bekommt (plesk db/login/bin extension --install-url, systemctl mask/disable
# /poweroff/isolate ...).
SAFE_SUBCOMMANDS = {
    "systemctl": {
        "status", "show", "show-environment", "list-units", "list-unit-files",
        "list-timers", "list-sockets", "list-dependencies", "list-jobs",
        "is-active", "is-enabled", "is-failed", "is-system-running",
        "cat", "get-default",
    },
    # plesk: nur die lesenden CLI-Familien. Innerhalb davon fangen die
    # generische Blacklist (add/del/set/...) und FORBIDDEN_SUBFLAGS die
    # schreibenden Aktionen ab.
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
}


class SSHError(ToolError, RuntimeError):
    """Erwarteter SSH-/Validierungsfehler. Als ToolError sieht der Client die
    Meldung selbst statt nur 'Error executing tool <name>'."""


def _configure_host_keys(client: paramiko.SSHClient) -> None:
    """Konfiguriert die Host-Key-Verifikation aus den Umgebungsvariablen.
    Mit PLESK_SSH_KNOWN_HOSTS/PLESK_SSH_HOST_KEY: strikte Pruefung
    (RejectPolicy). Ohne beides: AutoAdd mit einmaliger Warnung."""
    global _ssh_hostkey_warned
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

    if pinned:
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        if not _ssh_hostkey_warned:
            print(
                "WARNUNG: PLESK_SSH_KNOWN_HOSTS/PLESK_SSH_HOST_KEY nicht gesetzt "
                "- SSH-Host-Key wird nicht geprueft (kein MITM-Schutz).",
                file=sys.stderr, flush=True,
            )
            _ssh_hostkey_warned = True
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())


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


def _ssh_exec(command: str, timeout: int | None = None) -> tuple[str, str, int]:
    """Führt ein Kommando aus und liefert (stdout, stderr, exit_code) getrennt
    zurück - für Tools, die stdout selbst parsen (z.B. JSON der Imunify-CLI)."""
    client = _ssh_connect()
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout or _SSH_TIMEOUT)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()
    finally:
        client.close()
    return out, err, exit_code


def ssh_run(command: str, timeout: int | None = None) -> str:
    """Führt ein Kommando ungeprüft aus (nur für die fest verdrahteten Tools,
    NICHT für Nutzereingaben ohne Whitelist-Check - dafür ssh_run_whitelisted).
    """
    out, err, exit_code = _ssh_exec(command, timeout=timeout)

    result = out
    if err.strip():
        result += f"\n[stderr]\n{err}"
    if exit_code != 0:
        result += f"\n[exit code: {exit_code}]"
    return result.strip() or "(keine Ausgabe)"


def _check_segment(seg: list[str]) -> None:
    """Prueft ein einzelnes Pipe-Segment (Token-Liste) read-only-tauglich:
    erlaubtes Programm, erlaubtes Subcommand (Positiv-Liste, wo noetig),
    keine schreibenden Optionsflags, keine gefaehrlichen find-Primitive."""
    if not seg:
        raise SSHError("Leeres Pipe-Segment im Kommando.")
    program = seg[0]
    if program not in ALLOWED_COMMAND_PREFIXES:
        raise SSHError(
            f"Programm '{program}' ist nicht erlaubt. "
            f"Erlaubt sind: {', '.join(sorted(ALLOWED_COMMAND_PREFIXES))}"
        )
    args = seg[1:]

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

    # 3) find-Aktions-Primitive (fuehren Kommandos aus / schreiben Dateien).
    if program == "find":
        for tok in args:
            if not tok.startswith("-"):
                continue
            if tok.lstrip("-").lower() in FORBIDDEN_FIND_PRIMARIES:
                raise SSHError(
                    f"find-Primitiv '{tok}' ist nicht erlaubt "
                    "(kann Kommandos ausfuehren oder Dateien schreiben)."
                )


def ssh_run_whitelisted(command: str, timeout: int | None = None) -> str:
    """Führt ein Kommando aus, das direkt von Claude/dem Nutzer kommt. Prüft:
    1. Keine Shell-Chaining-Konstrukte (;, &&, ||, `, $(, >, <, &, Newline)
    2. Keine verbotenen Wörter (restart/stop/kill/rm/... als eigenständiges Wort)
    3. Jedes Pipe-Segment: erlaubtes Programm, erlaubtes Subcommand
       (Positiv-Liste für plesk/systemctl), keine schreibenden Flags, keine
       gefährlichen find-Primitive.
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
        tokens = shlex.split(stripped)
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

    return ssh_run(stripped, timeout=timeout)


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
) -> tuple[str, str, int]:
    """Wie _ssh_exec, aber auf einer bereits offenen SSH-Verbindung."""
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout or _SSH_TIMEOUT)
    if stdin_data is not None:
        stdin.write(stdin_data)
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return out, err, stdout.channel.recv_exit_status()


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

    clean_path = path.strip().lstrip("/")
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


def _db_admin_password() -> str:
    """Liest das Plesk-Admin-DB-Passwort aus /etc/psa/.psa.shadow (root-only,
    lesbar da wir ohnehin als root per SSH verbunden sind)."""
    out = ssh_run(f"cat {_DB_SHADOW_PATH} 2>&1")
    pw = out.strip()
    if not pw or "No such file" in pw or "Permission denied" in pw or "\n" in pw:
        raise RuntimeError(
            f"Konnte das Plesk-DB-Admin-Passwort nicht aus {_DB_SHADOW_PATH} lesen "
            f"(Ausgabe: {pw!r}). Ist dies ein Plesk-Server mit lokaler MySQL/"
            "MariaDB-Installation und root-SSH-Zugang?"
        )
    return pw


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
    """Führt sql via `mysql`-CLI als Plesk-Admin-User aus. Passwort wird über
    die Umgebungsvariable MYSQL_PWD übergeben (nicht als -p-Flag), damit es
    nicht in `ps aux` für andere lokale User auf dem Server sichtbar ist."""
    pw = _db_admin_password()
    db_part = f" {shlex.quote(database)}" if database else ""
    # --binary-mode: deaktiviert die mysql-CLI-eigenen Kommandos (\!, \., system,
    # source, tee ...) und behandelt Backslashes als literal. Ohne dieses Flag
    # fuehrt der Client z.B. "SELECT 1 \! id" als Shell-Kommando aus (RCE als
    # root), obwohl die SQL-Blacklist das Statement fuer harmlos haelt.
    flags = "--connect-timeout=10 --batch --raw --binary-mode"
    if skip_column_names:
        flags += " -N"
    cmd = (
        f"MYSQL_PWD={shlex.quote(pw)} mysql -u{_DB_ADMIN_USER} {flags}"
        f"{db_part} -e {shlex.quote(sql)}"
    )
    return ssh_run(cmd, timeout=timeout)


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


@mcp.tool()
def search_log(
    domain: str,
    log_type: str = "proxy_error_log",
    pattern: str = "",
    lines: int = 200,
    include_rotated: bool = False,
) -> str:
    """Durchsucht ein Vhost-Log der Domain nach einem optionalen Muster
    (regex, an grep -E übergeben) und gibt die letzten `lines` Treffer zurück.
    log_type: proxy_error_log | error_log | access_log
    Pfad: /var/www/vhosts/<domain>/logs/<log_type>
    include_rotated: zusätzlich die rotierten, gzip-komprimierten Logs
    (<log_type>-YYYYMMDD.gz im selben Verzeichnis) mit durchsuchen - nötig
    für Vorfälle, die länger als die aktuelle Logrotation zurückliegen.
    """
    d = _domain_arg(domain)
    allowed_logs = {"proxy_error_log", "error_log", "access_log"}
    if log_type not in allowed_logs:
        raise ValueError(f"log_type muss einer von {allowed_logs} sein.")

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
) -> str:
    """Schreibt/überschreibt eine Datei im Vhost-Verzeichnis der Domain
    (/var/www/vhosts/<domain>/<path>) - legt auch fehlende Zwischenordner an.
    encoding: "text" (content ist Klartext, UTF-8 - Standard) oder "base64"
    (content ist base64-kodiert, für Binärdateien wie Bilder/ZIPs).

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

    full = _vhost_path(domain, path)
    base = f"{_VHOST_BASE}/{_domain_arg(domain)}"
    if full == base:
        raise ValueError("path muss auf eine Datei zeigen.")
    parts = full[len(base) + 1:].split("/")

    if encoding == "base64":
        try:
            data = base64.b64decode(content, validate=True)
        except Exception as e:
            raise ValueError(f"content ist kein gültiges Base64: {e}")
    else:
        data = content.encode("utf-8")

    # Gemeinsame Schreiblogik mit Upload/Fetch (Helper auf dem Plesk-Server,
    # siehe _HELPER_SRC): Temp-Datei, Backup, atomarer rename, Besitzer.
    try:
        res = _helper_put(_helper_args(base, parts=parts, max_bytes=len(data)), [data])
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


def _replace_parts(domain: str, path: str) -> list[str]:
    """Pfadprüfung wie write_vhost_file (_vhost_path), zusätzlich ohne "..",
    absolute Pfade oder Wildcards (_strict_parts)."""
    full = _vhost_path(domain, path)
    if full == f"{_VHOST_BASE}/{_domain_arg(domain)}":
        raise ValueError("path muss auf eine Datei zeigen.")
    return _strict_parts(path)


def _replace_run(domain: str, old_str: str, new_str: str, replace_all: bool,
                 paths: list[list[str]] | None = None, glob: str | None = None) -> dict[str, Any]:
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
                        reserve=_UPLOAD_MIN_FREE)
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
    write_vhost_file: kein "..", keine absoluten Pfade, Abbruch bei Symlinks
    (Datei oder Ordner im Pfad), keine root-eigenen Dateien ausserhalb von
    httpdocs; Besitzer, Gruppe und Rechte bleiben erhalten (root-eigene
    Dateien unter httpdocs werden wie bei write_vhost_file auf den
    Subscription-User umgestellt).

    Rückgabe: Anzahl Ersetzungen, Backup-Pfad, neue Dateigrösse und je Treffer
    eine Kontextzeile vorher/nachher (max. 120 Zeichen, max. 50 Treffer).
    """
    if not confirm:
        raise ValueError(
            "confirm=true erforderlich - dieses Tool schreibt auf einem "
            "Produktivserver und braucht eine explizite Bestätigung pro Aufruf."
        )
    parts = _replace_parts(domain, path)
    res = _replace_run(domain, old_str, new_str, replace_all, paths=[parts])
    if not res["files"]:
        raise ValueError(
            f"old_str kommt in '{'/'.join(parts)}' nicht vor - nichts geändert. Exakten Text "
            "inkl. Leerzeichen/Einrückung prüfen (z.B. mit read_vhost_file)."
        )
    base = f"{_VHOST_BASE}/{_domain_arg(domain)}"
    return _json(_replace_file_view(base, res["files"][0]))


@mcp.tool()
def replace_in_vhost_files(
    domain: str,
    old_str: str,
    new_str: str,
    paths: list[str] | None = None,
    glob: str = "",
    replace_all: bool = False,
    confirm: bool = False,
) -> str:
    """Wie replace_in_vhost_file, aber für mehrere Dateien in einem Aufruf.
    Entweder paths (Liste relativer Pfade, max. 50) ODER glob angeben. glob
    ist relativ zu /var/www/vhosts/<domain>/ und muss mit "httpdocs/"
    beginnen (z.B. "httpdocs/wp-content/themes/mein-theme/**/*.php"; "*", "?",
    "[...]" pro Ordnerebene, "**" für beliebig viele Ebenen; max. 50 Treffer).
    Beim glob werden Symlinks nicht verfolgt (nur aufgelistet), Backups
    (.bak-*) und der Papierkorb ignoriert.

    Pro Datei dieselben Prüfungen und Backups wie replace_in_vhost_file.
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
    plist = None
    if paths:
        if len(paths) > _REPLACE_MAX_FILES:
            raise ValueError(f"Maximal {_REPLACE_MAX_FILES} Dateien pro Aufruf.")
        plist = [_replace_parts(domain, p) for p in paths]
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
    res = _replace_run(domain, old_str, new_str, replace_all, paths=plist, glob=g or None)
    base = f"{_VHOST_BASE}/{_domain_arg(domain)}"
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
            "SELECT h.www_root FROM domains d JOIN hosting h ON h.dom_id = d.id "
            f"WHERE d.id = {int(dom_id)} OR d.webspace_id = {int(dom_id)}"
        )
        out, _, rc = _client_exec(client, f"plesk db -Ne {shlex.quote(sql)}")
        docroots = []
        for r in out.strip().splitlines():
            r = posixpath.normpath(r.strip())
            if r.startswith(base + "/"):
                docroots.append(r[len(base) + 1:].split("/"))
        out, _, rc = _client_exec(client, f"id -u {shlex.quote(login)} && id -g {shlex.quote(login)}")
        ids = out.split()
        if rc != 0 or len(ids) != 2 or not all(i.isdigit() for i in ids) or "0" in ids:
            raise ValueError(f"System-User von '{d}' nicht ermittelbar oder root.")
    finally:
        client.close()
    if not docroots:
        raise ValueError(f"Kein Docroot für '{d}' gefunden.")
    return {"domain": d, "base": base, "login": login, "uid": int(ids[0]), "gid": int(ids[1]),
            "docroots": docroots}


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
    if not _HTTP_MODE or not _PUBLIC_BASE_URL:
        raise ValueError("Upload nur im HTTP-Modus mit gesetzter PUBLIC_BASE_URL verfügbar.")
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
    if _rate_limited(_client_ip(scope), now):
        await reply(429, {"error": "too_many_requests"})
        return
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
# Nur Optionen aus der Allowlist sind erlaubt. Zugangsdaten (wp-config.php,
# DB-Passwort, Salts, /etc/psa/.psa.shadow) werden von diesen Tools weder
# gelesen noch ausgegeben: die Domain-Daten kommen über "plesk db", das sich
# selbst authentifiziert.

_WP_CLI_PATH = os.environ.get("WP_CLI_PATH", "/usr/local/bin/wp-standalone").strip()
_WP_CLI_TIMEOUT = int(os.environ.get("WP_CLI_TIMEOUT", "120"))
_WP_OPTION_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,191}$")
_WP_OPTION_ALLOWLIST_DEFAULT = ("wp_rocket_settings", "elementor_font_display")
_WP_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_WP_SYSUSER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,63}$")
_WP_BACKUP_ID_RE = re.compile(r"^(\d{14})-([0-9a-f]{8})@([a-z0-9.-]+)$")
_WP_BACKUP_SUBDIR = ".plesk-mcp/wp-option-backups"


def _wp_option_allowlist() -> set[str]:
    """Standard-Allowlist plus Einträge aus WP_OPTION_ALLOWLIST (kommagetrennt)."""
    names = set(_WP_OPTION_ALLOWLIST_DEFAULT)
    for n in os.environ.get("WP_OPTION_ALLOWLIST", "").split(","):
        n = n.strip()
        if n and _WP_OPTION_NAME_RE.match(n):
            names.add(n)
    return names


def _wp_check_option(option_name: str) -> str:
    name = option_name.strip()
    allowed = _wp_option_allowlist()
    if not _WP_OPTION_NAME_RE.match(name) or name not in allowed:
        raise ValueError(
            f"Option {option_name!r} ist nicht freigegeben. Erlaubt: "
            f"{', '.join(sorted(allowed))} (erweiterbar per ENV WP_OPTION_ALLOWLIST)."
        )
    return name


def _wp_domain(domain: str) -> str:
    d = domain.strip().lower()
    if not _WP_DOMAIN_RE.match(d) or ".." in d:
        raise ValueError(f"Ungültiger Domainname: {domain!r}")
    return d


def _wp_context(client: paramiko.SSHClient, domain: str) -> dict[str, str]:
    """System-User, Home und PHP-CLI der Domain aus der Plesk-Datenbank."""
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
    return {"domain": domain, "login": login, "home": home, "php": php}


def _wp_run_as_user(
    client: paramiko.SSHClient,
    ctx: dict[str, str],
    argv: list[str],
    cwd: str,
    stdin_data: bytes | None = None,
) -> tuple[str, str, int]:
    """Führt argv als System-User der Subscription aus (runuser, leere
    Umgebung, HOME = Subscription-Home) - nie als root."""
    cmd = (
        f"cd {shlex.quote(cwd)} && runuser -u {shlex.quote(ctx['login'])} -- "
        f"env -i HOME={shlex.quote(ctx['home'])} PATH=/usr/bin:/bin "
        + " ".join(shlex.quote(a) for a in argv)
    )
    return _client_exec(client, cmd, timeout=_WP_CLI_TIMEOUT, stdin_data=stdin_data)


def _wp_cli(
    client: paramiko.SSHClient, ctx: dict[str, str], wp_dir: str, args: list[str]
) -> str:
    out, err, rc = _wp_run_as_user(
        client, ctx,
        [ctx["php"], _WP_CLI_PATH, f"--path={wp_dir}", "--no-color", *args],
        cwd=wp_dir,
    )
    if rc != 0:
        msg = "\n".join(line for line in (err.strip() or out.strip()).splitlines()[-5:])
        raise SSHError(f"WP-CLI 'wp {args[0]} {args[1] if len(args) > 1 else ''}' fehlgeschlagen: {msg}")
    return out


def _wp_json(text: str) -> Any:
    """JSON aus WP-CLI-Ausgabe; toleriert Zeilen, die Plugins vorneweg ausgeben."""
    text = text.strip()
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
    out, err, rc = _wp_run_as_user(
        client, ctx,
        [ctx["php"], _WP_CLI_PATH, f"--path={wp_dir}", "--no-color",
         "option", "get", name, "--format=json"],
        cwd=wp_dir,
    )
    if rc != 0:
        if "does it exist" in (err + out).lower():
            return _WP_MISSING
        msg = "\n".join((err.strip() or out.strip()).splitlines()[-5:])
        raise SSHError(f"WP-CLI 'wp option get' fehlgeschlagen: {msg}")
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
    korrekt bedient. Nur Optionen aus der Allowlist (wp_rocket_settings,
    elementor_font_display, erweiterbar per ENV WP_OPTION_ALLOWLIST).
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
    hostname, wc, zcat, zgrep), destruktive Subcommands
    (restart/stop/kill/rm/delete/...) sind blockiert. Beispiel:
    "journalctl -k --since '2026-09-17 21:00'"
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
            self.token = token

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return
            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"").decode("latin-1")
            if auth_header != f"Bearer {self.token}":
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


def main() -> None:
    if _HTTP_MODE:
        import asyncio

        asyncio.run(_run_http_server())
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
