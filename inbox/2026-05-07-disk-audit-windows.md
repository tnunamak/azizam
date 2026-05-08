# Windows partition audit (2026-05-07)

Mountpoint: `/media/windows` (NTFS, fuseblk).
**Total: 64 GB free of 489 GB** (per `df -h /media/windows`).

Audit is read-only. No deletions performed. All sizes via `du -sh` (apparent usage). All paths absolute.

---

## Big picture (top-level breakdown)

| Path | Size | Category |
|---|---|---|
| `/media/windows/AI Models` | 256 GB | Linux-managed model store |
| `/media/windows/hiberfil.sys` | 51 GB | Windows hibernation file |
| `/media/windows/Program Files (x86)` | 47 GB | Windows apps (Overwatch, Steam, etc.) |
| `/media/windows/Windows` | 24 GB | Windows OS |
| `/media/windows/Users` | 23 GB | All user data (only `tnunamak` populated) |
| `/media/windows/Program Files` | 9.8 GB | 64-bit apps + WindowsApps |
| `/media/windows/pagefile.sys` | 8.0 GB | Windows swap |
| `/media/windows/ProgramData` | 3.1 GB | App-shared system data |
| `/media/windows/System Volume Information` | 3.0 GB | Windows restore points / VSS |
| `/media/windows/$WinREAgent` | 1.1 GB | Windows Recovery Environment leftovers |
| `/media/windows/Python312` | 152 MB | Standalone Python install |
| `/media/windows/swapfile.sys` | 16 MB | Windows modern-app swap |
| `/media/windows/$Recycle.Bin` | 1.5 KB | Empty (already emptied) |
| `/media/windows/Recovery` | 4 KB | Empty stub |

`/media/windows/AI Models` is referenced by Linux directly and is **not** part of this culling discussion (it is the legitimate model store).

---

## Definitely-safe-to-delete (from Linux, no Windows-boot impact)

These are clearly orphans, written by Linux on top of NTFS, or so old that Windows-side functionality is moot. Total **~22 GB**.

