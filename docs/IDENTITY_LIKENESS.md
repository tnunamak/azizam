# Identity scenes and the likeness check

Two native routes, one CPU sidecar.

| Route | What it does | Cost |
|---|---|---|
| `POST /gateway/v1/faces/likeness` | Scores the faces in `candidate` against the people in the `reference` photos. | CPU, about 2 s warm |
| `POST /gateway/v1/images/identity` | Puts the people from the reference photos into a new scene. Runs up to N seeds, scores each, returns the best with its scores. | GPU, about 2-3 min per attempt |
| `POST /v1/images/edits` with `model=image-identity` | Same pipeline as the route above. Reference photos are the `image[]` parts. Options go in `extensions["com.vividfish.workflow"]`. | same |

Both are typed in `/openapi.json`, listed in `/gateway/v1/workflows` and `/gateway/v1/capabilities`
(`image.edit.image-identity`, `image.analyze.face-likeness`) and in `/v1/models` (`image-identity`).
They use the `openai.images` / `image.edit` policy, so a key that may edit images may use them.

## Why a pipeline and not one edit call

A reference-edit model keeps a face only when it sees a roomy, sharp head per person and the whole
photo, and each input is tagged `<imageN>` in the prompt. With 150 px face tiles and free-text
references it invents a similar-looking stranger. The gateway therefore:

1. detects faces in the original photo (or uses the `box` you give);
2. crops a roomy square head per person from the **original** pixels and scales it to 896 px;
3. sends photo + heads to Qwen-Image 2.1 multi-reference edit, every input tagged `<imageN>`;
4. scores the result against the real faces and repeats with the next seed until every person meets
   the threshold (default 3 attempts, at most 8);
5. returns the best attempt by worst-person score.

A head-swap LoRA pass (BFS head V1.1) after step 3 was tested and lowered both scores (adult 0.605 to
0.51, infant 0.48 to 0.35), so there is no swap pass.

### A ComfyUI input bug this work found

`TextEncodeQwenImage21` takes its reference images as an Autogrow input. In ComfyUI's API format the
keys are flat: `images.image_1`, `images.image_2`. The gateway's Qwen 2.1 edit, multiref and inpaint
workflows used a nested `"images": {"image_1": [...]}` dict. ComfyUI does not resolve that as links, so
the node ran with **no reference images** and every edit became text-to-image ("make the outfit red"
returned an unrelated product photo). Fixed in commit 21d992a; `tests/test_qwen_autogrow_inputs.py`
guards it. This alone explains the "inspired by" faces in the failing session.

## Likeness score

Cosine similarity of ArcFace embeddings, averaged over two recognisers: `buffalo_l` (`w600k_r50`) and
`antelopev2` (`glintr100`), run on CPU in `services/face-service`. Detection is multi-scale SCRFD,
because insightface's single-scale detector misses faces in tight crops. Each reference person is
matched to exactly one candidate face (optimal assignment). `margin` is the score minus the best score
the same candidate face gets against any other reference person.

Verdict per person: `match` at score >= 0.45 and margin >= 0.10; `uncertain` at >= 0.30; else
`mismatch`; `missing` when no candidate face is left. Override with the `thresholds` field. A face the
references do not claim is listed under `extra_faces`.

### Calibration

LFW pairs (adults, 800 pairs: 400 same person, 400 different, seed 0, all detected):

| Recogniser | Same: mean / p5 | Different: mean / p99 / max | TPR at 0.45 | FPR at 0.30 |
|---|---|---|---|---|
| buffalo_l | 0.660 / 0.473 | 0.002 / 0.144 / 0.181 | 0.953 | 0 |
| antelopev2 | 0.690 / 0.514 | 0.002 / 0.151 / 0.180 | 0.968 | 0 |
| mean of both | 0.675 / 0.494 | 0.002 / 0.155 / 0.163 | 0.965 | 0 |

LFW "different" pairs are random strangers, so they are easy negatives. The 0.30 floor leaves room
for relatives and look-alikes; it was not calibrated on hard negatives because no such data was
available locally. Treat `uncertain` as "look at it".

Real family photo (source photo: mother, infant, grandmother):

| Pair | Score |
|---|---|
| Different people in the same photo (mother / infant / grandmother, mean of both models) | 0.01 to 0.12 |
| The failing session's outputs against the real faces (`baby-grandma-ng.png`, `-v2.png`) | -0.10 to 0.17 |
| Qwen edit without working reference images (scene-multi, tagged text only) | 0.00 to 0.29 |
| Qwen edit with working reference images, adult (3 seeds) | 0.60 to 0.62 (buffalo_l) |
| Same, infant (3 seeds) | 0.48 to 0.63 (buffalo_l) |

### Infants

ArcFace-class recognisers are weak on infants (the 2026 infant-recognition paper reports about 31 %
TAR at 0.1 % FAR for 0-6 month olds). Measured here on the repo's own baby photos: edits of one baby
scored 0.21 to 0.96 against the source (style changes move the score a lot), and two babies that may or
may not be the same child scored 0.5 to 0.68 against each other (no ground truth), while three
clearly different babies scored 0.0 to 0.38. So an infant score separates "obviously another baby" from
"same baby", and little else. The route marks infants `reliability: "low"`, never returns `match`
for them (it downgrades to `uncertain`), and sets `needs_human_check`. Use the score to rank seeds and
look at the picture.

## Deploy

1. Sidecar venv: `uv venv ~/applications/face-service/venv --python 3.12 && uv pip install --python
   ~/applications/face-service/venv/bin/python -r services/face-service/requirements.txt`.
2. Models in `/media/windows/AI Models/insightface/models/{buffalo_l,antelopev2}/` (about 1.1 GB; the
   unzipped `buffalo_l.zip` and `antelopev2.zip` from the insightface v0.7 release).
3. `cp services/face-service/face-service.service ~/.config/systemd/user/ && systemctl --user
   daemon-reload`. The gateway starts it on demand (`identity.face_service` in `proxy_config.yml`);
   it exits after 15 idle minutes.
4. Restart the gateway with `scripts/safe-restart`.
