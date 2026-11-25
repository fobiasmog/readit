#!/usr/bin/env bash
# Link /listen (Orpheus) and /listen-say (macOS say) into ~/.claude/skills. The Orpheus model (~2 GB) downloads on first use.
set -euo pipefail
repo="$(cd "$(dirname "$0")" && pwd -P)"
mkdir -p ~/.claude/skills
for s in listen listen-say; do
  ln -sfn "$repo/skills/$s" ~/.claude/skills/$s
  echo "linked ~/.claude/skills/$s -> $repo/skills/$s"
done
