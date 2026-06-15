#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

"$ROOT_DIR/scripts/build_app.sh"

cd "$ROOT_DIR/dist"
rm -f MacUnpack.zip
/usr/bin/ditto -c -k --sequesterRsrc --keepParent MacUnpack.app MacUnpack.zip

echo "Packaged $ROOT_DIR/dist/MacUnpack.zip"
