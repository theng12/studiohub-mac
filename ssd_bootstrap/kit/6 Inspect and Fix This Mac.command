#!/bin/zsh
set -e
set -u
set -o pipefail

SCRIPT_DIR="${0:A:h}"

if (( $# > 1 )) || (( $# == 1 )) && [[ "$1" != "--dry-run" ]]; then
  print -u2 -- "Usage: ${0:t} [--dry-run]"
  exit 2
fi

if (( $# == 1 )); then
  /usr/bin/python3 "$SCRIPT_DIR/runtime_state_migration.py" \
    --dry-run --update-current --preserve-machine-environment
  "$SCRIPT_DIR/.terranash-bootstrap.command" \
    --models --dry-run --action restore --prune
  /usr/bin/python3 "$SCRIPT_DIR/repair_startup.py" --dry-run
  exit 0
fi

/usr/bin/python3 "$SCRIPT_DIR/runtime_state_migration.py" \
  --dry-run --update-current --preserve-machine-environment
"$SCRIPT_DIR/.terranash-bootstrap.command" --models --action restore --prune
/usr/bin/python3 "$SCRIPT_DIR/runtime_state_migration.py" \
  --update-current --preserve-machine-environment
/usr/bin/python3 "$SCRIPT_DIR/repair_startup.py"
