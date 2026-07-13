#!/usr/bin/env bash
# Однократная регенерация вендор-бандла pipecat JS SDK (UI v2, S26).
# Требует node>=18 с npm. Результат КОММИТИТСЯ; serve-путь остаётся без сборки.
# Факт план-фазы: сырые ESM-бандлы несут bare-specifier'ы (bowser/events/uuid/
# daily-js/lodash) — поэтому bundle, а не import-map.
set -euo pipefail
CLIENT_JS_VERSION="1.12.0"
TRANSPORT_VERSION="1.10.5"
ESBUILD_VERSION="0.25.5"
OUT="$(cd "$(dirname "$0")/.." && pwd)/synapse/pipeline/client/vendor/pipecat.mjs"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cd "$TMP"
npm init -y >/dev/null
npm install --no-audit --no-fund \
  "@pipecat-ai/client-js@${CLIENT_JS_VERSION}" \
  "@pipecat-ai/small-webrtc-transport@${TRANSPORT_VERSION}" >/dev/null
printf '%s\n%s\n' \
  'export { PipecatClient, RTVIEvent } from "@pipecat-ai/client-js";' \
  'export { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";' > entry.js
mkdir -p "$(dirname "$OUT")"
npx --yes "esbuild@${ESBUILD_VERSION}" entry.js --bundle --format=esm \
  --platform=browser --minify --outfile="$OUT"
echo "OK: $OUT"
