#!/bin/sh
# usage: ./install.sh [--restart]
# Installs niri-remote into ~/.local/share/niri-remote and ~/.config/systemd/user,
# backing up any modified files as *.bak-<timestamp>.
# Creates ~/.config/niri-remote/profile.env from profile.env.example only if missing.
set -e
here=$(cd "$(dirname "$0")" && pwd)
ts=$(date +%Y%m%d-%H%M%S)
put() { # put <src> <dst> [mode]
  mkdir -p "$(dirname "$2")"
  if [ -e "$2" ] && ! cmp -s "$1" "$2"; then cp -p "$2" "$2.bak-$ts"; echo "backup: $2.bak-$ts"; fi
  install -m "${3:-644}" "$1" "$2"; echo "installed: $2"
}

case "${1:-}" in
  ""|--restart) ;;
  *) echo "usage: $0 [--restart]"; exit 2 ;;
esac

d="$HOME/.local/share/niri-remote"
put "$here/panel.py" "$d/panel.py" 755
for i in icon-192.png icon-512.png; do
  put "$here/icons/$i" "$d/$i"
done

cfg="$HOME/.config/niri-remote/profile.env"
if [ ! -e "$cfg" ]; then
  put "$here/profile.env.example" "$cfg"
  echo "Edit $cfg to set PANEL_BIND before starting the service."
else
  echo "kept existing: $cfg"
fi

put "$here/niri-remote.service" "$HOME/.config/systemd/user/niri-remote.service"
systemctl --user daemon-reload

if [ "${1:-}" = "--restart" ]; then
  systemctl --user enable --now niri-remote
  systemctl --user restart niri-remote
fi
