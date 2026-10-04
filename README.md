# STRATA-TUI

> A small interface, not another layer.

A minimal terminal launcher for Strata that reads the **installed Strata
checkout's own** model support definitions and GGUF reader instead of
maintaining a separate model compatibility list.

**Single file, fully portable**: the whole launcher is `STRATA-TUI.py`
with zero companion modules — the custom-build (variant, e.g. abliterated)
identity and install logic is inlined.  It never patches official Strata
files, so Strata updates can never conflict with it.

## What this is

One compact, keyboard-driven terminal screen over an existing Strata
checkout:

```
STRATA

○ Stopped

Model folder: E:\Model

> Qwen3.8-Flash-Next Q2_0 · abliterated · 64K    READY
  Qwen3.8-Flash-Next Q2_0 · 64K                  READY

↑/↓ Select
Enter Start/Prepare
R Refresh
X Stop
C Chat
M Monitor
F Folder
U Unsupported
Q Quit
```

- extremely small scope
- keyboard-driven
- zero third-party Python dependencies (plain ANSI terminal codes only)
- no extra server, no proxy, no daemon, no dashboard
- real GGUF metadata verification (Strata's own `tools/gguf_reader.py`)
- follows the currently installed Strata support rules (setup.py's
  `MODELS`, `FAMILIES`, `GGUF_QUANT`, `gguf_dir_shards()`)
- Prepare / Start / Stop open the operation log automatically
- Strata itself remains unchanged

## Custom builds (variants) — inlined, not patched

Official `setup.py` no longer has `--variant`; the variant knowledge (the
`abliterated` label of `strata-q2_0-abliterated.json`, the friendly API id,
the aliases) is **inlined in this file**:

- **identity** — a config's family/size/variant is read from its own
  `model_name` (the same field the official server serves), so
  `strata-q2_0-abliterated.json` shows up as *Qwen3.8-Flash-Next Q2_0 ·
  abliterated* and stays selectable/launchable.
- **prepare** — runs the official `setup.py`, then converts the result into
  the variant's own config + pack (the canonical install is moved aside for
  the run and restored — rename-only, nothing destroyed or duplicated).
- **settings** — a variant's context/vision is edited on its own config file
  directly; its `model_name`/`aliases` are never touched.

## Where the Strata root comes from

The launcher finds the checkout itself, in this order:

1. a `--root DIR` argument (used by the inlined install/settings modes)
2. its own location — dropped into the checkout root (or `tools/`)
3. a `strata-root.txt` file next to it (first line = the checkout path)
4. the `STRATA_ROOT` environment variable

## Install (Windows)

```powershell
cd C:\Users\matri\strata-tui
powershell -ExecutionPolicy Bypass -File .\INSTALL.ps1
```

INSTALL.ps1 copies `STRATA-TUI.py` and `STRATA-TUI.bat` into a Strata
checkout and creates a Start Menu shortcut named **Strata**.  The checkout
is detected automatically (STRATA_ROOT, a sibling `strata` folder, or an
interactive prompt) — pass `-StrataDir <path>` to choose it.

Settings (all overridable in the settings file `~/.strata-tui.json`):

| | path |
|---|---|
| Model folder (Windows) | `E:\Model` |
| Model folder (Linux) | `/mnt/Storage/Model` |

## Update

```powershell
powershell -ExecutionPolicy Bypass -File .\UPDATE.ps1
```

UPDATE.ps1 pulls the latest version, validates it
(`py -3 -m py_compile STRATA-TUI.py`), backs up the installed copy and only
then replaces it.  If anything fails, the previous working copy is kept.
Like INSTALL, the checkout is detected or given with `-StrataDir`.

## How READY is decided

An installed `strata-*.json` config is shown as READY only when the
artifacts the *current* Strata version actually needs exist:

- the engine binary (`cfg["exe"]`)
- the prepared pack's completion markers (`--pack`: `index.txt` +
  `experts.bin`, or `native_experts.txt` for a native pack) and its tokenizer
- the tokenizer directory (`cfg["tokenizer"]`: `vocab.json`, `merges.txt`,
  `token_type.json`)
- every GGUF shard the config points at
- the `--mtp` draft runtime (`dense.txt`, `dense.bin`, `experts.bin`)

These mirror checks Strata itself performs in `setup.py` step 6,
`setup.py start()`, `serve/server.py` and the engine.  Only file existence
is tested - the tens-of-GB payloads are never read for a READY check.

A config whose prepared pack was deleted is **never** shown as READY:

- its raw GGUF still exists  -> one selectable **PREPARE** row; Enter
  re-runs the current `setup.py` (family/model/variant/context are reused,
  e.g. an `abliterated` build stays `abliterated`)
- the raw GGUF is gone too    -> **BROKEN** with the reason shown

## Files

```
strata-tui/
  STRATA-TUI.py     THE launcher - single file, variant logic inlined
  STRATA-TUI.bat    Windows launcher (venv -> py -3 -> python)
  INSTALL.ps1       copies the two files into a Strata checkout
  UPDATE.ps1        pull + validate + swap, with rollback
  README.md
  .gitignore
```

## Requirements

- an existing Strata checkout (this launcher has no `setup.py` of its own;
  run it next to Strata or tell it where the checkout is, see above)
- Python 3 (Windows: `py -3`)