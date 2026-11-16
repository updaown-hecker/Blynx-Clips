#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "$0")/.." && pwd)"
release="${1:-$root/release}"
mkdir -p "$release"
release="$(realpath "$release")"
if [[ "${BLYNX_USE_BUILT_FRONTEND:-0}" != 1 ]]; then
    (cd "$root/frontend" && npm ci && npm run build)
fi
test -f "$root/frontend/dist/index.html"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
app="$stage/opt/blynx-clips"
install -d "$stage/DEBIAN" "$stage/usr/bin" "$stage/usr/lib/systemd/user" \
    "$stage/usr/share/applications" "$stage/usr/share/icons/hicolor/128x128/apps" "$app/frontend" "$app/scripts"
cp -r "$root/backend" "$root/extension" "$app/"
find "$app" -type d -name __pycache__ -prune -exec rm -rf {} +
cp -r "$root/frontend/dist" "$app/frontend/"
install -m 755 "$root/scripts/blynx-clips" "$app/scripts/blynx-clips"
install -m 644 "$root/scripts/blynx-clips.service" "$stage/usr/lib/systemd/user/"
install -m 644 "$root/scripts/blynx-clips.desktop" "$stage/usr/share/applications/"
install -m 644 "$root/frontend/src-tauri/icons/128x128.png" "$stage/usr/share/icons/hicolor/128x128/apps/blynx-clips.png"
cp "$root/requirements.txt" "$root/README.md" "$root/integrations.example.env" "$app/"
ln -s /opt/blynx-clips/scripts/blynx-clips "$stage/usr/bin/blynx-clips"
cat >"$stage/DEBIAN/control" <<'CONTROL'
Package: blynx-clips
Version: 0.2.0
Architecture: all
Section: video
Priority: optional
Maintainer: Blynx Clips project
Depends: python3 (>= 3.11), python3-venv, ffmpeg, curl, xdg-utils, util-linux, systemd
Recommends: libnotify-bin
Description: Blynx Clips local livestream highlight detector
 Monitors supported Twitch and YouTube streams and turns chat spikes into MP4 clips.
 Includes the local service, Chromium app launcher, and Chrome companion source.
CONTROL
dpkg-deb --root-owner-group --build "$stage" "$release/blynx-clips_0.2.0_all.deb"

