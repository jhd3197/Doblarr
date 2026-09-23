# Doblarr — app image (no ML stack; the worker runs dry-run until Demucs/voicebox
# are added). ffmpeg is included for extract/mux/mix. For Demucs, whisperx and
# pyannote on an NVIDIA GPU, use the GPU variant: Dockerfile.gpu, started with
# docker-compose.gpu.yml (see README "GPU").
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY doblarr ./doblarr
COPY web ./web

# Run as a non-root user (uid/gid 1000). Bind-mounted volumes must be writable
# by uid 1000 on the host, e.g.:  mkdir -p config data && chown -R 1000:1000 data
RUN groupadd --gid 1000 doblarr \
    && useradd --uid 1000 --gid doblarr --no-create-home --shell /usr/sbin/nologin doblarr \
    && mkdir -p /config /data \
    && chown -R doblarr:doblarr /app /config /data
USER doblarr

ENV PYTHONUNBUFFERED=1
EXPOSE 6363

# Readiness probe (voicebox reachable + a configured *arr source), not just
# liveness. No curl in python:slim — use urllib.
HEALTHCHECK --interval=30s --timeout=30s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:6363/api/health/ready', timeout=25)" || exit 1

# Config is mounted at /config; data (jobs, kometa fragment, scratch) at /data.
CMD ["python", "-m", "doblarr", "-c", "/config/config.yaml", "serve", "--host", "0.0.0.0", "--port", "6363"]
