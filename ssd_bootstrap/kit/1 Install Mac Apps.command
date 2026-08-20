#!/bin/zsh
SCRIPT_DIR="${0:A:h}"
exec "$SCRIPT_DIR/.terranash-bootstrap.command" --mac-apps "$@"
