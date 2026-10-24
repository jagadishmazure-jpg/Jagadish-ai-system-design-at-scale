#!/usr/bin/env bash
# Self-signed cert for the local nginx edge (git-ignored). Never use outside a laptop.
set -euo pipefail
d=deploy/nginx/certs; mkdir -p "$d"
openssl req -x509 -newkey rsa:2048 -nodes -days 7 -subj "/CN=localhost" \
  -keyout "$d/key.pem" -out "$d/cert.pem" 2>/dev/null
echo "wrote $d/cert.pem"
