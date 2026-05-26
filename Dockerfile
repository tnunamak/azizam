FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent state lives in /app/data; mount a volume there.
VOLUME ["/app/data"]

EXPOSE 5000

ENV OPENAI_PROXY_PORT=5000

CMD ["python3", "proxy.py"]
