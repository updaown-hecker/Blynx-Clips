import os

import uvicorn

from .server import dashboard_bind

if __name__ == "__main__":
    os.umask(0o077)
    uvicorn.run(
        "blynx.app:app", host=dashboard_bind(), port=int(os.getenv("BLYNX_PORT", "17865")),
        access_log=False, timeout_graceful_shutdown=15,
    )
