# Strata Manager

The **external** Manager / TUI for your official Strata checkout.  This folder lives
**outside** `C:\AI\Runtime\Strata` and treats that repo as read-only:

```
C:\AI\Runtime\Strata            PURE official Strata (tracks upstream/main; UPDATE.bat)
    └── setup.py, serve/, engine/ …   official code, never patched from here

C:\AI\Tools\Strata-Manager    all Manager/TUI code (this folder)
    ├── manager.env              STRATA_ROOT / STRATA_API / MANAGER_PORT (the only place paths live)
    ├── manager-models.json      friendly API id -> config -> GGUF path -> display name
    ├── manager_config.py        the config loader
    ├── manager_identity.py      the external compatibility layer (identity + variant installs)
    ├── gui/                     the Manager web server (port 8275, proxy, supervisor)
    ├── STRATA-TUI.py/.bat       the minimal terminal launcher
    ├── START-MANAGER.bat        opens the Manager (no console window)
    └── logs/                    the Manager's own runtime files
```

## Why it exists

Strata updates used to conflict with the Manager because the Manager's custom
model-identity code was **patched into official files** (setup.py's `--variant`,
`api_identity`, `saved_aliases`, ...).  With this layout nothing patched remains:

- `setup.py`, `serve/`, `engine/` are exactly upstream.
- The Manager reads the official `setup.py` module *as data* (`import setup` from
  STRATA_ROOT), reads the `strata-*.json` configs, starts `serve/server.py`, and
  proxies the API — official source is never written.
- Custom-build knowledge (the abliterated model's identity, the alias map) lives in
  `manager-models.json` + `manager_identity.py`.

## Start

```
C:\AI\Tools\Strata-Manager\START-MANAGER.bat     # the web Manager on port 8275
C:\AI\Tools\Strata-Manager\STRATA-TUI.bat        # the terminal launcher
```

Update Strata the normal way — it can never touch this folder:

```
cd C:\AI\Runtime\Strata
.\UPDATE.bat
```

## The files

- **manager.env** — `STRATA_ROOT`, `STRATA_API`, `MANAGER_PORT`.  Everything reads
  this; no path is hard-coded elsewhere.
- **manager-models.json** — the external identity map.  Each key is the friendly
  model/API id (`qwen3.8-flash-next-q2_0-abliterated`), with the config file
  (`strata-q2_0-abliterated.json`), the GGUF folder, the display name, the family /
  size / variant label, and the alias list.  The official server already answers
  aliases from the config; this file adds the naming that official `setup.py` no
  longer carries.
- **manager_identity.py** — `reconcile()` merges official `setup.choices_from_config`
  with manager-models.json; `prepare_variant` is the external custom-GGUF install
  (it runs the **official** setup.py and then converts the result to the variant's
  own config + pack, restoring the canonical install); `apply_settings` edits one
  variant config directly; `child_flags` keeps nvidia-smi etc. console-free.
## Install / update launcher

The repository's existing `INSTALL.ps1` and `UPDATE.ps1` remain available for
installing/updating the terminal launcher in a Strata checkout. They only copy
`STRATA-TUI.py` and `STRATA-TUI.bat`; the Manager itself runs from this
repository using `START-MANAGER.bat`.
