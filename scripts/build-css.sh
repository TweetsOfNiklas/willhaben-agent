#!/bin/sh
# Builds app/static/app.css (Tailwind CSS + daisyUI) for the v2 templates (UI prototype, app/templates/v2).
# No Node: Tailwind's standalone binary plus daisyUI's plugin files, downloaded once into build/ (gitignored).
# The built CSS is committed, so the Docker image needs no build step. Run after changing classes in v2 templates.
# Usage: scripts/build-css.sh [--watch]
set -e
cd "$(dirname "$0")/.."
TAILWIND=v4.3.3
DAISYUI=v5.7.42
DAISYUI_SHA=eb3ec02c7e911b077753de1f2eaa868e4bca5fe57278d5d917c54e0d026b769b
DAISYUI_THEME_SHA=7dfeb9bc77bce72602028b147897fecbcf36aadaf22bf26233c94aacd4d0adab
DIR=build/css-tools

case "$(uname -s)-$(uname -m)" in
  Darwin-arm64) BIN=tailwindcss-macos-arm64 ;;
  Darwin-x86_64) BIN=tailwindcss-macos-x64 ;;
  Linux-aarch64|Linux-arm64) BIN=tailwindcss-linux-arm64 ;;
  Linux-x86_64) BIN=tailwindcss-linux-x64 ;;
  *) echo "unsupported platform $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

sha() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }
fetch() {  # url file sha256
  [ -f "$2" ] && [ "$(sha "$2")" = "$3" ] && return
  curl -fsSL -o "$2.part" "$1"
  [ "$(sha "$2.part")" = "$3" ] || { echo "checksum mismatch: $1" >&2; rm -f "$2.part"; exit 1; }
  mv "$2.part" "$2"
}

mkdir -p "$DIR"
TW_URL=https://github.com/tailwindlabs/tailwindcss/releases/download/$TAILWIND
[ -f "$DIR/sha256sums-$TAILWIND.txt" ] || curl -fsSL -o "$DIR/sha256sums-$TAILWIND.txt" "$TW_URL/sha256sums.txt"
TW_SHA=$(grep " \*\{0,1\}\./\{0,1\}$BIN\$" "$DIR/sha256sums-$TAILWIND.txt" | cut -d' ' -f1)
[ -n "$TW_SHA" ] || { echo "no checksum for $BIN" >&2; exit 1; }
fetch "$TW_URL/$BIN" "$DIR/tailwindcss-$TAILWIND" "$TW_SHA"
chmod +x "$DIR/tailwindcss-$TAILWIND"
DU_URL=https://github.com/saadeghi/daisyui/releases/download/$DAISYUI
fetch "$DU_URL/daisyui.mjs" "$DIR/daisyui.mjs" "$DAISYUI_SHA"
fetch "$DU_URL/daisyui-theme.mjs" "$DIR/daisyui-theme.mjs" "$DAISYUI_THEME_SHA"

exec "$DIR/tailwindcss-$TAILWIND" -i app/styles/app.css -o app/static/app.css --minify "$@"
