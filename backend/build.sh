#!/bin/bash
# Build script for Findit Nim backend

set -e

echo "Building Findit backend..."

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

echo "Installing dependencies..."
nimble install -y db_connector

nim c --app:lib --noMain --mm:orc -d:release --opt:speed \
    --outdir:. \
    --out:libfindit_backend.so \
    src/findit_backend.nim

echo "Build complete. Library: $DIR/libfindit_backend.so"
