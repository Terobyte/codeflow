#!/bin/zsh
set -a
source "${0:A:h}/.env"
set +a
exec npx -y @genwave/svgmaker-mcp
