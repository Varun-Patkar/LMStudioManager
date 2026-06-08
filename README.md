# LM Studio Manager

A system-tray desktop app that keeps [PI](https://pi.dev) and GitHub Copilot coding agents in sync with whichever local model is running in LM Studio.

## Why

I use PI and GitHub Copilot agents interchangeably and recently started switching to local models via LM Studio. The problem: every time I loaded a different model I had to manually update the model config files for both agents. This tool automates that. It discovers every model LM Studio has, lets me load/unload with one click, and automatically syncs the loaded model into both `~/.pi/agent/models.json` and VS Code's `chatLanguageModels.json` — so I'm never left with a stale or missing model config.

## Features

- Runs in the Windows system tray — close hides to tray, app keeps running until you quit from the tray menu.
- Discovers all available LM Studio models on startup (single API call, no continuous polling).
- One-click load, unload, and warmup.
- Per-model context-length preferences that persist across restarts.
- **Auto-syncs the loaded model** to:
  - `~/.pi/agent/models.json` (PI agent)
  - `%APPDATA%/Code/User/chatLanguageModels.json` (GitHub Copilot / VS Code custom endpoints)
- Dark themed UI with color-coded status rows (green = loaded).

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

## Run on Startup

To launch automatically when Windows starts, add a shortcut to `lmstudio.bat` in your Startup folder:

1. Press <kbd>Win</kbd>+<kbd>R</kbd>, type `shell:startup`, and hit Enter.
2. Right-click inside the folder → **New → Shortcut**.
3. Browse to `lmstudio.bat` in the project directory (or paste the full path).
4. Click **Next**, give it a name, and **Finish**.

The app will now start minimized to the system tray on every login.

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
