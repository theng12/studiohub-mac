#!/bin/zsh
SCRIPT_DIR="${0:A:h}"
exec /usr/bin/python3 "$SCRIPT_DIR/repair_startup.py" "$@"
