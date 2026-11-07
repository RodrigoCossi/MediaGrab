#!/bin/sh
# MediaGrab installer: creates .venv, installs dependencies and adds menu + desktop shortcuts.
# Usage: ./install.sh            (run again any time to update yt-dlp)
set -e
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$DIR"

for cmd in python3 ffmpeg; do
  command -v "$cmd" >/dev/null || { echo "Missing: $cmd — install it with your package manager first."; exit 1; }
done
command -v node >/dev/null || echo "Note: Node.js not found. YouTube downloads need it (install 'nodejs')."

if command -v uv >/dev/null; then
  [ -x .venv/bin/python ] || uv venv -q .venv
  uv pip install -q -U --python .venv/bin/python -r requirements.txt
else
  [ -x .venv/bin/python ] || python3 -m venv .venv
  .venv/bin/pip install -q -U pip
  .venv/bin/pip install -q -U -r requirements.txt
fi
chmod +x bin/mediagrab-video bin/mediagrab-mp3

APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP="$(command -v xdg-user-dir >/dev/null && xdg-user-dir DESKTOP || echo "$HOME/Desktop")"
mkdir -p "$APPS"

entry() { # id name comment
  cat <<ENTRY
[Desktop Entry]
Type=Application
Name=$2
Comment=$3
Exec=$DIR/bin/$1 %U
Icon=$DIR/desktop/$1.svg
Terminal=false
Categories=AudioVideo;Network;
StartupWMClass=$1
ENTRY
}
for id in mediagrab-video mediagrab-mp3; do
  if [ "$id" = mediagrab-video ]; then
    entry $id "MediaGrab Video Downloader" "Download videos up to 8K from YouTube and 1,800+ sites"
  else
    entry $id "MediaGrab Video to MP3" "Save audio from links or convert video files to MP3"
  fi > "$APPS/$id.desktop"
  chmod +x "$APPS/$id.desktop"
  if [ -d "$DESKTOP" ]; then
    cp "$APPS/$id.desktop" "$DESKTOP/$id.desktop"
    command -v gio >/dev/null && gio set "$DESKTOP/$id.desktop" metadata::trusted true 2>/dev/null || true
  fi
done
echo "MediaGrab installed. Launch it from the app menu, the desktop icons, or:"
echo "  $DIR/bin/mediagrab-video    $DIR/bin/mediagrab-mp3"
