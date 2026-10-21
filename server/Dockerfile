FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py cloud.py health.py mcp_server.py ./
COPY dashboard ./dashboard

RUN groupadd --system --gid 10001 likelyread && useradd --system --uid 10001 --gid 10001 likelyread && mkdir -p /data
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status == 200 else 1)"

# The bind-mounted ./data is created by Docker as root, so fix its owner at
# start, then drop privileges for the server process.
# --proxy-headers takes the client IP from X-Forwarded-For, but only from peers
# listed in FORWARDED_ALLOW_IPS (default: none but localhost). The compose file
# trusts its private network, where Caddy is the only peer.
ENV FORWARDED_ALLOW_IPS=127.0.0.1
CMD ["sh", "-c", "chown -R likelyread:likelyread /data && exec setpriv --reuid=10001 --regid=10001 --clear-groups uvicorn app:create_app --factory --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips \"$FORWARDED_ALLOW_IPS\" --no-server-header"]
