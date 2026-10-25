# STRATA-TUI

A lightweight, standalone control interface for Strata.

STRATA-TUI makes Strata easier to use without adding another heavy management layer. It provides a simple terminal interface for configuring models, starting and stopping Strata, switching models, tuning runtime settings, and viewing logs.

## Features

- Start and stop Strata
- Switch between installed models
- Adjust model loading settings
- Configure Context, Vision, VRAM reserve, and KV cache
- Estimate MoE experts that can fit in VRAM
- Prepare models when required
- View live loading and runtime logs
- Open Chat and Monitor directly
- Automatically use the capabilities of the installed Strata version

## Lightweight by design

STRATA-TUI is intentionally small and uses very little CPU and memory.

Its workflow is:

**configure → load → run → monitor → stop / switch**

It does not replace Strata and does not patch Strata source files. STRATA-TUI stays separate from the main Strata installation, so Strata can be updated normally without maintaining custom modifications inside the Strata repository.

## Start

Windows:

```powershell
.\STRATA-TUI.bat
```

or:

```powershell
python STRATA-TUI.py
```

## Install

```powershell
git clone https://github.com/Ayshinko/strata-tui.git
cd strata-tui
powershell -ExecutionPolicy Bypass -File .\INSTALL.ps1
```

An existing official **Strata source checkout with its Python environment** is required;
a Desktop-only installation is not a replacement for this checkout. INSTALL creates a
Start Menu shortcut; it does not install Strata, models, or Python dependencies.

Before first launch:

```powershell
Copy-Item .\manager.env.example .\manager.env
notepad .\manager.env
```

Set `STRATA_ROOT` to your new official Strata checkout (the directory containing
`setup.py`, `strata.py`, and `serve/`). Do not copy an old `manager.env` blindly after
moving or reinstalling Strata. Run the official Strata setup first and prepare or
reuse your local model through its supported workflow. Start this TUI only after
that environment works. `manager.env` is local and is never committed.

### Before deleting an old installation

A GitHub clone restores **code only**, not your working environment. Keep a verified
backup outside both install directories of:

- Strata model run configs, launch presets/profiles and custom startup scripts;
- model packs/tokenizers and `expert-profile.bin` (or retain the model directory);
- TUI `manager.env`, `manager-models.json`, `.strata-tui.json`, and
  `logs/deepseek-bench.jsonl` if you want the history;
- experimental engines and their dependencies if rollback is needed.

Stop serving through the process owner before uninstalling. Do not delete model
storage or backups when removing program directories. On a fresh install, restore
settings selectively and validate engine paths and supported flags; do not restore
an experimental `--expert-copy-gb` argument into an official engine config.

## Update

```powershell
powershell -ExecutionPolicy Bypass -File .\UPDATE.ps1
```

Strata itself continues to use its own normal update process.

## Model support

The installed Strata checkout is the catalog: `FAMILIES`, `OWN_PATH_FAMILIES`,
`MULTI_FILE_FAMILIES`, and their model tables are read at runtime. This includes
Flash-Next/Qwen, Swift, Coder, Unsloth, Gyro, NVFP4/QAD packs, Qwen3.6 and
DeepSeek-V4.1 when the installed version offers them. There is no separate
quantization whitelist to update in this companion.

The menu is **local inventory**, not a download catalog. It lists saved configs
beside the selected Strata installation, GGUF sets in the selected model folder
and its immediate model subfolders, and catalog-defined local ready-pack inputs.
Families without local files are not added as empty rows. `F` changes the model
folder; `R` refreshes; `U` reveals non-launchable files. Engram and DSpark files
are auxiliaries, not selectable base models.

- Discovery reads headers and file sizes, never hashes or scans weight payloads.
  Existing Flash-Next architecture checks remain in place. Own-engine models
  must match an official file set, pinned sizes, architecture and shard metadata.
- **PREPARE** runs the installed official installer for the exact family/model.
  Official SHA-256 checks, engine acquisition, native packing and smoke checks
  remain enabled. Missing helpers/engine can require downloads; they are not
  evidence that a model is ready. NVFP4 inputs use `--models-dir`, not `--gguf-dir`.
- **READY** requires actual artifacts. DeepSeek uses its own component inventory
  and the official `strata-dsv41 --pack-info` check; it is not checked against
  Qwen pack markers. Qwen3.6 does not require a Flash-Next pack or a separate MTP
  folder (`--mtp 2` is its draft count).
