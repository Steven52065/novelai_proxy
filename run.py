from __future__ import annotations

import sys
from pathlib import Path

import uvicorn

repo_root = Path(__file__).resolve().parent
sdk_src = repo_root / "novelai-python" / "src"
if sdk_src.exists() and str(sdk_src) not in sys.path:
    sys.path.insert(0, str(sdk_src))

from app.config import load_config


if __name__ == "__main__":
    config = load_config()
    uvicorn.run(
        "app.main:app",
        host=config.server.host,
        port=config.server.port,
        reload=False,
        proxy_headers=True,
        forwarded_allow_ips=",".join(config.security.trusted_proxy_ips),
        timeout_graceful_shutdown=None,
    )
