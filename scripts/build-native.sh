#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "$0")/.." && pwd)"
"$root/scripts/build-deb.sh"
cd "$root/frontend"
npm run tauri -- build --bundles deb

