FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ./

# Nicht als root laufen (wie infra-mcp, UID 10001). Der Container braucht
# keine Root-Rechte: nur ausgehende SSH-/HTTPS-Verbindungen und Port 8000.
# Gemountete Secrets (PLESK_SSH_KEY_PATH, PLESK_SSH_KNOWN_HOSTS) und ein
# AUDIT_LOG_FILE muessen fuer UID 10001 lesbar bzw. schreibbar sein.
RUN useradd --system --no-create-home --uid 10001 mcp
USER mcp

ENV MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    PYTHONUNBUFFERED=1

# Git-Commit und Version des Builds (von GitHub Actions gesetzt) - get_version
# gibt beides zurueck, damit nach einem Redeploy pruefbar ist, welcher Stand wirklich laeuft.
ARG GIT_SHA=unbekannt
ARG APP_VERSION=
ENV GIT_SHA=$GIT_SHA \
    APP_VERSION=$APP_VERSION

EXPOSE 8000

# /healthz ist ohne Auth erreichbar und prüft nur, ob der Prozess antwortet
# (nicht Plesk/SSH). Kein curl/wget im Image, darum per Python/urllib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('MCP_PORT', '8000') + '/healthz', timeout=4)"]

CMD ["python", "server.py"]
