# Real-speech seam check (`scripts/stt-seam-check`)

Measures what the long-audio cutter costs a transcription engine on real speech, through the gateway route, against
human transcripts. It is the permanent, re-runnable check behind decision D47 and the engine of the "measure long-audio
settings" action.

## What it does

For each *arm* (a source tree plus a `chunking` block) it starts a disposable gateway on a loopback port (18000-18999;
never the live ports), with one backend: an engine that is already running, reached through a recording tap. Each
recording is sent as it is and again with the first half-target seconds removed, so the second run cuts mid-piece in the
first. It does this at two start offsets (four requests per recording and arm). The engine is only used: the check never
starts, stops or configures it.

Scoring (`gateway/stt_measure.py`, pure functions with tests):

- **Excess near cuts.** Reference words within 2 s of a cut, errors in this run against the same words in the other run,
  where no cut is within 2.5 s. Reference mistakes and hard passages cancel. 95% interval from a 4000-draw bootstrap.
- **Noise floor.** The same estimate at places a quarter target after each cut, away from every cut in both runs.
  A cut effect smaller than the floor's interval cannot be told from nothing.
- **Deletion by piece length.** Share of reference words with no counterpart, inside each piece (2 s at each end left out).
- **Longest piece** the gateway sent, from the tap.

## Run it

```
scripts/stt-seam-check \
  --recordings earnings21/4366522,icsi/Bdb001_first30min,ami/TS3003a,ami/ES2014a \
  --arm current=/path/to/old/tree --arm d47=$PWD \
  --chunking 'current={...old chunking block...}' --chunking 'd47={...new chunking block...}' \
  --engine-port 8017 --model <engine-model-id> --hard-max 40 --out ~/Documents/azizam/stt-seam-eval/runs/$(date +%F)
```

Use the repo's venv Python (it needs `httpx`, `PyYAML` and `uvicorn`). Outputs: `REPORT.md`, `results.json`, `raw/` (per
request: transcript text and the pieces the tap saw; a rerun skips requests already in `raw/`).

It refuses to run when the GPU is busy (utilization above 60% in every one of four samples; `--allow-busy-gpu` skips the
check) or when the engine does not answer `/health`.

## Data

`--data` (default `~/Documents/azizam/stt-eval-data`) holds `<corpus>/<id>/audio.wav` (16 kHz mono) and `ref.json`
(`words[{w,t0,t1}]`, `timing`, `licence`). Audio and transcripts are never stored in the repo. How to fetch and rebuild them
and each licence: `MANIFEST.md` and `scripts/` there. Recordings used: Earnings-21 (CC BY-SA 4.0, interpolated word times),
ICSI Bdb001 first 30 min (CC BY 4.0), AMI TS3003a and ES2014a (CC BY 4.0). Filler words (`uh`, `um`, `mm`, ...) are dropped from both sides.

## Limits

Power is set by the number of cuts (about 60 per half hour and request). The pooled interval is the thing to read; a
single recording's interval is wide. Reference word times from Earnings-21 are spread within speaker turns, so its
windows are less exact than the meeting corpora's.
