@echo off
title LMStudioClaw
cd /d "%~dp0"
start "" "%~dp0venv\Scripts\pythonw.exe" -m lmstudioclaw.cli
