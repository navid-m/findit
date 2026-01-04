#!/bin/bash
# Build script for Findit Nim backend

set -e

echo "Building Findit backend..."

# Get the directory of this script
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

# Install dependencies
echo "Installing dependencies..."
nimble install -y db_connector

# Build as shared library
nim c --app:lib --noMain --mm:orc -d:release --opt:speed \
    --outdir:. \
    --out:libfindit_backend.so \
    src/findit_backend.nim

echo "Build complete! Library: $DIR/libfindit_backend.so"
