# Doblarr — app image (no ML stack; the worker runs dry-run until Demucs/voicebox
# are added in a future GPU variant). ffmpeg is included for extract/mux/mix.
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY doblarr ./doblarr
COPY web ./web

ENV PYTHONUNBUFFERED=1
EXPOSE 6363

# Config is mounted at /config; data (jobs, kometa fragment, scratch) at /data.
CMD ["python", "-m", "doblarr", "-c", "/config/config.yaml", "serve", "--host", "0.0.0.0", "--port", "6363"]
