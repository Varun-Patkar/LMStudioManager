@echo off
title LM Studio Model Manager
cd /d "D:\Projects\LMStudioEval"
call .venv\Scripts\activate.bat
start "" pythonw -m lmstudio_manager.cli