| Path | Size | Why safe |
|---|---|---|
| `/media/windows/Users/tnunamak/.cache/huggingface/hub/models--TheBloke--Mistral-7B-OpenOrca-GGUF` | 4.1 GB | Orphan HF cache written under the **Windows** user profile. Not referenced by Linux's `~/.cache/huggingface/`. Old GGUF (Mistral-7B OpenOrca, deprecated since Voxtral/etc.). |
| `/media/windows/Users/tnunamak/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2` | 88 MB | Same orphan HF cache, sentence-transformers. |
| `/media/windows/Users/tnunamak/kubuntu-22.04.2-desktop-amd64.iso` | 4.7 GB | Old installer ISO, 22.04.2 (you're now on 24+). |
| `/media/windows/Users/tnunamak/ubuntu.tar` | 1.7 GB | WSL distro export tarball. WSL itself rebuilds from upstream. |
| `/media/windows/Users/tnunamak/ext4.vhdx` | 1.9 GB | WSL2 virtual disk. If you don't boot Windows, useless; if you do boot Windows and use WSL, WSL recreates on first launch. |
| `/media/windows/Users/tnunamak/code` | 3.6 GB | Old Windows-side dev clones, presumably superseded by your Linux `~/code`. **Spot-check before delete** for anything not pushed. |
| `/media/windows/Users/tnunamak/miniconda3` | 318 MB | Windows-side conda install. Independent of your Linux conda/pyenv. |
| `/media/windows/Users/tnunamak/AppData/Local/Programs/balena-etcher` + `balena-etcher-updater` | 237 MB + 148 MB | Per-user app install. Easy to reinstall if needed. |
| `/media/windows/Users/tnunamak/AppData/Local/Programs/frame` + `frame-updater` | 434 MB + 113 MB | Per-user app. |
| `/media/windows/Users/tnunamak/AppData/Local/Programs/mavis-hub` + `mavis-hub-updater` + Roaming `Mavis Hub` + `MavisHub` | ~384 MB | Axie Infinity launcher. Easy reinstall. |
| `/media/windows/Users/tnunamak/AppData/LocalLow/Axie Infinity` | 87 MB | Axie game data. |
| `/media/windows/Users/tnunamak/AppData/LocalLow/Unity` | 475 MB | Unity engine cache from defunct game(s). |
| `/media/windows/Users/tnunamak/AppData/Roaming/TREZOR Bridge` + `Program Files (x86)/TREZOR Bridge` | 73 MB + 16 MB | Trezor Bridge has been deprecated by Trezor itself in favor of Suite. |
| `/media/windows/Users/tnunamak/AppData/Local/Discord` + `Roaming/discord` | 455 MB + 151 MB | Discord cache; rebuilt on launch. |
| `/media/windows/Users/tnunamak/AppData/Local/Mozilla/Firefox` | 1012 MB | Firefox cache (history/cookies survive elsewhere; even if not, you don't use this Firefox). |
| `/media/windows/Users/tnunamak/AppData/Roaming/Mozilla` | 119 MB | Same. |
| `/media/windows/Users/tnunamak/AppData/Local/Microsoft/Terminal Server Client` | 301 MB | RDP client cache. |
| `/media/windows/Users/tnunamak/AppData/Local/Microsoft/OneDrive` | 336 MB | Local OneDrive client cache; the actual cloud copies are intact. |
| `/media/windows/Users/tnunamak/AppData/Local/Microsoft/Edge` | 247 MB | Edge browser cache. |
| `/media/windows/Users/tnunamak/AppData/Local/Steam` | 67 MB | Steam client cache. |
| `/media/windows/Users/tnunamak/AppData/Local/JetBrains/IntelliJIdea2023.3` | 468 MB | Old IDE caches/indexes. IntelliJ rebuilds. |
| `/media/windows/Users/tnunamak/AppData/Roaming/JetBrains` | 753 KB | (already small, listed for completeness) |
| `/media/windows/Users/tnunamak/AppData/Roaming/pypoetry` | 52 MB | Poetry venv cache, Windows-only. |

Total above: **~22 GB**.

Note on HF caches: there are several `.cache/huggingface` subdirs **inside** `/media/windows/AI Models/` (e.g. `Diffusion/diffusion_models/.cache/huggingface`, `LLMs/GGUF/.cache/huggingface`). These are HF lock/metadata files inside your model store and are **not orphans** — leave them.

---

## Probably-safe-to-delete (verify with Tim first)

Total **~50 GB**. These would only matter if you boot Windows and want the app pre-installed.

| Path | Size | mtime / install date | Why suggest |
|---|---|---|---|
| `/media/windows/Program Files (x86)/Overwatch` | 30 GB | 2023-04-28 | Game install. You said you don't boot Windows. Reinstall via Battle.net if you ever want to play again. |
| `/media/windows/Program Files (x86)/Steam/steamapps/common/Fall Guys` | 5.8 GB | 2023-09-01 (Steam dir age) | Game install. |
| `/media/windows/Program Files (x86)/Steam/steamapps/common/3DMark` | 2.7 GB | 2023-09-01 | Benchmark, run-once. |
| `/media/windows/Program Files (x86)/Steam` (entire) | 10 GB total | 2023-09-01 | If killing all Steam games — including the 6 GB Fall Guys + 2.7 GB 3DMark above + 216 MB Steamworks Shared + the Steam client itself (~1 GB). |
| `/media/windows/Program Files (x86)/Epic Games/Launcher` | 1.1 GB | 2023-04-29 | Epic Launcher. Reinstallable. |
| `/media/windows/Program Files (x86)/Epic Games/Epic Online Services` | 254 MB | 2023-04-29 | EOS background service. |
| `/media/windows/Program Files (x86)/Battle.net` | 291 MB | 2023-04-28 | Battle.net launcher (only useful if you keep Overwatch). |
| `/media/windows/Program Files/Axie Infinity - Homeland` | 148 MB | (not stat'd individually; Axie family) | Axie game install. |
| `/media/windows/Program Files (x86)/Microsoft Visual Studio` | 2.9 GB | 2024-02-04 | VS 2022 (likely Community). Useful only if you boot Windows to do .NET work. |
| `/media/windows/Program Files (x86)/Windows Kits` | 1.6 GB | 2024-02 | Windows SDK; pairs with VS. |
| `/media/windows/Program Files (x86)/Microsoft` | 917 MB | 2023-04-28 | Mostly old MS bundled tools. |
| `/media/windows/Program Files/JetBrains` | 3.1 GB | 2024-02 | IntelliJ install. |
| `/media/windows/Program Files/WSL` | 459 MB | 2024-02-04 | WSL2 runtime. Independent of `ext4.vhdx`. Only useful if you boot Windows. |
| `/media/windows/Program Files/Mozilla Firefox` | 470 MB | 2024-08-14 | Firefox install. |
| `/media/windows/Program Files/Git` | 308 MB | 2024-02-04 | Git for Windows. |
| `/media/windows/Program Files/nodejs` | 85 MB | (recent) | Node for Windows. |
| `/media/windows/Program Files/WindowsApps`: Spotify (317M), Teams (254M), Solitaire (75M), Photos (174M), GamingApp (326M) | ~1.1 GB picked items | various 2024 | UWP apps. **Caution:** WindowsApps is locked by NTFS ACL; deleting from Linux may also confuse Windows app registration. Lower priority. |
| `/media/windows/Python312` (root-level) | 152 MB | 2024-02-04 | Standalone Python 3.12 install. |
| `/media/windows/Users/tnunamak/AppData/Local/Packages` | 405 MB | various | UWP per-user app data. Same caution as WindowsApps. |
| `/media/windows/Users/tnunamak/AppData/Local/Microsoft` (excluding subfolders already listed above) | small | — | Edge/OneDrive/RDP already accounted for. |

---

## Boot-Windows-and-reclaim (cannot safely delete from Linux)

Total **~63 GB** sitting in non-Linux-deletable space.

| Path | Size | Reclaim instructions |
|---|---|---|
| `/media/windows/hiberfil.sys` | **51 GB** | Boot Windows → admin PowerShell → `powercfg -h off`. Disables hibernation; the file is removed. Windows will not recreate it. |
| `/media/windows/pagefile.sys` | 8.0 GB | Boot Windows → System Properties → Advanced → Performance Settings → Advanced → Virtual Memory → set "No paging file" for C:, reboot. (Or shrink to e.g. 2 GB.) Windows recreates if not configured. |
| `/media/windows/swapfile.sys` | 16 MB | Linked to pagefile.sys management; disabled via same flow. Negligible. |
| `/media/windows/System Volume Information` | 3.0 GB | Boot Windows → System Protection → delete restore points + Volume Shadow Copies. `vssadmin delete shadows /all` from admin cmd. |
| `/media/windows/$WinREAgent` | 1.1 GB | Leftover from a Windows feature update. Boot Windows → Disk Cleanup → "Previous Windows installation(s)" / "Windows Update Cleanup". May also remove via `cleanmgr /sageset` flow. |
| `/media/windows/Recovery` | 4 KB | Already trivial. Leave. |

**Do not** delete any of these from Linux. NTFS journal will be inconsistent and Windows may flag the volume as dirty on next boot, triggering a repair pass.

The **biggest single win** is `hiberfil.sys` at 51 GB — disabling hibernation alone gets you to ~115 GB free.

---

## Don't touch (Tim's user data, Windows OS, drivers)

These exist; sizes only — **leave alone**.

| Path | Size | Reason |
|---|---|---|
| `/media/windows/Windows` | 24 GB | Windows OS. |
| `/media/windows/ProgramData` | 3.1 GB | System-shared application data. |
| `/media/windows/Users/tnunamak/Documents` | 446 KB | User docs. |
| `/media/windows/Users/tnunamak/Desktop` | 0 B | Empty. |
| `/media/windows/Users/tnunamak/Downloads` | 0 B | Empty. |
| `/media/windows/Users/tnunamak/Pictures`, `Music`, `Videos`, `Saved Games`, `Favorites`, `Links`, `Searches`, `Contacts`, `OneDrive` | <10 KB combined | Empty/near-empty. |
| `/media/windows/Users/tnunamak/.ssh` | 20 KB | SSH keys. |
| `/media/windows/Users/tnunamak/ntuser.dat*`, `NTUSER.DAT*` | ~5 MB | User registry hive. Required for Windows boot. |
| `/media/windows/Program Files/WindowsApps` (system parts) | 4.7 GB total | Mostly system UWP apps; ACL-protected. Leaving the bulk alone is safer than picking through. |
| `/media/windows/Program Files/NVIDIA Corporation` (229 MB) + `Program Files (x86)/NVIDIA Corporation` (199 MB) + `AMD` dirs (104 MB + 115 MB) + `Realtek` (6 MB) + `Alienware` (28 MB) + `Dell` (25 MB) + `Windows Defender*` (~80 MB) | <1 GB combined | Drivers / OEM tools. Required if Windows ever boots. |
| `/media/windows/AI Models/` | 256 GB | Your Linux-side model store; explicitly off-limits per request. |
| `/media/windows/.Trash-1000` | small | Linux GVFS trash from this mount; check first if anything is in it. |

---

## Per-app breakdown — Program Files

64-bit, sorted by size:

| App | Size | mtime |
|---|---|---|
| WindowsApps | 4.7 GB | 2024-08-14 |
| JetBrains (IntelliJ) | 3.1 GB | 2024-02 |
| Mozilla Firefox | 470 MB | 2024-08-14 |
| WSL | 459 MB | 2024-02-04 |
| Git | 308 MB | 2024-02-04 |
| NVIDIA Corporation | 229 MB | 2023-04-28 |
| Axie Infinity - Homeland | 148 MB | (older) |
| AMD | 104 MB | 2023-04-28 |
| nodejs | 85 MB | recent |
| Windows Defender ATP | 77 MB | — |
| Common Files | 47 MB | — |
| Alienware | 28 MB | 2023-09-01 |

x86, top 20:

| App | Size | mtime |
|---|---|---|
| Overwatch | **30 GB** | 2023-04-28 |
| Steam | 10 GB | 2023-09-01 |
| Microsoft Visual Studio | 2.9 GB | 2024-02-04 |
| Windows Kits | 1.6 GB | 2024-02-04 |
| Epic Games (Launcher + EOS) | 1.4 GB | 2023-04-29 |
| Microsoft | 917 MB | 2023-04-28 |
| Battle.net | 291 MB | 2023-04-28 |
| NVIDIA Corporation | 199 MB | 2023-04-28 |
| AMD | 115 MB | 2023-04-28 |
| Reference Assemblies | 94 MB | 2024-02-04 |
| Common Files | 65 MB | — |
| InstallShield Installation Information | 40 MB | — |
| Dell | 25 MB | 2023-04-28 |
| Microsoft SDKs | 24 MB | — |
| TREZOR Bridge | 16 MB | 2024 |
| Microsoft.NET | 8 MB | — |
| Futuremark | 6 MB | 2023-04-29 |
| Realtek | 6 MB | 2023-04-28 |

---

## Per-AppData breakdown — Users/tnunamak/AppData

### Local

| Folder | Size |
|---|---|
| Programs (frame, mavis-hub, balena-etcher, Python) | 1.1 GB |
| Mozilla (Firefox profiles + cache) | 1012 MB |
| Microsoft (OneDrive, RDP, Edge, …) | 961 MB |
| JetBrains (IntelliJ 2023.3 caches) | 468 MB |
| Discord | 455 MB |
| Packages (UWP per-user data) | 405 MB |
| balena-etcher-updater | 148 MB |
| frame-updater | 113 MB |
| mavis-hub-updater | 72 MB |
| Steam (client cache) | 67 MB |
| Package Cache | 29 MB |
| pypa | 23 MB |
| CEF | 20 MB |
| Comms | 18 MB |
| pip | 15 MB |
| AMD | 11 MB |
| node | 7.8 MB |
| NVIDIA | 6.6 MB |

### LocalLow

| Folder | Size |
|---|---|
| Unity | 475 MB |
| Axie Infinity | 87 MB |

### Roaming

| Folder | Size |
|---|---|
| discord | 151 MB |
| Mozilla | 119 MB |
| TREZOR Bridge | 73 MB |
| pypoetry | 52 MB |
| Mavis Hub | 48 MB |
| frame | 17 MB |
| balena-etcher | 2.6 MB |
| Microsoft | 1.7 MB |

---

## HF caches — full inventory under /media/windows

```
/media/windows/Users/tnunamak/.cache/huggingface/hub
  models--TheBloke--Mistral-7B-OpenOrca-GGUF                4.1 GB  ORPHAN — old GGUF, Windows-side cache
  models--sentence-transformers--all-MiniLM-L6-v2            88 MB  ORPHAN

/media/windows/AI Models/huggingface/hub
  models--mistralai--Voxtral-4B-TTS-2603                    7.5 GB  in-use Voxtral model — KEEP

# Inside AI Models (HF metadata sidecars, ~KB each)
/media/windows/AI Models/Diffusion/diffusion_models/.cache/huggingface
/media/windows/AI Models/Diffusion/RMBG/BEN2/.cache/huggingface
/media/windows/AI Models/Diffusion/text_encoders/.cache/huggingface
/media/windows/AI Models/Diffusion/vae/.cache/huggingface
/media/windows/AI Models/Embeddings/nomic-embed-text-v1.5/.cache/huggingface
/media/windows/AI Models/LLMs/Cydonia-24B-v4.3-exl3-6bpw-H6/.cache/huggingface
/media/windows/AI Models/LLMs/GGUF/.cache/huggingface
```

**Net orphan HF reclaim: 4.2 GB** in `/media/windows/Users/tnunamak/.cache/huggingface/`.

---

## Stray model files outside /media/windows/AI Models/

`find` for `*.gguf`, `*.safetensors`, `*.bin`, `*.pt`, `*.pth` (depth 8) returned only:

- `/media/windows/Program Files/Git/usr/share/vim/vim90/tutor/tutor.pt` — vim tutor file, **not a model**.
- `/media/windows/Users/tnunamak/miniconda3/Lib/site-packages/ruamel.yaml-0.17.21-py3.11-nspkg.pth` — Python path file, **not a model**.
- `/media/windows/Users/tnunamak/miniconda3/Lib/site-packages/distutils-precedence.pth` — Python path file, **not a model**.

No stray real model weights outside `/media/windows/AI Models/`.

---

## Recycle Bin / Linux trash

- `/media/windows/$Recycle.Bin` — **1.5 KB** (effectively empty).
- `/media/windows/.Trash-1000` — small (root + last modified Nov 26). Worth peeking before clearing.

---

## Summary of reclaim potential

| Tier | Approx GB |
|---|---|
| Definitely safe (caches, orphan HF, old WSL/ISO/conda, dead launchers) | ~22 GB |
| Probably safe (games + dev installs, with confirmation) | ~50 GB |
| Boot-Windows-only (hiberfil + pagefile + WinRE + restore points) | ~63 GB |
| **Combined ceiling** | **~135 GB** |

If we did all three tiers, the partition would go from 64 GB free → ~199 GB free out of 489 GB (~41 % free).

The single highest-leverage action is `powercfg -h off` from inside Windows: **+51 GB** with no other work.
