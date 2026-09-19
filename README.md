# Plesk MCP Connector

MCP-Server für Diagnose (und, mit expliziter Bestätigung, gezielte Datei-Edits) auf einem Plesk-Server. Läuft in zwei Betriebsarten:

- **stdio (lokal)** — klassischer lokaler MCP-Server via `uv`/Claude Desktop.
- **HTTP (Cloud)** — als Docker-Container mit Streamable-HTTP-Transport (z.B. via Portainer), für den Zugriff aus Cloud-Sessions ohne laufenden lokalen Rechner.

Drei Datenquellen: SSH/SFTP (für alles, was nur auf Betriebssystem-Ebene existiert - PHP-FPM-Status, systemd-Journal, OOM-Kills, Disk-Nutzung, Serverlast, Vhost-Dateizugriff - sowie für Plesk-CLI-Befehle), die Plesk-REST-API (für strukturierte Plesk-eigene Daten wie Domains/Subscriptions über das `plesk_api_get`-Tool) und MySQL/MariaDB direkt (`db_list`/`db_query`/`db_search`, read-only, über den Plesk-internen Admin-DB-Zugang - siehe Abschnitt "Sicherheit").

Alle Zugangsdaten (SSH, Plesk-API-Key, Auth-Token) werden ausschliesslich über Umgebungsvariablen konfiguriert - im Code stehen keine Secrets. Dieses Repo ist **privat** (verarbeitet SSH-Zugangsdaten zu Produktivservern).

Fast alle Tools sind read-only: kein Neustart von Services, keine destruktiven Kommandos (Whitelist + Blacklist in `server.py`), und das `plesk_api_get`-Tool kann nur GET-Requests stellen. Ausnahme: `write_vhost_file` und `delete_vhost_backup` dürfen Dateien innerhalb von `/var/www/vhosts/<domain>/` anlegen/überschreiben/löschen - beide erfordern zwingend `confirm=true` pro Aufruf (keine globale Freischaltung), `write_vhost_file` legt vor dem Überschreiben automatisch ein Backup der alten Version an. Details siehe Abschnitt "Sicherheit" unten.

## Lokale Installation (stdio)

Kein `pip install` nötig - `uv` lädt die Dependencies automatisch beim ersten Start (siehe PEP-723-Metadaten am Dateianfang von `server.py`).

### Konfiguration in Claude Desktop (claude_desktop_config.json)

```json
{
  "mcpServers": {
    "plesk": {
      "command": "uv",
      "args": ["run", "C:\\Pfad\\zu\\plesk-mcp\\server.py"],
      "env": {
        "PLESK_SSH_HOST": "dein-plesk-server.example.com",
        "PLESK_SSH_PORT": "22",
        "PLESK_SSH_USER": "root",
        "PLESK_SSH_PASSWORD": "dein-passwort"
      }
    }
  }
}
```

Ohne gesetztes `MCP_TRANSPORT` (oder mit `MCP_TRANSPORT=stdio`) verhält sich der Server als klassischer lokaler MCP-Server - die Cloud/HTTP-Erweiterung ändert am lokalen Betrieb nichts.

## Umgebungsvariablen

| Variable | Pflicht | Standard | Beschreibung |
|---|---|---|---|
| `PLESK_SSH_HOST` | ja | – | Hostname/IP des Plesk-Servers |
| `PLESK_SSH_PORT` | nein | `22` | SSH-Port |
| `PLESK_SSH_USER` | nein | `root` | SSH-Benutzer |
| `PLESK_SSH_PASSWORD` | ja, ausser bei Key-Auth | – | SSH-Passwort |
| `PLESK_SSH_KEY_PATH` | nein | – | Alternative zu Passwort: Pfad zu einem privaten SSH-Key |
| `PLESK_SSH_TIMEOUT` | nein | `20` | SSH-Verbindungs-Timeout in Sekunden |
| `PLESK_API_HOST` | nein | `PLESK_SSH_HOST` | Host für die Plesk-REST-API, falls abweichend vom SSH-Host |
| `PLESK_API_PORT` | nein | `8443` | Port der Plesk-REST-API |
| `PLESK_API_KEY` | ja, für `plesk_api_get` | – | Secret Key, erzeugt via `plesk bin secret_key --create` |
| `PLESK_API_VERIFY_SSL` | nein | `true` | `false`, falls der Plesk-Server ein selbstsigniertes Zertifikat nutzt |
| `PLESK_API_TIMEOUT` | nein | `20` | Timeout in Sekunden für Requests gegen die Plesk-REST-API |
| `MCP_TRANSPORT` | nein | `stdio` | `stdio` = lokal (Standard) / `http` = Cloud-Modus (Streamable HTTP) |
| `MCP_API_KEY` | ja, nur im HTTP-Modus | – | Statisches Bearer-Token zum Schutz des öffentlichen Endpoints. Ohne dieses Token startet der HTTP-Modus nicht (fail-safe) |
| `MCP_HOST` | nein | `0.0.0.0` | Bind-Adresse des HTTP-Servers *innerhalb* des Containers |
| `MCP_PORT` | nein | `8000` | Port des HTTP-Servers *innerhalb* des Containers |
| `MCP_BIND_ADDR` | nein | `127.0.0.1` | Bind-Adresse *auf dem Docker-Host* (docker-compose Port-Mapping) |
| `MCP_HOST_PORT` | nein | `8422` | Port *auf dem Docker-Host* (docker-compose Port-Mapping) |

