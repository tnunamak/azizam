# OpenClaw Configuration for Azizam

This is an example deployment for one operator's machine. A fresh Azizam install
does not include these engines or user systemd units unless you configure them.

This example provides a unified OpenAI-compatible API on `http://127.0.0.1:5000` with:
- **LLM**: Qwen3-VL-30B (vision-capable, auto-loads on demand)
- **TTS**: Qwen3-TTS 0.6B (~2s latency)
- **STT**: Parakeet TDT 0.6B v3 (20x realtime, 25 languages)

## OpenClaw Settings

### API Configuration
```
Base URL: http://127.0.0.1:5000/v1
API Key: create an `openclaw` App key from `/`, `/setup`, or `/admin`
```

Azizam stores only the API key hash in `admin.db`. Use the printed `opk_...`
value as the OpenAI API key in clients. Proxied `/v1` requests are rejected
without a valid proxy key; `/health` remains public for service checks.

See `AUTH_GUIDE.md` for the agent-facing key generation and rotation guide.
See `MODEL_COMPATIBILITY.md` for how OpenAI model names map to local backends.

Use the dashboard Profile attached to the `openclaw` App to decide which
protocols, models, workflows, and provider-native routes OpenClaw may call.

### Speech-to-Text (STT)
```
STT Engine: OpenAI
STT Model: parakeet-tdt-0.6b-v3
OpenAI Base URL: http://127.0.0.1:5000/v1
```

### Text-to-Speech (TTS)
```
TTS Engine: OpenAI
TTS Model: tts-1
TTS Voice: alloy (or: echo, fable, nova, onyx, shimmer)
OpenAI Base URL: http://127.0.0.1:5000/v1
```

### Chat/Completions
```
Model: my-llama, or choose a configured model from GET /v1/models
```

## Endpoints

| Endpoint | Function |
|----------|----------|
| `POST /v1/chat/completions` | Chat with vision support |
| `POST /v1/audio/speech` | Text-to-speech |
| `POST /v1/audio/transcriptions` | Speech-to-text |
| `GET /v1/models` | List available models |
| `GET /health` | Health check |

## Example services (systemd user services)

```bash
# Check status
systemctl --user status tabbyapi azizam qwen3-tts parakeet-stt

# Restart engines; restart the gateway with scripts/safe-restart
systemctl --user restart tabbyapi qwen3-tts parakeet-stt
scripts/safe-restart

# View logs
journalctl --user -u azizam -f
```

## Performance

| Service | Latency | Notes |
|---------|---------|-------|
| STT | ~200ms | 20x realtime on CPU |
| TTS | ~2s | First token ~100ms |
| LLM | ~5s cold start | Auto-unloads after 5min idle |

## Notes

- LLM model auto-loads on first request, unloads after 5 minutes idle to free VRAM
- In this example deployment, services start automatically on login (user systemd)
- STT runs on CPU (ONNX int8), TTS/LLM use GPU

Use [Safe restart](docs/ops/SAFE_RESTART.md) for gateway restarts.
