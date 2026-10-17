FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DENO_INSTALL=/usr/local

WORKDIR /app

# ffmpeg/ffprobe are used for normalization and stitching; curl installs Deno,
# which current yt-dlp builds can use for YouTube's JavaScript challenge solver.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl ffmpeg unzip \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && test -x /usr/local/bin/deno

COPY requirements.txt ./requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

RUN groupadd --system appuser \
    && useradd --system --gid appuser --home-dir /home/appuser appuser \
    && mkdir -p /app/outputs \
    && chown -R appuser:appuser /app

COPY --chown=appuser:appuser app.py pipeline.py llm_clients.py ./
COPY --chown=appuser:appuser tests ./tests

USER appuser
EXPOSE 8501

CMD ["sh", "-c", "streamlit run app.py --server.address=0.0.0.0 --server.port=${PORT:-8501} --server.headless=true"]
