# Face Likeness Check: workflow notes

Workflow `image.analyze.face-likeness` (manifest `manifests/workflows/face-likeness.yml`).

Research and provenance notes for owners and maintainers. No screen or API renders this file.

## Notes moved from the manifest (2026-10-10)

### Description

Objective likeness check: detect the faces in a candidate image and score them against the people in reference photos (ArcFace cosine similarity, buffalo_l and antelopev2 averaged, one-to-one matching, margin against the other people). Returns per-person score, verdict (match, uncertain, mismatch, missing), reliability and the thresholds used. Runs on CPU in the face sidecar, about 2 seconds. Infant faces are flagged reliability=low and never reach "match": ArcFace-class recognisers are weak on infants. Calibration data and thresholds are in docs/IDENTITY_LIKENESS.md.
