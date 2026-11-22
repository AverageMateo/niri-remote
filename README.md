# niri-remote

A lightweight, touch-friendly web control panel, virtual trackpad, and remote keyboard for [niri](https://github.com/YaLTeR/niri), designed to run from a phone or tablet over Tailscale (or LAN) — ideal alongside Sunshine/Moonlight streaming.

## Features

- **Zero-build modular Python server**: Uses only the Python standard library (optional `cryptography` package for WebAuthn passkeys).
- **Live niri status & window switcher**: Top status bar displays the currently focused window and opens an interactive window list (`GET /state`, `POST /focus-window/<id>`).
- **Comprehensive niri controls**:
  - **Apps & system**: Launcher (`dms` / `fuzzel` / `wofi`), terminal, overview, screenshot, and monitor DPMS power off.
  - **Focus & navigation**: Column left/right/first/last, window up/down, workspace up/down, next monitor — with **press-and-hold auto-repeat** on directional buttons.
  - **Column & window layout**: Cycle preset column width, center column, toggle tabbed column display, expand width, maximize, fullscreen, float, and close.
  - **Move**: Move column/window across columns, workspaces, or monitors, and join/split (`consume-or-expel`).
  - **Live workspaces**: Automatically reflects active workspaces on the focused output and highlights the current workspace.
- **Virtual trackpad (`/dev/uinput`)**:
  - Fullscreen touch surface over WebSocket (`/ws`) with `requestAnimationFrame` batching.
  - 1-finger move/tap-to-click, 2-finger scroll and right-click, drag lock, adjustable sensitivity, and Screen Wake Lock.
  - **3-finger niri gestures**: 3-finger tap toggles Overview; 3-finger horizontal/vertical swipes switch columns and workspaces.
  - Built-in quick action bar (`◀ Col`, `Overview`, `Keyboard`, `Col ▶`) inside the trackpad view.
- **Virtual keyboard & media remote**:
  - Type or paste text from your phone's native keyboard, toggle sticky modifiers (`Ctrl`, `Alt`, `Super`, `Shift`), tap special keys (`Esc`, `Tab`, `Bksp`, `Enter`, arrows, `PgUp`/`PgDn`), or trigger common shortcuts (`Ctrl+C`, `Ctrl+V`, `Ctrl+T`, `Ctrl+W`, etc.).
  - Volume (`Vol -`, `Mute`, `Vol +`) and media playback (`Prev`, `Play`, `Next`) via `dms`, `wpctl`, or `playerctl`.
- **Multi-monitor display scale control**: Select any enabled niri output and apply scale presets (`1`–`3`) or `±0.25` steps.
- **PWA + PIN & biometric unlock**: Installable home-screen web app with optional 6-digit PIN (escalating lockouts) and WebAuthn fingerprint/face unlock over HTTPS.

## Repository Layout

```text
niri-remote/
├── panel.py               # HTTP/WebSocket server & niri IPC handlers
├── uinput.py              # Virtual relative mouse & keyboard (/dev/uinput)
├── auth.py                # Salted PBKDF2 PIN auth & WebAuthn passkeys
├── static/
│   ├── index.html         # Main control panel, trackpad & keyboard UI
│   └── login.html         # PIN & WebAuthn login page
├── icons/                 # PWA icons (192x192, 512x512)
├── niri-remote.service    # systemd --user service unit
├── profile.env.example    # Environment configuration template
└── install.sh             # Installer with automatic backups
```

## Requirements

- `niri` and Python 3
- Read/write access to `/dev/uinput` (typically membership in the `input` group) for the virtual trackpad and keyboard
- Optional: `cryptography` (`python-cryptography` on Arch) and HTTPS (e.g. `tailscale serve`) for fingerprint/face unlock

## Install

```sh
./install.sh
```

Edit `~/.config/niri-remote/profile.env` and set `PANEL_BIND` (for example, your Tailscale IP from `tailscale ip -4`, or `127.0.0.1` if serving via `tailscale serve`), then enable and start the user service:

```sh
./install.sh --restart
```

## Configuration (`~/.config/niri-remote/profile.env`)

| Variable | Default | Description |
| --- | --- | --- |
| `PANEL_BIND` | `127.0.0.1` | IP address to bind to (e.g. Tailscale IP) |
| `PANEL_PORT` | `8765` | TCP port to listen on |
| `PANEL_TITLE` | `niri` | Page title and PWA short name |
| `PANEL_NAME` | `PANEL_TITLE` | PWA full name |
| `PANEL_FEATURES` | `scale` | Comma-separated optional features (set empty to hide scale controls) |
| `PANEL_PIN_TIMEOUT` | `600` | Seconds of inactivity before the PIN is requested again |
| `PANEL_PIN_FILE` | `~/.config/niri-remote/pin` | Path to the salted PIN hash |
| `PANEL_PASSKEY_FILE` | `~/.config/niri-remote/passkeys.json` | Path to stored WebAuthn public keys |

## Authentication

> **Warning**: The panel has **no authentication until a PIN is set**. Without a PIN, anyone able to reach `PANEL_BIND:PANEL_PORT` can run niri actions and control mouse/keyboard input.

### PIN

```sh
python3 ~/.local/share/niri-remote/panel.py --set-pin
```

- Takes effect immediately; deleting `~/.config/niri-remote/pin` disables the PIN.
- 5 wrong attempts lock login for 10 min, then 1 h, 6 h, and 24 h on subsequent rounds.
- Sessions and lockouts live in memory (`systemctl --user restart niri-remote` clears both).

### Fingerprint / face unlock (WebAuthn)

Requires `cryptography` and HTTPS (e.g. `tailscale serve --bg 8765`):

1. Log in with your PIN.
2. Open **Configuration → Fingerprint unlock → Add this device**.
3. On subsequent visits, the login screen prompts for biometrics automatically.
