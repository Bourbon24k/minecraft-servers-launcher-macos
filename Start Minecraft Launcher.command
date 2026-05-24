#!/bin/zsh
cd "$(dirname "$0")" || exit 1
python3 ./minecraft_launcher.py
echo
echo "Launcher closed. Press Enter to exit."
read