## Image-Build (GitHub Actions → GitHub Container Registry)

Das Docker-Image wird bei jedem Push auf `main` automatisch per GitHub Actions gebaut und nach `ghcr.io/kdt-solutions/plesk-mcp:latest` veröffentlicht (`.github/workflows/docker-publish.yml`) - kein lokaler Build in Portainer nötig (das ist zuverlässiger, siehe Erfahrung mit dem zammad-mcp-Repo).

**Wichtig, da das Repo privat ist:** Das GHCR-Package ist ebenfalls privat. Damit Portainer es pullen kann, muss dort unter **Registries → Add registry → Custom registry** ein Eintrag für `ghcr.io` mit deinem GitHub-Benutzernamen und einem Personal Access Token (Scope `read:packages`) hinterlegt werden.

## Cloud-Betrieb via Portainer (empfohlen)

1. In Portainer **Registries** den ghcr.io-Zugang hinterlegen (siehe oben)
2. **Stacks → Add stack**, **Build method: Repository** wählen, Repo-URL eintragen (Branch `main`), Compose-Pfad `docker-compose.yml`
3. Unter **Environment variables** die Werte aus der Tabelle oben setzen (landen NICHT im Repo)
4. **Deploy the stack**
5. Für ein Update später: **Pull and redeploy** klicken

## Netzwerk / Reverse-Proxy

Der Container bindet standardmässig nur auf `127.0.0.1:8422` auf dem Docker-Host - nicht direkt öffentlich erreichbar. Für den Zugriff aus einer Cloud-Claude-Session braucht es zusätzlich:

1. Eine eigene Subdomain (z.B. `plesk-mcp.deine-domain.ch`)
2. Einen Reverse-Proxy (Plesk/nginx) mit TLS-Zertifikat, der auf `127.0.0.1:8422` weiterleitet (Endpoint-Pfad: `/mcp`)
3. In der Cloud-Claude-Session wird der Server dann als Remote-MCP mit `https://plesk-mcp.deine-domain.ch/mcp` und dem `MCP_API_KEY` als Bearer-Token eingebunden - im Connector-Setup **"Keine Anmeldung"** wählen (kein OAuth) und einen Request-Header `Authorization: Bearer <MCP_API_KEY>` hinzufügen

## Sicherheit

- Der HTTP-Modus startet nur, wenn `MCP_API_KEY` gesetzt ist - es gibt also nie einen ungeschützten öffentlichen Endpoint.
- Jeder HTTP-Request muss den Header `Authorization: Bearer <MCP_API_KEY>` mitschicken, sonst Antwort `401 unauthorized`.
- `run_diagnostic` prüft Programmname gegen eine Whitelist und blockt bekannte destruktive Subcommands/Shell-Konstrukte zusätzlich per Blacklist-Substring-Check. Kein Freibrief für beliebige Shell-Befehle.
- `plesk_api_get` kann nur GET-Requests stellen - schreibender Zugriff auf die Plesk-REST-API ist über dieses Tool nicht möglich.
- `.env` ist in `.gitignore` und wird nie committet.
- Repo und GHCR-Package sind privat - zusätzlich zur Bearer-Auth des Servers.
- Für produktiven Einsatz: dedizierten SSH-User mit eingeschränkten Rechten (statt root) und/oder Key-Auth statt Passwort erwägen; für `plesk_api_get` den Secret Key optional per `-ip-address` auf die Docker-Host-IP einschränken (siehe `plesk bin secret_key --create`).
- `write_vhost_file`/`delete_vhost_backup` sind strikt auf `/var/www/vhosts/<domain>/` beschränkt (Path-Traversal wie `../../etc/passwd` wird über `os.path.normpath` + Prefix-Check blockiert) und erfordern beide `confirm=true` als expliziten Parameter pro Aufruf - es gibt keinen globalen Schreib-Schalter, ein Aufruf ohne `confirm=true` schlägt immer fehl.
- `write_vhost_file` legt vor jedem Überschreiben automatisch ein Backup der alten Version als `<path>.bak-<YYYYMMDDHHMMSS>` an und bricht ab, falls der Zielpfad bereits ein Symlink ist (kein Schreiben durch Symlinks hindurch).
- `delete_vhost_backup` löscht ausschliesslich Dateien, deren Pfad auf `.bak-<14-stellige Zeitstempel>` endet - kein generisches Lösch-Tool für beliebige Vhost-Dateien.
- `db_list`/`db_query`/`db_search` nutzen automatisch den Plesk-internen MySQL/MariaDB-Admin-Zugang (Login `admin`, Passwort im Klartext in `/etc/psa/.psa.shadow` - damit verwaltet Plesk selbst alle Kunden-Datenbanken, z.B. für phpMyAdmin-SSO). Das ist technisch ein sehr mächtiger Zugang (faktisch DB-root) und wird genutzt, weil die Tools ohnehin per Root-SSH laufen und diese Datei sonst ebenso lesen könnten - es wird also kein zusätzlicher, separat abgesicherter Zugang geschaffen. Die Beschränkung auf read-only passiert ausschliesslich in `server.py` (`_validate_select_sql`), nicht durch MySQL-Rechte: erlaubt sind nur einzelne SELECT/SHOW/EXPLAIN/DESCRIBE-Statements ohne `;`-Verkettung, eine Blacklist blockt zusätzlich Schreib-Befehle (INSERT/UPDATE/DELETE/DROP/...) sowie dateisystemnahe Funktionen (`LOAD_FILE`, `INTO OUTFILE`/`DUMPFILE`) und potenzielle DoS-Funktionen (`SLEEP`, `BENCHMARK`). Das Passwort wird beim Aufruf über die Umgebungsvariable `MYSQL_PWD` an den `mysql`-Client übergeben statt als `-p`-Flag, damit es nicht in `ps aux` für andere lokale User auf dem Server sichtbar ist.

