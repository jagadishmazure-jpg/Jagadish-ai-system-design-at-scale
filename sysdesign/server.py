"""Process entry point for containers: `python -m sysdesign.server` (uvicorn, one worker)."""
import os

import uvicorn

from .app import create_app

app = create_app(replica=os.environ.get("REPLICA", os.environ.get("HOSTNAME", "api")))

if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("BIND", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")),  # noqa: S104
                timeout_keep_alive=30)
