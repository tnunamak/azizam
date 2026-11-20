# video.generate.minimax-h3-i2v-hires: workflow notes

Workflow `video.generate.minimax-h3-i2v-hires` (manifest `manifests/workflows/video-minimax-h3-i2v-hires.yml`).

## Description

Two-pass MiniMax H3 first-frame image-to-video with native stereo audio. Pass 1 renders at half the requested size, a learned x2 latent upscaler enlarges that result, and pass 2 refines it at the requested size. The default and largest size is 1216x704; width and height must be multiples of 64, because pass 1 runs at exactly half of them. The longest clip is 124 frames (5.17 s at 24 fps) on the 17N+5 frame grid.

Measured on one RTX 3090: 1216x704 for 124 frames took 195 s, with a peak of 20354 MiB (19.9 GiB) of GPU memory in use on the card, other programs included. A clip of 39 frames (1.6 s) at the same size took 85 s. The single-pass `video.generate.minimax-h3-i2v` took 213 s for 5 s at 960x544.

The checkpoint is `10Eros-Max-Turbo-Beta7-Q4_K_M.gguf`, a third-party merge of MiniMax H3 (loaded with the GGUF loader). It already has the 8-step turbo folded in, so the graph loads no turbo LoRA. The sigma ladders for both passes are fixed in the graph (4 steps, then 3), and the graph sets the sigma shift to 12 for video and 3 for audio, which the merge expects. The workflow has no steps input and no checkpoint input.

The merge is adult-oriented, so the manifest follows `image.generate.krea2-turbo-snofs`: the name says "uncensored" and the description says it only shows adults. The owner puts it in the adult category.

LICENSE: the MiniMax H3 Community License excludes use in the US, EU, UK and South Korea. The merge's card says the standard H3 license applies, and that the licenses of the models it draws on (LTX 2.3, Wan 2.2 and Krea 2) apply to the parts that came from each.

## Second conditioning node

`MiniMaxH3ImageToVideo` builds reference conditioning sized to the latent grid. Pass 2 runs on a grid twice as large, so pass 1's conditioning fails at the sampler with a shape mismatch. The graph therefore has two conditioning nodes, `cond` at half size and `cond2` at the requested size, both fed by the same prompt and picture. Keep both.

## How size and length reach the graph

`width`, `height` and `length` each map to one constant node. Two math nodes (`a // 2`) turn the requested width and height into the pass 1 size inside the graph. The picture is cropped to the requested size before both conditioning nodes, as in the single-pass workflow.

## Parked

`minimax_h3_latent_upscaler_2d_conv_v1_fp16.safetensors` needs a custom node and does not load in core ComfyUI, so no workflow uses it.
