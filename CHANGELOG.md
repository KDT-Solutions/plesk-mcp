# Changelog

## 0.22

- `web_server_settings_update`: neue Option `nginx-directives` für die zusätzlichen nginx-Anweisungen einer Domain (Plesk "Zusätzliche nginx-Anweisungen", `vhost_nginx.conf`).
  - Whitelist: nur `add_header`, `expires` und `location`-Blöcke, die ausschliesslich `add_header`/`expires` enthalten; Kommentare und Leerzeilen erlaubt, alles andere wird abgelehnt.
  - Neuer Parameter `mode`: `replace` (Standard, kompletter Inhalt) oder `append` (an den bestehenden Inhalt anhängen).
  - Vorschau und Ergebnis zeigen alt/neu als `diff`, mit Hinweis, wenn bestehende Zeilen ersetzt werden.
  - Mit `confirm=true`: `nginx -t` vorher, Backup, Schreiben per `-nginx-directives <datei>`, danach `nginx -t` und HTTP-Prüfung der Startseite; bei Fehler automatisch zurück auf den alten Stand.
- `web_server_settings_rollback` stellt die nginx-Anweisungen nur zurück, wenn das zugehörige Update sie geändert hat.
- `web_server_settings_show` listet `nginx-directives` unter `aenderbar`; der Inhalt steht weiterhin unter `additional nginx settings`.
