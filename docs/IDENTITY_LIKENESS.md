# Identity scenes and the likeness check

Two native routes, one CPU sidecar.

| Route | What it does | Cost |
|---|---|---|
| `POST /gateway/v1/faces/likeness` | Scores the faces in `candidate` against the people in the `reference` photos. | CPU, about 2 s warm |
| `POST /gateway/v1/images/identity` | Puts the people from the reference photos into a new scene. Runs up to N seeds, scores each, refines people who still fail, returns the best with its scores. Async media job by default. | GPU, about 2-3 min per attempt |
| `POST /v1/images/edits` with `model=image-identity` | Same pipeline as the route above. Reference photos are the `image[]` parts. Options go in `extensions["com.vividfish.workflow"]`. | same |

Both are typed in `/openapi.json`, listed in `/gateway/v1/workflows` and `/gateway/v1/capabilities`
(`image.edit.image-identity`, `image.analyze.face-likeness`) and in `/v1/models` (`image-identity`).
They use the `openai.images` / `image.edit` policy, so a key that may edit images may use them.

## Why a pipeline and not one edit call

A reference-edit model keeps a face only when it sees a roomy, sharp head per person and the whole
photo, and each input is tagged `<imageN>` in the prompt. With 150 px face tiles and free-text
references it invents a similar-looking stranger. The embeddings the likeness check uses do NOT
generate anything; they only score a finished picture. The picture comes from Qwen-Image 2.1
multi-reference edit, which is given the reference photos as images. The gateway:

1. detects faces in the original photos (or uses the `box` you give);
2. crops roomy square heads from the ORIGINAL pixels and scales them to 896 px;
3. sends the heads (and, optionally, the whole photo as a style canvas) to Qwen-Image 2.1 multi-reference
   edit, every input tagged `<imageN>`;
4. scores each result against the real faces and repeats with the next seed until every person meets the
   threshold (default 3 attempts, at most 8), keeping the best by (extra faces, facing, worst person, mean);
5. **refines** any person still below the match threshold in its own pass (below);
6. returns the best picture with the scores, the attempts, the refinements and any warnings.

### Several reference photos per person

`people[].images` lists every photo of a person: `{"label":"grandma","images":[{"image":0,"box":[x1,y1,x2,y2]},3,4]}`.
Entries that repeat a label merge. The first photo is the anchor (give a box when it holds several
faces). In a later photo without a box the gateway picks the face that looks like the anchor (ArcFace
similarity above 0.25, 0.15 for infants); a photo with nobody who matches is skipped and reported in
`warnings[]` (`reference_skipped`). References are ranked by size x detector score x frontal pose; the best
`max_refs_per_person` (default 3, at most 5; Qwen-Image 2.1 takes 10 images in total) go to the generator.
Every photo is used for scoring: `pooling=best` compares with the closest photo, `pooling=mean` averages
(`match.per_reference` lists each). Mark a photo `generate:false` to use it for scoring only (sunglasses,
profile, tiny face).

### Per-person refinement

One attempt tends to get one face right and the other wrong. After composing, each person below the match
threshold (`refine=auto`; `all` refines everyone, `off` skips) gets a pass: the whole picture is edited with
only that person's references ("the right person in <image1> (a baby) must become the person shown in
<image2>..., keep every other person, pose, clothing, lighting and background"). The result is kept only if
that person's score rises by at least 0.03 and nobody else's falls by more than 0.03 and no extra face
appears. `refinements[]` records every pass with the scores before and after and `accepted`.
A crop-and-paste head refinement was tried first and dropped: the edit model re-poses and re-sizes the head,
so the pasted face sat next to the old one.

### Pose versus likeness

Profile views and foreheads touching remove the features ArcFace needs; a three-quarter view keeps them.
`pose_policy=likeness` (default) rewrites "profile", "side view", "foreheads touching", "nose to nose",
"cheek to cheek" to a three-quarter view with the heads close, adds a sentence asking for visible faces with
both eyes, and returns `warnings[].code == "pose_rewritten"`. `pose_policy=as_written` keeps your words and
warns (`pose_risks_likeness`). A result head turned past |yaw| 0.45 gets a note and a `profile_result` warning.

### Async

`mode=auto` (default) returns `202` and a media job unless `attempts=1` and `refine=off`. Poll
`GET /gateway/v1/media/jobs/{id}` (`status`, `progress` 0-100), then `GET .../content` for the picture and
the scores. The job waits for ComfyUI up to `identity.max_wait_s` (3600) instead of the 300 s image timeout,
so a prompt queued behind a long video is not cancelled. Jobs live in memory: a gateway restart fails them
("submit it again"). `mode=sync` keeps the connection open for the whole run. The `/v1/images/edits` alias
stays synchronous unless `extensions["com.vividfish.workflow"].mode` is `async`.

A head-swap LoRA pass (BFS head V1.1) was also tested and lowered scores, so there is no swap pass.

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
