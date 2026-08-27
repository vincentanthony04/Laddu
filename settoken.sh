#!/usr/bin/env bash
# Project Laddu — set the Upstox access token for the Dockerized backend.
#
# Same operating model as Windows settoken.ps1 (run a script to update the
# token, then restart the service) but writes a plain, owner-only file since
# Windows DPAPI is not available on Linux.
#
# Usage:
#   ./settoken.sh
# or non-interactively:
#   echo "<token>" | ./settoken.sh

set -euo pipefail

SECURE_DIR="${PROJECT_LADDU_SECURE_DIR:-./secure}"
TOKEN_FILE="${PROJECT_LADDU_LINUX_TOKEN_FILE:-${SECURE_DIR}/upstox_token.txt}"

mkdir -p "$SECURE_DIR"
chmod 700 "$SECURE_DIR"

if [ -t 0 ]; then
  read -r -s -p "Paste Upstox access token: " TOKEN
  echo
else
  read -r TOKEN
fi

if [ -z "${TOKEN:-}" ]; then
  echo "Error: empty token" >&2
  exit 1
fi

printf '%s' "$TOKEN" > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"
unset TOKEN

echo "[OK] Token saved to $TOKEN_FILE"
echo "Restarting the laddu-app container..."
docker compose -f infra/compose/docker-compose.yml restart laddu-app
echo "[OK] Done."
