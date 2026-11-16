import os

import uvicorn

if __name__ == "__main__":
    os.umask(0o077)
    uvicorn.run(
        "blynx.app:app", host="127.0.0.1", port=int(os.getenv("BLYNX_PORT", "17865")),
        access_log=False, timeout_graceful_shutdown=15,
    )

