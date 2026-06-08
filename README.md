# LM Studio Manager

System tray desktop app for managing LM Studio models.

## Features

- Runs in the Windows system tray.
- Open/close behavior:
  - Close button hides window to tray.
  - App keeps running in tray until you quit from tray menu.
- Manual API refresh only:
  - One refresh on startup.
  - Refresh button/menu calls API on demand.
  - No continuous polling loop.
- Model operations:
  - Load selected model.
  - Unload currently loaded model.
  - Warmup loaded model.
- Per-model context preference:
  - Default is model max context.
  - Set exact value per model.
  - Preference persists across restarts.
- Syncs loaded model data to:
  - `~/.pi/agent/models.json`
  - `%APPDATA%/Code/User/chatLanguageModels.json`

## Install

```powershell
python -m venv .venv
. .\.venv\Scripts\Activate.ps1
pip install -e .
```

## Run

```powershell
lmstudio
```

Or use `lmstudio.bat`.

## Controls

- `Refresh` calls LM Studio API once.
- `Set Exact Context` stores per-model context preference.
- `Load Selected` unloads current model (if any), loads selected model with preferred context, then warmup.
- `Unload Loaded` unloads current model.
- `Warmup Loaded` sends a small request to initialize KV cache.

## Config

Connection settings are read from:

- `lmstudio_manager/configs/default.yaml`

Expected fields:

```yaml
lmstudio:
  base_url: "http://localhost:1234/v1"
  api_key: "your-key"
```
