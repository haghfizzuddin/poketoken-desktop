#!/usr/bin/env bash
# Run poketoken from a checkout without installing:  scripts/poketoken.sh app
cd "$(dirname "$(readlink -f "$0")")/.." && exec python3 -m poketoken "$@"
