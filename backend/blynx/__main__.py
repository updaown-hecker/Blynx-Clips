import errno
import logging
import os
import socket

import uvicorn

from .server import lan_ip

if __name__ == "__main__":
    os.umask(0o077)
    address = lan_ip()
    host = "0.0.0.0" if address == "0.0.0.0" else "127.0.0.1"
    port = int(os.getenv("BLYNX_PORT", "17865"))
    config = uvicorn.Config("blynx.app:app", host=host, port=port,
                            access_log=False, timeout_graceful_shutdown=15)
    server = uvicorn.Server(config)
    if not address or address == host:
        server.run()
    else:
        with socket.create_server((host, port)) as loopback:
            try:
                lan = socket.create_server((address, port))
            except OSError as exc:
                if exc.errno != errno.EADDRNOTAVAIL:
                    raise
                logging.getLogger("uvicorn.error").warning(
                    "BLYNX_LAN_IP=%s is unavailable. Monitoring continues on %s:%s. "
                    "Check the server interface address and restart to enable LAN access.",
                    address, host, port,
                )
                server.run(sockets=[loopback])
            else:
                with lan:
                    server.run(sockets=[loopback, lan])

