# Reference provenance

This skill is deliberately source-only. It does not vendor third-party HTML,
PDFs, workflow JSON, model files, screenshots, or licence texts. The prior
cache filenames are recorded below only so an operator can identify their
source. Fetch material from the authoritative publisher when needed and obey
that publisher's current terms.

## Technique and implementation references

| Prior cache name | Authoritative source | Use in this skill | Licensing / retrieval note |
| --- | --- | --- | --- |
| `newgas_2021_classification.html` | [BorisTheBrave, “Classification of Tilesets”](https://www.boristhebrave.com/2021/11/14/classification-of-tilesets/) | Vocabulary for a 16-case vertex-label/marching-squares set. | Copyright remains with the author/site; linked, not copied. |
| `newgas_2023_quarter_tile.html` | [BorisTheBrave, “Quarter-Tile Autotiling”](https://www.boristhebrave.com/2023/05/31/quarter-tile-autotiling/) | Explains quarter-tile composition and distinguishes it from the lookup rule. | Copyright remains with the author/site; linked, not copied. |
| `stalberg_2021_dual_grid_thread.html` | [Oskar Stålberg’s dual-grid post](https://x.com/OskSta/status/1448248658865049605) | Historical pointer for the dual-grid rendering pattern. | Platform post; availability and terms are controlled by X. No copy is included. |
| `hammer_dualgrid_godot_README.md` | [Jess Hammer’s Godot dual-grid system](https://github.com/jess-hammer/dual-grid-tilemap-system-godot) | Example target-engine integration, not a required runtime dependency. | See the upstream repository’s licence before reuse; no code or assets are copied. |
| `pablogila_tilemapdual_README.md` | [pablogila/TileMapDual](https://github.com/pablogila/TileMapDual) | Example Godot dual-grid implementation. | Upstream repository declares MIT; no source or assets are copied. |
| `excalibur_dual_tilemap.html` | No stable canonical URL verified during this harvest. | Historical web-engine example only; it is not needed to run this skill. | Not included or redistributed. Re-add a verified upstream URL before relying on it. |
| `lquesada_inpaint_flux.json` | [lquesada/ComfyUI-Inpaint-CropAndStitch](https://github.com/lquesada/ComfyUI-Inpaint-CropAndStitch) | Shape of the configurable direct-ComfyUI inpaint workflow. | See the upstream repository’s licence and node documentation; no workflow JSON is bundled. |

## AI-generation research references

| Prior cache name | Authoritative source | Scope |
| --- | --- | --- |
| `newgas_2025_diffusion_tilesets.html` | [BorisTheBrave, “Generating Tilesets with Stable Diffusion”](https://www.boristhebrave.com/2025/02/04/generating-tilesets-with-stable-diffusion/) | Useful prior art and limitations discussion. It does not establish a guarantee for this implementation. |
| `madar_fried_2025_tiled_diffusion.pdf` | [Madar & Fried, “Tiled Diffusion,” CVPR 2025](https://arxiv.org/abs/2412.15185), [project code](https://github.com/madaror/tiled-diffusion) | Research on coherent diffusion tiling. It is not used by these scripts. Paper and code have separate upstream terms. |
| `bytedance_2025_onereward.pdf` | No verified canonical source retained. | The source-only skill makes no claim about a particular model or paper. Choose installed model files through `AUTOTILE_*` configuration and follow their licences. |

## Maintenance notes

- Provenance reviewed: 2026-08-11. The listed links were checked except where
  the table explicitly records that no stable source was verified. Availability
  may change. A broken link is not a reason to vendor a copy without permission.
- The skill generates original output only to the extent permitted by the
  selected models, prompts, and operator-provided assets. Those model and asset
  licences are the operator’s responsibility.
- The code implements its own simple 16-case compositor. It does not import
  upstream source code or bundle upstream workflow/model data.
