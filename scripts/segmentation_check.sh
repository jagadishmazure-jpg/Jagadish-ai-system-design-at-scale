#!/usr/bin/env bash
# Run with `docker compose up -d`. Proves the edge (nginx) cannot reach the data tier and the API can.
set -uo pipefail
fail=0
if docker compose exec -T nginx sh -c 'nc -z -w2 redis 6379' 2>/dev/null; then echo "FAIL nginx reached redis"; fail=1
else echo "ok   nginx cannot reach redis"; fi
if docker compose exec -T api-1 python -c "import socket;socket.create_connection(('redis',6379),2)"; then
  echo "ok   api reaches redis"; else echo "FAIL api cannot reach redis"; fail=1; fi
exit $fail
