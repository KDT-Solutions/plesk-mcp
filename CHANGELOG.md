# Changelog

## 0.26

- Neue Tools `wp_custom_css_show`, `wp_custom_css_update` und `wp_custom_css_rollback` für das "Zusätzliche CSS" des WordPress-Customizers (Design > Customizer > Zusätzliches CSS, `custom_css`-Post je Theme). Damit landet CSS dort, wo es auch im Backend sichtbar und editierbar ist, statt als mu-Plugin oder Theme-Datei.
  - Läuft wie `wp_option_update` über WP-CLI als System-User der Subscription (nie root): `wp eval` mit festem PHP-Code (`wp_get_custom_css`/`wp_update_custom_css_post`), das CSS wird per stdin übergeben und steht nie im Code; der Theme-Name nur nach Regex-Prüfung als Literal. WordPress legt dabei wie im Customizer eine Revision an.
  - `mode`: `replace` (Standard) oder `append`; `stylesheet` leer = aktives Theme, sonst Theme-Verzeichnis (Hinweis, wenn es nicht das aktive ist).
  - Ohne `confirm=true` nur Vorschau mit Unified-Diff alt/neu; mit `confirm=true` vorher JSON-Backup des alten CSS unter `/var/www/vhosts/<domain>/.plesk-mcp/wp-custom-css-backups/<backup_id>.json` (0600, Subscription-User), dann Schreiben, erneut Lesen und Kontrolle, danach `wp cache flush` (best effort). Grenzen: 512 KB, kein Nullbyte, kein `</style`.
  - `wp_custom_css_rollback(backup_id)` stellt das damalige komplette CSS wieder her und legt vorher selbst ein Backup an.
- Neue Tools `wp_comment_list` und `wp_comment_moderate` zur Kommentar-Moderation per WP-CLI (z.B. Spam-Kommentare aus "Bitte moderiere"-Mails erledigen). `wp_comment_list` listet Kommentare nach Status (`hold`/`approve`/`spam`/`trash`/`all`, max. 200, Textsuche, Inhalt auf 300 Zeichen gekürzt) plus Zähler. `wp_comment_moderate` führt `spam`, `trash`, `approve`, `unapprove`, `unspam`, `untrash` oder `delete` (endgültig, `--force`) für 1-200 IDs aus - ohne `confirm=true` nur Vorschau mit Autor/E-Mail/Inhalt/Status, mit `confirm=true` Ausführung als Subscription-User und Statuskontrolle je Kommentar danach; WordPress-Hooks (Akismet) laufen mit.
- Neue Tools `wc_coupon_list`, `wc_coupon_get`, `wc_coupon_create`, `wc_coupon_update`, `wc_coupon_rollback` und `wc_coupon_delete` für WooCommerce-Gutscheine über die WooCommerce-CLI (`wp wc shop_coupon`, mit `--user` = erster Administrator der Installation, damit die WooCommerce-Validierung und -Hooks greifen). Felder gegen eine Whitelist geprüft (Code, Typ `percent`/`fixed_cart`/`fixed_product`, Betrag, Ablaufdatum, Limits, Produkt-/Kategorie-IDs, E-Mail-Einschränkungen, Status). `create` prüft vorher, ob der Code schon existiert; `update` zeigt alt/neu je Feld, legt ein JSON-Backup unter `/var/www/vhosts/<domain>/.plesk-mcp/wc-coupon-backups/` an und kontrolliert danach; `delete` verschiebt in den Papierkorb, `permanent=true` löscht endgültig (`--force`), jeweils mit Backup. Alle schreibenden Tools ohne `confirm=true` nur Vorschau.
- Tests: `tests/test_wp_custom_css.py`, `tests/test_wp_comment.py`, `tests/test_wc_coupon.py` (Validierung ohne Serverzugriff).

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
