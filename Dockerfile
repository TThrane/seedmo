FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SEEDMO_CONFIG=/config \
    SEEDMO_DOWNLOADS=/downloads \
    SEEDMO_PEER_PORT=6881

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Kører som root i containeren, så den altid kan skrive i NAS'ens delte mapper.
# Vil du hellere køre som en bestemt bruger, så tilføj fx  user: "1000:100"  i docker-compose.yml.
RUN mkdir -p /config /downloads

EXPOSE 8080 6881/tcp 6881/udp
VOLUME ["/config", "/downloads"]

HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/api/auth')" || exit 1

# --timeout-graceful-shutdown giver tid til at gemme resume-data ved stop
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-graceful-shutdown", "15", "--proxy-headers"]
