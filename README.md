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

An existing Strata installation is required. INSTALL creates a Start Menu shortcut to this checkout; it does not copy files into or modify Strata's source tree.

## Update

```powershell
powershell -ExecutionPolicy Bypass -File .\UPDATE.ps1
```

Strata itself continues to use its own normal update process.

## Goal

Make Strata easier to configure, launch, tune, monitor, stop, and switch — with minimal overhead and without interfering with Strata updates.
