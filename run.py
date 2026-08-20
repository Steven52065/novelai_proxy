from __future__ import annotations

import uvicorn

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
