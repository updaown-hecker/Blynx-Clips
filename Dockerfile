FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/index.html frontend/tsconfig.json frontend/vite.config.ts ./
COPY frontend/src ./src
RUN npm run build

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates tzdata fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 blynx && useradd -m -u 10001 -g blynx blynx \
    && install -d -m 0700 -o blynx -g blynx /data /data/credentials
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "faster-whisper>=1.1,<2"
COPY backend /app/backend
COPY --from=frontend /build/dist /app/frontend/dist
ENV PYTHONPATH=/app/backend BLYNX_FRONTEND=/app/frontend/dist BLYNX_DATA_DIR=/data \
    BLYNX_CREDENTIAL_DIR=/data/credentials BLYNX_SERVER_MODE=1 \
    BLYNX_PORT=17866 BLYNX_LOCAL_ORIGIN=http://127.0.0.1:17866
USER blynx
CMD ["python", "-m", "blynx"]
