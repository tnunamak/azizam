# OpenClaw Configuration for Local LLM Proxy

This setup provides a unified OpenAI-compatible API on `http://127.0.0.1:5000` with:
- **LLM**: Qwen3-VL-30B (vision-capable, auto-loads on demand)
- **TTS**: Qwen3-TTS 0.6B (~2s latency)
- **STT**: Parakeet TDT 0.6B v3 (20x realtime, 25 languages)

## OpenClaw Settings

### API Configuration
```
Base URL: http://127.0.0.1:5000/v1
API Key: create an `openclaw` App key from `/`, `/setup`, or `/admin`
```

The proxy stores only the API key hash in `admin.db`. Use the printed `opk_...`
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
Model: qwen3-vl-30b (or use default from /v1/models)
```

## Endpoints

| Endpoint | Function |
|----------|----------|
| `POST /v1/chat/completions` | Chat with vision support |
| `POST /v1/audio/speech` | Text-to-speech |
| `POST /v1/audio/transcriptions` | Speech-to-text |
| `GET /v1/models` | List available models |
| `GET /health` | Health check |

## Services (systemd user services)

```bash
# Check status
systemctl --user status tabbyapi openai-proxy qwen3-tts parakeet-stt

# Restart all
systemctl --user restart tabbyapi openai-proxy qwen3-tts parakeet-stt

# View logs
journalctl --user -u openai-proxy -f
```

## Performance

| Service | Latency | Notes |
|---------|---------|-------|
| STT | ~200ms | 20x realtime on CPU |
| TTS | ~2s | First token ~100ms |
| LLM | ~5s cold start | Auto-unloads after 5min idle |

## Notes

- LLM model auto-loads on first request, unloads after 5 minutes idle to free VRAM
- All services start automatically on login (user systemd)
- STT runs on CPU (ONNX int8), TTS/LLM use GPU
