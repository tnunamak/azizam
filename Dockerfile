FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 /uv /bin/uv
COPY requirements.txt .
COPY tests/requirements-hermetic.lock tests/requirements-hermetic.lock
# Reuse the reviewed dependency closure; do not resolve new versions at build time.
RUN uv pip install --system --no-cache --no-build --require-hashes -r tests/requirements-hermetic.lock

COPY . .

# Persistent state lives in /app/data; mount a volume there.
VOLUME ["/app/data"]

EXPOSE 5000

ENV OPENAI_PROXY_PORT=5000

CMD ["python3", "proxy.py"]
