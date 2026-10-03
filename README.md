# STRATA-TUI

> A small interface, not another layer.

A minimal terminal launcher for Strata that reads the **installed Strata
checkout's own** model support definitions and GGUF reader instead of
maintaining a separate model compatibility list.

## What this is

One compact, keyboard-driven terminal screen over an existing Strata
checkout:

```
STRATA

○ Stopped

Model folder: E:\Model

> Qwen3.8-Flash-Next Q2_0 · abliterated · 64K    PREPARE
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
- no second compatibility database
- real GGUF metadata verification (Strata's own `tools/gguf_reader.py`)
- follows the currently installed Strata support rules (setup.py's
  `MODELS`, `FAMILIES`, `GGUF_QUANT`, `gguf_dir_shards()`)
- Prepare / Start / Stop open the operation log automatically
- Strata itself remains unchanged

## Install (Windows)

```powershell
cd C:\Users\matri\strata-tui
powershell -ExecutionPolicy Bypass -File .\INSTALL.ps1
```

INSTALL.ps1 copies `STRATA-TUI.py` and `STRATA-TUI.bat` into the Strata
checkout and creates a Start Menu shortcut named **Strata**.

Default paths (all overridable in the settings file `~/.strata-tui.json`):

| | path |
|---|---|
| Strata checkout | `C:\Users\matri\strata` |
| Model folder (Windows) | `E:\Model` |
| Model folder (Linux) | `/mnt/Storage/Model` |

## Update

```powershell
powershell -ExecutionPolicy Bypass -File .\UPDATE.ps1
```

UPDATE.ps1 pulls the latest version, validates it
(`py -3 -m py_compile STRATA-TUI.py`), backs up the installed copy and only
then replaces it.  If anything fails, the previous working copy is kept.

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
  STRATA-TUI.py     the launcher (resolves the Strata root itself)
  STRATA-TUI.bat    Windows launcher (venv -> py -3 -> python)
  INSTALL.ps1       copies the two files into the Strata checkout
  UPDATE.ps1        pull + validate + swap, with rollback
  README.md
  .gitignore
```

## Requirements

- an existing Strata checkout (this launcher has no `setup.py` of its own;
  install it next to Strata's)
- Python 3 (Windows: `py -3`)