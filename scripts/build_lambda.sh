#!/usr/bin/env bash
# Portable builder also works directly from Windows PowerShell.
set -euo pipefail
cd "$(dirname "$0")/.."
python scripts/build_lambda.py
