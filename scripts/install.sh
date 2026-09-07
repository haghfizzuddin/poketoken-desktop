#!/usr/bin/env bash
# Puts a `poketoken` command in ~/.local/bin (symlink to this checkout) and checks dependencies.
set -e
here="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
python3 -c "import PIL" 2>/dev/null || { echo "Pillow is missing:  pip install --user pillow"; exit 1; }
python3 -c "import tkinter" 2>/dev/null || { echo "tkinter is missing:  sudo apt install python3-tk"; exit 1; }
mkdir -p "$HOME/.local/bin"
ln -sf "$here/scripts/poketoken.sh" "$HOME/.local/bin/poketoken"
echo "installed  ~/.local/bin/poketoken  ->  $here"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "note: ~/.local/bin is not on your PATH yet" ;;
esac
echo "try:  poketoken app"
