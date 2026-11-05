#!/bin/sh
set -eu
cd /opt/kangodl-engine/token-provider
export KANGODL_POT_SECRET="$(cat .secret)"
export POT_HOST="${POT_HOST:-127.0.0.1}"
export POT_PORT="${POT_PORT:-8878}"
exec node server.js
