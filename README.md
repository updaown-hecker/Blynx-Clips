# Blynx Clips 0.2.0

Blynx Clips runs a local service that uses live chat to find highlights while a Twitch or YouTube broadcast is still running. Streamlink and FFmpeg maintain a rolling buffer. FFprobe checks audio, video, and duration before a clip appears as Ready.

## Quick start on KDE Neon

Install the included Debian package from this folder.

    sudo apt install ./release/blynx-clips_0.2.0_all.deb
    blynx-clips

Open **Blynx Clips** from the application menu. The packaged launcher opens a standalone Chromium window when Chrome, Chromium, or Brave is installed. The same dashboard is at **http://127.0.0.1:17865/**. The first run downloads Python runtime packages from PyPI and can take a minute or more. FFmpeg and FFprobe are system dependencies.

In the dashboard, click **Run simulation**. It takes about a minute. The generated test video, audio, and chat are labeled Simulation. Open **Highlights** and play the generated MP4. Sources and credentials are required for live use.

## Linux home server

Run the Docker Compose service on your CasaOS server from an SSH shell. It runs without a desktop session, starts again when Docker starts, and keeps its library, settings and account authorizations in the `blynx-data` volume.

Clone this private repository using a GitHub account authorized to access it, or extract its source ZIP on the server. In the project directory:

```bash
test -e server.env || cp server.env.example server.env
docker compose --env-file server.env up -d --build
docker compose --env-file server.env logs -f
```

From your desktop, forward the server dashboard to your own device:

```bash
ssh -N -L 17866:127.0.0.1:17866 YOUR_USER@YOUR_SERVER_IP
```

