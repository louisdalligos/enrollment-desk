#!/usr/bin/env bash
# Builds build/lambda.zip: the enrollment_desk package plus Linux wheels of its dependencies.
set -euo pipefail
cd "$(dirname "$0")/.."

rm -rf build/lambda build/lambda.zip
mkdir -p build/lambda
python -m pip install --quiet \
  --platform manylinux2014_x86_64 --implementation cp --python-version 3.12 --only-binary=:all: \
  --target build/lambda "pydantic>=2.6,<3" "openpyxl>=3.1,<4"
cp -r src/enrollment_desk build/lambda/
find build/lambda -name "__pycache__" -type d -prune -exec rm -rf {} +
(cd build/lambda && zip -qr ../lambda.zip .)
echo "built build/lambda.zip ($(du -h build/lambda.zip | cut -f1))"
