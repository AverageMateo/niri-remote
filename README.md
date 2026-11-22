# niri-remote

A lightweight, touch-friendly web control panel and virtual trackpad for [niri](https://github.com/YaLTeR/niri), designed to run from a phone or tablet over Tailscale (or LAN) — ideal alongside Sunshine/Moonlight streaming.

## Features

- **Single-file Python server**: `panel.py` uses only the Python standard library (optional `cryptography` package for WebAuthn passkeys).
- **Niri window & workspace controls**: Launcher (`dms` / `fuzzel` / `wofi`), terminal, overview, focus/move columns, windows, and workspaces, join/split, maximize, fullscreen, float, close, and direct workspace buttons (`1–9`, configurable in the UI).
- **Virtual trackpad**: Fullscreen touch surface streaming over WebSocket (`/ws`) to a virtual `/dev/uinput` mouse — supports 1-finger move/tap-to-click, 2-finger scroll and right-click, drag lock, adjustable sensitivity, and Screen Wake Lock.
- **Display scale control**: Quick presets (`1`–`3`) and `±0.25` stepping for the active niri output.
- **PWA support**: Installable to a phone home screen via `/manifest.webmanifest`.
- **PIN & biometric unlock**: Optional 6-digit PIN with escalating lockouts plus WebAuthn fingerprint/face unlock over HTTPS.

## Requirements

- `niri` and Python 3
- Read/write access to `/dev/uinput` (typically membership in the `input` group) for the trackpad
- Optional: `cryptography` (`python-cryptography` on Arch) and HTTPS (e.g. `tailscale serve`) for fingerprint/face unlock

## Install

```sh
./install.sh
```

Edit `~/.config/niri-remote/profile.env` and set `PANEL_BIND` (for example, your Tailscale IP from `tailscale ip -4`, or `127.0.0.1` if serving via `tailscale serve`), then enable and start the user service:

```sh
./install.sh --restart
```

Existing files in `~/.local/share/niri-remote` and `~/.config/systemd/user` are backed up as `*.bak-<timestamp>` before being overwritten. An existing `~/.config/niri-remote/profile.env` is never overwritten.

The panel listens on port `8765`.

## Configuration (`~/.config/niri-remote/profile.env`)

| Variable | Default | Description |
| --- | --- | --- |
| `PANEL_BIND` | `127.0.0.1` | IP address to bind to (e.g. Tailscale IP) |
| `PANEL_TITLE` | `niri` | Page title and PWA short name |
| `PANEL_NAME` | `PANEL_TITLE` | PWA full name |
| `PANEL_FEATURES` | `scale` | Comma-separated optional features (set empty to hide scale controls) |
| `PANEL_PIN_TIMEOUT` | `600` | Seconds of inactivity before the PIN is requested again |
| `PANEL_PIN_FILE` | `~/.config/niri-remote/pin` | Path to the salted PIN hash |
| `PANEL_PASSKEY_FILE` | `~/.config/niri-remote/passkeys.json` | Path to stored WebAuthn public keys |

## Authentication

> **Warning**: The panel has **no authentication until a PIN is set**. Without a PIN, anyone able to reach `PANEL_BIND:8765` can run niri actions and control the mouse.

### PIN

Set a 6-digit PIN (stored as a salted PBKDF2-SHA256 hash in `~/.config/niri-remote/pin`):

```sh
python3 ~/.local/share/niri-remote/panel.py --set-pin
```

- Takes effect immediately; deleting `~/.config/niri-remote/pin` disables the PIN.
- 5 wrong attempts lock login for 10 min, then 1 h, 6 h, and 24 h on subsequent rounds.
- Sessions and lockouts live in memory (`systemctl --user restart niri-remote` clears both). Use **Configuration → Lock now** to end a session manually.

### Fingerprint / face unlock (WebAuthn)

Requires `cryptography` and HTTPS (e.g. `tailscale serve --bg 8765`):

1. Log in with your PIN.
2. Open **Configuration → Fingerprint unlock → Add this device**.
3. On subsequent visits, the login screen prompts for biometrics automatically (PIN remains available as a fallback).

Public keys are stored in `~/.config/niri-remote/passkeys.json`. Fingerprint logins are not affected by PIN lockouts.