While SSH is open, visit [the server dashboard](http://127.0.0.1:17866). In **Settings → Platform connections**, set up Twitch or YouTube once. Twitch accepts the Client ID you already registered. Use **Sign in with Twitch** to authorize this server separately. For YouTube, paste your API key. The service keeps running after you close the browser and SSH tunnel. Look for ready clips in **Highlights** and download them from the dashboard.

Use **Add streamer** in the dashboard after connecting the platform. To follow future broadcasts, paste a channel page such as `https://www.twitch.tv/theirname` or `https://www.youtube.com/@theirchannel`. Use **Streamers** to disable or remove channels. The server saves your list across restarts. Offline channels use no recorder. Twitch checks run about every 45 seconds. YouTube channel checks share the API search budget and can take longer.

`BLYNX_FOLLOW_URL` is optional. Set it only to pin unattended monitoring to one channel and automatically add that channel if it is missing. To switch an existing installation to dashboard management, clear or comment out that variable in `server.env`, then run `docker compose --env-file server.env up -d`.

The Linux container shares the host network and the app binds to port 17866 on loopback by default. The local dashboard assumes users on the host are trusted. The server presets use one recording slot and prefer 720p, falling back to the best available resolution. Headless credentials are stored in permission-restricted files on the server volume. Treat volume backups as sensitive. Use `docker compose --env-file server.env down` to stop the service and keep your library.

Automatic storage reclamation is optional. Set `BLYNX_SERVER_PRUNE=1` in `server.env` before starting the service to remove the oldest unreviewed, non-favorite automatic clips when near the configured storage limit. Manual, reviewed, favorite and exported clips are kept. If protected clips fill the budget, recording pauses until you free space or raise the limit.

### Forward the dashboard through Cloudflare Tunnel

Generate a private dashboard password with `openssl rand -hex 32`. In your existing `server.env`, set `BLYNX_PUBLIC_ORIGIN=https://clips.blynx.vip` (or your chosen HTTPS hostname) and `BLYNX_WEB_PASSWORD` to the generated password. Rebuild with `docker compose --env-file server.env up -d --build`.

On the Cloudflare tunnel running on the CasaOS host, add a published route for the same hostname. Use **HTTP** with the local service `127.0.0.1:17866`. Leave any Origin HTTP Host Header override empty so the app receives the public hostname. If cloudflared runs in Docker bridge mode, use the server's LAN IP as the origin and enable the LAN listener below. A host-network connector can use loopback directly. Enable HTTPS redirects for the public hostname at Cloudflare so the login is always served over HTTPS.

Visit the public HTTPS URL and enter the dashboard password. The public dashboard uses a secure session cookie and supports live updates, clip previews and downloads. Local SSH access remains available and uses the same dashboard password when forwarding is configured. Route public traffic through the HTTPS tunnel.

### Direct access on home Wi-Fi

Set `BLYNX_LAN_IP` to the CasaOS host's private IPv4 address and `BLYNX_LAN_PASSWORD` to a separate password generated with `openssl rand -hex 32`. Run `docker compose --env-file server.env up -d --build`. Open `http://SERVER_LAN_IP:17866` and sign in with the LAN password. The loopback listener stays available. When only LAN access is enabled, the loopback dashboard also uses the LAN password.

Direct LAN access uses HTTP. Use it on a trusted LAN and use the public HTTPS address for setting up platform credentials. If the server's address changes, update `BLYNX_LAN_IP` before restarting.

## Platform setup

Open **Settings → Platform connections**. The app checks pasted credentials with the provider before saving them in KDE Wallet or your system keyring.

- **Twitch** — Register a Public client in the [Twitch Developer Console](https://dev.twitch.tv/console/apps) and enter its Client ID. Choose **Sign in with Twitch**, open the activation page, and enter the displayed code. You can also paste a user access token for the same Client ID with the \`user:read:chat\` scope. Pasted access tokens need manual renewal when they expire.
- **YouTube** — Enable [YouTube Data API v3](https://console.cloud.google.com/apis/library/youtube.googleapis.com) in your Google Cloud project. Paste a YouTube Data API key, or select **OAuth access token** and provide a user token with YouTube read access. Use an API key for ongoing public monitoring, or replace a pasted OAuth token when it expires.
- If the app reports that credential storage is unavailable, unlock KDE Wallet or start your desktop’s Secret Service provider. Existing environment-based installations can continue to use \`integrations.env\`.

Start with a single public broadcast with active chat. For a one-time YouTube broadcast, paste its watch or live URL. To follow future broadcasts, paste a channel handle URL or channel ID URL separately. Twitch channel URLs and VOD links resolve to a broadcaster. Disabled chat, inaccessible video, expired authorization, or exhausted quota appear in the dashboard.

Twitch live discovery uses batched Helix queries about every 45 seconds. Twitch chat uses EventSub WebSocket subscriptions with reconnect and deduplication. YouTube uses Data API discovery and prefers the streaming gRPC chat endpoint. Where compatible, the fallback uses server-provided polling intervals. Channel discovery shares a persistent daily search budget. At default settings, one saved offline YouTube channel is checked about every 16 minutes. More offline channels divide the configured search budget. Direct YouTube video links are checked about every minute, subject to quotas. **Refresh** requests an immediate check within available quota. These interval targets drift when API calls are slow. Key-restricted or nonpublic broadcasts can deny access. Streamlink compatibility also depends on the broadcast and platform.

Official references: [Twitch EventSub](https://dev.twitch.tv/docs/eventsub/handling-websocket-events/), [YouTube streaming chat](https://developers.google.com/youtube/data/live/streaming-live-chat), [YouTube quotas](https://developers.google.com/youtube/data/determine_quota_cost), [Streamlink CLI](https://streamlink.github.io/cli.html).

## Clip timing and scores

The detector scores each five-second window from 0 to 100 based on volume compared with the channel's median, recent variability, relative increase, and unique participation. Each author's contribution is capped at three messages per second. A stable, high-volume chat can trigger on a smaller relative jump when the change is still statistically unusual. Minimum participation, confirmation time, recovery, cooldown and hourly limits continue to govern clip capture. Legacy scores stay labeled with their original scale.

**Similar or busier windows** in Live Monitoring shows how many recent, non-overlapping five-second windows had at least this much effective chat activity. It shows the count after 12 comparable windows have been collected in the current session. This is observed chat frequency, so it cannot estimate the chance that a moment will go viral.

Video capture keeps a rolling segment buffer using stream copies. The first completed segment anchors media time to a local clock. Platform timestamps place incoming chat into one-second buckets with a short delay allowance for late delivery. The per-channel offset shifts the entire requested clip. Negative values move earlier in the buffer. Use a short test broadcast to calibrate against a visible event. Chat reactions, acquisition latency, and source keyframes affect alignment. Reconnect or missing video can produce a labeled partial clip. If neither track is usable, the event is labeled Failed with the reason.

Default clips use up to 20 seconds of pre-roll and 20 seconds after recovery, with a 120-second maximum and a 100-second rolling buffer. FFmpeg encodes only completed clips to H.264/AAC for playback. NVIDIA NVENC is optional for clip creation, with CPU encoding fallback. The source stream is copied into the temporary buffer. One worker failing leaves the other capture workers running. The service rotates the main log and removes expired temporary segments. The storage limit pauses capture and preserves saved clips.

## Editing and export

In **Highlights**, open a clip to play, rename, favorite, mark for review, copy the source link, or download. Set start and end times to export a trim. **1080×1920 center crop** creates a vertical MP4 and retains the recorded audio. The **Original aspect** preset preserves the horizontal layout. Both go into the exports folder under the clip output directory. Import the MP4 into the editor of your choice. Confirm rights to republish the footage before uploading.

## Chrome companion

In Chrome or Chromium 116+, open \`chrome://extensions\`, enable **Developer mode**, and choose **Load unpacked**. Select \`/opt/blynx-clips/extension\` after a Debian installation, or choose this project’s \`extension\` folder. If you already installed 0.1.0, reload the extension and pair again.

In Blynx Clips, open **Chrome Extension → Generate pairing code**. Enter the five-minute, single-use code in the Chrome popup. The dashboard shows **Paired** after redemption. Use **Unpair** in the dashboard to revoke access.

- **Monitor this stream** adds the current Twitch or YouTube page using the configured platform connection.
- **Save up to 30 seconds** uses the existing server-side buffer for an active recording. During warm-up, the popup waits for five seconds of footage before enabling the action.
- **Record this tab** starts an explicit Chrome capture, displays a REC badge, and stops after 60 seconds or when you select **Stop and save tab recording**. The service encodes the recording as MP4 and adds it to Highlights. Tab recording still works when a paired extension can play the stream but Streamlink access is unavailable. Chrome controls whether the tab is capturable. Tab capture uses only the tab selected through the extension and keeps a temporary recording until the service saves it.

## Background service

Closing the Chromium window leaves the user service running. Use **Stop all monitoring** to pause capture and retain data. To stop the service entirely, use systemctl:

    systemctl --user stop blynx-clips.service
    blynx-clips --enable-autostart
    blynx-clips --disable-autostart

The enable and disable commands are alternatives. Paused monitoring stays paused across restarts until resumed. The default data directory is ~/.local/share/blynx-clips, or blynx-clips under XDG_DATA_HOME. The database, output folder, buffers, and rotated logs are stored there by default. Setup diagnostics are in setup.log. Service diagnostics are available with:

    journalctl --user -u blynx-clips.service -n 80
    systemctl --user daemon-reload

If adding a channel reports missing credentials immediately after editing integrations.env, restart the service. If YouTube discovery is slow, check your quota and try a direct video link. If video is stalling, check that the broadcast is still accessible and try 720p, 480p, or worst quality. Use the dashboard activity log for recorder and chat errors. If FFmpeg fails, verify ffmpeg and ffprobe are both available.

## Build and test from source

Requirements: Python 3.11+, FFmpeg/FFprobe, Node 20.19+ or 22.12+, npm, and dpkg-deb for Debian packaging. Node 22 was used for the bundled frontend. Runtime requirements are in requirements.txt and the frontend has an npm lock file.

    python3 -m venv .venv
    .venv/bin/pip install -r requirements-dev.txt
    .venv/bin/python -m pytest
    cd frontend
    npm ci
    npm run build
    cd ..
    scripts/build-deb.sh

To run without installing:

    set -a
    . ./integrations.example.env
    set +a
    export PATH="$PWD/.venv/bin:$PATH"
    export PYTHONPATH="$PWD/backend"
    .venv/bin/python -m blynx

Open http://127.0.0.1:17865/. See TESTING.md for media and browser smoke test commands. For development with separate data, set an absolute BLYNX_DATA_DIR. Run only one backend per data directory. The API and websocket are loopback-only. The dashboard uses a session cookie and same-origin request checks. The paired companion uses a dedicated bearer token scoped to extension operations.

### Optional Tauri build

The native wrapper starts the same installed user service and supports a tray menu where available. The included Debian package uses Chromium app mode. To build the Tauri wrapper on KDE Neon:

    sudo apt install build-essential pkg-config libwebkit2gtk-4.1-dev libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev
    scripts/build-native.sh

Install both the service package in release/ and the native package generated by Tauri in frontend/src-tauri/target/release/bundle/deb/. You also need a current Rust toolchain. The native package depends on the service package. See the [Tauri Linux prerequisites](https://v2.tauri.app/start/prerequisites/#linux).

### Current scope

The archive includes source for all seven dashboard pages, three capture slots, adaptive detection, rolling capture, MP4 and vertical exports, SQLite recovery, systemd integration, the extension companion, and a Debian package with a Chromium launcher. Twitch online/offline discovery uses Helix polling. Automatic chat detection uses the provider APIs. Chrome tab recording is a manual capture path. External editors use manual MP4 import. Native Tauri compilation, live account API compatibility, KDE tray behavior, and NVIDIA encoding require verification on the target setup.