**Bekannte Einschränkung von `run_diagnostic`:** Die Wort-Blacklist (restart, stop, kill, rm, ...) matcht auch dann, wenn das Wort z.B. in einem Suchmuster für `find`/`grep` vorkommt (z.B. `find / -iname "*kill*"` wird blockiert). Für den Anwendungsfall "Fehler/Support-Tickets diagnostizieren" ist das unkritisch, da die dedizierten Tools (`check_oom_kills`, `search_log`) die relevanten Fälle direkt abdecken.

**Bekannte Einschränkung von `db_query`/`db_search`:** Die SQL-Wort-Blacklist prüft auf ganze Wörter, blockt also z.B. eine Query mit einer Spalte, die exakt `start` heisst (nicht aber `start_date` o.ä.) - analog zur Einschränkung von `run_diagnostic`. `db_search` ist ein einfacher `LIKE '%term%'`-Scan ohne Index-Nutzung und daher bei sehr grossen Tabellen langsam; `max_tables`/`limit_per_table` begrenzen den Aufwand pro Aufruf.

## Verfügbare Tools

| Tool | Beschreibung |
|------|--------------|
| `domain_info` | Plesk-Domain-Infos (Status, Disk, Traffic, SSL, Subscription) |
| `dns_records` | DNS-Resource-Records der Domain-Zone (`plesk bin dns --info`) |
| `backup_list` | Vorhandene lokale Backup-Dateien (Server-/Domain-Backups unter `/var/lib/psa/dumps`) |
| `wp_toolkit_list` | Vom WP Toolkit verwaltete WordPress-Installationen auflisten |
| `wp_toolkit_info` | Detail-Infos zu einer WordPress-Installation (Version, Updates, Plugins/Themes) |
| `plesk_api_get` | Read-only GET gegen die Plesk-REST-API für strukturierte JSON-Daten (z.B. Domains, Subscriptions) |
| `lve_stats` | CloudLinux-LVE-Ressourcen-Faults (CPU/IO-Limits) |
| `fpm_service_status` | Findet den PHP-FPM-Pool-Service der Domain und zeigt dessen Status |
| `fpm_journal` | systemd-Journal des FPM-Service in einem Zeitfenster |
| `search_log` | Durchsucht proxy_error_log/error_log/access_log (optional inkl. rotierter .gz-Logs) |
| `search_main_nginx_log` | Durchsucht das serverweite nginx-Log unter /var/log/nginx/ (access/error, optional inkl. rotierter .gz-Logs) - erfasst auch 408/523-Fehler vor dem Routing zum Vhost |
| `check_oom_kills` | Kernel-OOM-Kills im Zeitfenster |
| `disk_usage` | Speichernutzung des Vhost-Verzeichnisses |
| `read_vhost_file` | Liest eine Datei aus dem Vhost-Verzeichnis (Text oder Base64) |
| `write_vhost_file` | Schreibt/überschreibt eine Datei im Vhost-Verzeichnis - **erfordert `confirm=true`**, legt vorher automatisch ein Backup an |
| `delete_vhost_backup` | Löscht eine von `write_vhost_file` angelegte `.bak-*`-Datei - **erfordert `confirm=true`** |
| `db_list` | Listet MySQL/MariaDB-Datenbanken (optional gefiltert nach Domain) inkl. Tabellen, Zeilenanzahl und Grösse |
| `db_query` | Führt eine einzelne read-only SQL-Query (SELECT/SHOW/EXPLAIN/DESCRIBE) gegen eine Datenbank aus |
| `db_search` | Durchsucht alle Text-Spalten aller Tabellen einer Datenbank nach einem Begriff (LIKE-Scan) |
| `server_load` | Allgemeine Serverlast (uptime, free, Prozessanzahl) |
| `run_diagnostic` | Generischer Fallback, nur Whitelist an read-only Befehlen erlaubt |
