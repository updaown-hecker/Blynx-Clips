import os
import socket

import uvicorn

from .server import lan_ip

if __name__ == "__main__":
    os.umask(0o077)
    host = "127.0.0.1"
    port = int(os.getenv("BLYNX_PORT", "17865"))
    config = uvicorn.Config("blynx.app:app", host=host, port=port,
                            access_log=False, timeout_graceful_shutdown=15)
    server = uvicorn.Server(config)
    if address := lan_ip():
        with socket.create_server((host, port)) as loopback, socket.create_server((address, port)) as lan:
            server.run(sockets=[loopback, lan])
    else:
        server.run()

