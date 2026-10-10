# audio.generate.yue2-song: workflow notes

Workflow `audio.generate.yue2-song` (manifest `manifests/workflows/yue2-song.yml`).

## Notes moved from the manifest (2026-10-10)

### Description

Full song with vocals from a style prompt (tags) and lyrics (YuE2-3B int8_convrot). POST /v1/audio/generate with tags, lyrics, duration. duration is a MAXIMUM in seconds, not an exact length: the model decides the real length, usually shorter. About 50-60 s for a 2-minute song. Output is MP3. LICENSE: CC BY-NC (non-commercial).

### Original manifest text

```yaml
description: 'Full song with vocals from a style prompt (tags) and lyrics (YuE2-3B int8_convrot). POST
  /v1/audio/generate with tags, lyrics, duration. duration is a MAXIMUM in seconds, not an exact length:
  the model decides the real length, usually shorter. About 50-60 s for a 2-minute song. Output is MP3.
  LICENSE: CC BY-NC (non-commercial).'
```
