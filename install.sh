#!/usr/bin/env bash
set -e

python -m venv venv

if [ -f "venv/Scripts/python.exe" ]; then
  venv/Scripts/python.exe -m pip install --upgrade pip
  venv/Scripts/python.exe -m pip install -r requirements.txt
else
  venv/bin/python -m pip install --upgrade pip
  venv/bin/python -m pip install -r requirements.txt
fi
