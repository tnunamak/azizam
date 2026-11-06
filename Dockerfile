FROM node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS web-build
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
COPY gateway/product_identity.json gateway/ui_pages.json /app/gateway/
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 /uv /bin/uv
COPY requirements.txt .
COPY tests/requirements-hermetic.lock tests/requirements-hermetic.lock
# Reuse the reviewed dependency closure; do not resolve new versions at build time.
RUN uv pip install --system --no-cache --no-build --require-hashes -r tests/requirements-hermetic.lock

COPY . .
COPY --from=web-build /app/web/out /app/web/out

# Persistent state lives in /app/data; mount a volume there.
VOLUME ["/app/data"]

# Request scratch stays on the image's own disk, not in a data mount another host user may own (REQUEST_RETENTION.md 5.1).
ENV AZIZAM_PRIVATE_TMPDIR=/var/lib/azizam/private-tmp

EXPOSE 5000

CMD ["python3", "proxy.py"]
