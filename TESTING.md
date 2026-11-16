# Blynx Clips verification

## Automated checks

- 24 Python tests cover supported and invalid URLs, provider error handling via mocked responses, message aggregation, independent baselines, warm-up and coverage gaps, single-author flooding, clips from chat bursts at different scales, merged peaks, cooldown, duration caps, synchronization offsets, buffer retention, mixed-platform capture scheduling, validated synthetic MP4 assembly while recording, recovery of an interrupted clip, a real local HLS feed through Streamlink and FFmpeg, credential rejection and secret redaction, scoped single-use pairing and revocation, and conversion of a WebM recording to MP4 with a thumbnail.
- The full synthetic engine smoke test creates an audio/video clip and thumbnail while the source process remains active.
- The Chromium smoke test opens the actual React dashboard, connects to FastAPI by WebSocket, validates a rejected URL, loads the actual unpacked extension, pairs it through the popup, verifies pairing persistence and revocation, then captures manual and automatic highlights in simulation and requests a partial byte range for playback.
- The TypeScript and Vite production build completed. The Debian package was generated and its install manifest inspected.

Repeat the Python checks from the project root after installing requirements-dev.txt:

    export PATH="$PWD/.venv/bin:$PATH"
    python -m pytest
    PYTHONPATH=backend python -u tests/smoke.py

To run the browser smoke check, install Playwright and its Chromium browser. Use a Node resolution path that includes Playwright and set BLYNX_PYTHON to the Python executable with backend dependencies installed:

    export BLYNX_PYTHON="$PWD/.venv/bin/python"
    node tests/browser-smoke.cjs

Reference environment: Debian 12-based Linux runtime, Python 3.12, Node 22, FFmpeg 5.1.9.

## Verification still needed on your setup

Twitch and YouTube production credentials were unavailable. OAuth, platform discovery, live chat, and video acquisition against actual Twitch and YouTube broadcasts need validation with your projects and account. A separate headed Chromium check started tab capture through the browser toolbar, stopped and saved it, and found the resulting MP4 in Highlights. The captured content was a locally generated test page, served in a browser test fixture. The Tauri wrapper was not compiled or installed. KDE service and tray behavior and NVIDIA NVENC were not checked on the target computer.
