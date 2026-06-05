#!/bin/bash
# Sync poi-plugin source to poi's plugins directory.
# Run this after editing poi-plugin/index.js, then restart poi.

SRC="$(cd "$(dirname "$0")/.." && pwd)/poi-plugin"
DST="$HOME/Library/Application Support/poi/plugins/node_modules/poi-plugin-kckit-bridge"

if [ ! -d "$DST" ]; then
  echo "Installing plugin for the first time..."
  cp -r "$SRC" "$DST"
  cd "$DST" && npm install
  echo "Done. Restart poi and enable 'kckit Bridge' in Settings → Plugins."
else
  cp "$SRC/index.js" "$DST/index.js"
  echo "Synced index.js → poi plugins. Restart poi to reload."
fi