- DeepSeek has native basic/advanced controls (see below); other own-engine
  families keep their saved settings read-only. No own-engine page applies Qwen
  KV splits, vision flags, expert estimates or MTP semantics. Existing Flash-Next
  controls and custom variant identities remain unchanged.
- Running status shows the model actually serving the port. This TUI stops only
  a process it started whose official process identity still matches. A server
  owned by the official Terminal stays running; stop it through that owner.
  Old PID-only records are deliberately not trusted.

## DeepSeek settings

Choose a READY DeepSeek model and press Enter. Performance controls include Context,
RAM expert store, CPU threads, DSpark mode/block/confidence, GPU expert-budget ratio,
cache policy, prefill chunk, memory mapping, Expert Profile and Expert Cache.
Advanced contains VRAM reserve. The official DSV41 0.1.51 engine is supported;
experimental Copy Budget and engine-switch actions are not offered.

Official 0.1.51 does **not** accept `--expert-copy-gb`. Its automatic copy policy
requires sufficient RAM and Windows commit room; explicit mmap/store settings take
precedence. This TUI does not patch the engine, change the page file or promise a
specific copy amount or throughput.

- The RAM cache uses `--ssd-cache`, but the number is **GB of RAM**, not disk
  allocation. GPU expert budget is a **0..1 share of the available expert budget**,
  shown as a percentage; it is not a predicted expert count or total GPU usage.
- DSpark `none` does not load the drafter. `off` disables drafting but **still
  loads the drafter**. Existing `off` remains unchanged
  until explicitly edited. Auto lets the engine decide.
- Enter edits a row; type a value and Enter. Blank keeps the current value.
  Esc, including inside a numeric editor, discards **all** temporary edits.
- **Save** persists without restarting; **Load & Start** explicitly saves and starts
  through the normal load workflow. Reset to Default stages edits until saved.
  Unedited flags/defaults, unknown arguments, user overrides,
  model aliases and the ordinary `.strata-tui.json` are preserved. Finite values
  and documented ranges are checked before saving. An unchanged Load is a no-op.
- Explicit choices use official `user_args`/`args_written` semantics. Matching
  official launch presets are synchronized through their profile-linked managed
  run config and `extra` settings (plus `context` when edited), so the saved
  model-specific Terminal launcher does not revert them on its next launch.
  Match requires family/model, pack identity and the existing backend values;
  different-context/tuning presets and unrelated profiles are not overwritten.
- Before writing, SHA-256-verified originals and a manifest are saved under
  `.deepseek-backups/<unique-id>/` beside the source config. Writes use the
  installed official private atomic writer, check for concurrent changes, and
  roll back earlier writes if a later write fails. These are atomic **per file**,
  not a power-loss-safe cross-file transaction; the backups support recovery.
- If the model is running, saving applies to the **next start** and reports
  **Restart Required**, never that the new values were applied. No model is stopped
  automatically, regardless of owner. Stop/reload deliberately through its owner.
  A pending marker keeps the warning across reopening; an external restart cannot
  be verified from health alone, so the marker is cleared on Load with no server
  running. A different model on the port blocks saving and launching.

## DeepSeek Benchmark

The Benchmark page reads engine logs; it never runs a benchmark automatically.
Save manual snapshots, compare compact history, and press Enter for full details.
`L`, `D`, and `P` mark the latest result, best Decode and best Prefill respectively.
Different workloads/models are not controlled A/B comparisons.

Configured arguments, log-observed Effective values and verified engine Defaults
are distinct. Missing evidence stays Unknown. Loaded GPU expert counts are not the
`--vram-experts` budget ratio. CPU Copy is not physical residency or Working Set.
Windows memory samples require a verified process identity; unavailable samples
stay Unknown, and ambiguous legacy RAM values are marked untrusted in Details.
`psutil` in the chosen Python environment is needed for process sampling; without
it the page remains usable but cannot report process memory.

## Tests

Use Strata's console Python (or this checkout's existing Python environment):

```powershell
python run_tests.py
```

The runner isolates `USERPROFILE`/`HOME`, temp files and each test module's fake
installer imports, then verifies the real `.strata-tui.json` is unchanged. Tests
exercise discovery, preparation/start command routing, backend readiness,
keyboard navigation/repaint and process-ownership guards without GPU loads or
model downloads. Official-family tests use the configured Strata installation.

## Goal

Make Strata easier to configure, launch, tune, monitor, stop, and switch — with minimal overhead and without interfering with Strata updates.
