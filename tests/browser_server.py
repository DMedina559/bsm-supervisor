"""Serve the real console against a temporary instance for browser regression tests."""

import argparse
import tempfile
from pathlib import Path

import uvicorn

from bsm_supervisor.config import Config
from bsm_supervisor.web import Console

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=11326)
    parser.add_argument("--origin", default="http://127.0.0.1:11326")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        token = root / "token"
        token.write_text("browser-test-credential-0123456789")
        token.chmod(0o600)
        app = Console(
            Config(root / "state", root / "envs", root / "data"),
            root / "config.toml",
            token,
            args.origin,
        )
        uvicorn.run(
            app, host="127.0.0.1", port=args.port, proxy_headers=False, lifespan="off"
        )
