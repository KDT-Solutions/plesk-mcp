# Changelog

## 0.25

- Neuer Endpunkt `GET /healthz` ohne Auth, liefert nur `{"status": "ok"}`. Prüft nur, ob der Prozess antwortet, nicht Plesk oder die SSH-Verbindung. Alle anderen Pfade brauchen weiterhin `MCP_API_KEY` (bzw. `/upload/{id}` das Einmal-Token).
- `HEALTHCHECK` im Dockerfile (Python/urllib auf `/healthz`, interval 30s, timeout 5s, start_period 15s, retries 3).

## 0.24

- Fix `web_server_settings_rollback` für `nginx-directives`, wenn die Datei vor dem Update nicht existierte: Nach dem Entfernen von `vhost_nginx.conf` lief `nginx -t` noch vor dem Reconfigure und schlug fehl, weil die Domain-Konfiguration die Datei noch per `include` einbindet ("No such file or directory"). Der Rollback wurde dadurch abgebrochen und der alte Stand wiederhergestellt. Beim Entfernen entfällt jetzt der Test vor dem Reconfigure; `nginx -t` läuft danach.

## 0.23

- Fix `nginx-directives` in `web_server_settings_update`: `plesk bin domain --update-web-server-settings` kennt keine Option `-nginx-directives` ("Unrecognized option"), Plesk kann die zusätzlichen nginx-Anweisungen per CLI nur anzeigen. Das Tool schreibt jetzt direkt in `/var/www/vhosts/system/<domain>/conf/vhost_nginx.conf`:
  - atomar (Temp-Datei im selben Ordner + rename), Besitzer/Rechte wie die bestehende Datei, neue Datei `root:nginx` `0600`
  - danach `nginx -t`; bei Fehler alte Datei zurück, nichts neu geladen
  - erst dann `plesk sbin httpdmng --reconfigure-domain <domain>`, danach Datei, `nginx -t` und Startseite (5xx) prüfen; bei Fehler alte Datei zurück und erneut Reconfigure
  - Domainname strikt geprüft, Pfad nur unter `/var/www/vhosts/system/<domain>/conf/`, keine Symlinks
  - `nginx-directives` muss allein gesetzt werden
- Rollback-Logik: Wurde nichts geschrieben, läuft kein Rollback mehr und es wird "FEHLER - nichts geändert" gemeldet statt "Zurückstellen fehlgeschlagen" (gilt auch für die übrigen Optionen).
- `web_server_settings_rollback` schreibt für `nginx-directives`-Backups den damaligen Dateistand zurück (bzw. entfernt die Datei, wenn es sie damals nicht gab), mit demselben Ablauf aus Test und Reconfigure.

## 0.22

- `web_server_settings_update`: neue Option `nginx-directives` für die zusätzlichen nginx-Anweisungen einer Domain (Plesk "Zusätzliche nginx-Anweisungen", `vhost_nginx.conf`).
  - Whitelist: nur `add_header`, `expires` und `location`-Blöcke, die ausschliesslich `add_header`/`expires` enthalten; Kommentare und Leerzeilen erlaubt, alles andere wird abgelehnt.
  - Neuer Parameter `mode`: `replace` (Standard, kompletter Inhalt) oder `append` (an den bestehenden Inhalt anhängen).
  - Vorschau und Ergebnis zeigen alt/neu als `diff`, mit Hinweis, wenn bestehende Zeilen ersetzt werden.
  - Mit `confirm=true`: `nginx -t` vorher, Backup, Schreiben per `-nginx-directives <datei>`, danach `nginx -t` und HTTP-Prüfung der Startseite; bei Fehler automatisch zurück auf den alten Stand.
- `web_server_settings_rollback` stellt die nginx-Anweisungen nur zurück, wenn das zugehörige Update sie geändert hat.
- `web_server_settings_show` listet `nginx-directives` unter `aenderbar`; der Inhalt steht weiterhin unter `additional nginx settings`.
