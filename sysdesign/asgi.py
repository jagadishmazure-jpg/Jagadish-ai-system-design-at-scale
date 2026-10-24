"""ASGI entrypoint for uvicorn in docker compose.

All replicas load the same dev signing key (DEV_JWT_KEY_FILE, created by
scripts/dev_token.py and git-ignored) so a token works on any replica.
"""
from __future__ import annotations

import os

from .app import build_app
from .auth import LocalIssuer

_key = os.environ.get("DEV_JWT_KEY_FILE")
app = build_app(replica=os.environ.get("REPLICA", "r1"),
                issuer=LocalIssuer.from_pem_file(_key) if _key else None)
