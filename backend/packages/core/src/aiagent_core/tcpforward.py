"""Credential-free TCP forwarder for the published edge port (`python -m aiagent_core.tcpforward 0.0.0.0:8080 api:8080`).

Docker cannot publish a port from an `internal: true` network, and a container on a normal bridge has an outbound
route. So the API stays on internal networks only and this forwarder (no secrets, no DB, no agent code) is the only
service on `edge`. In the Trusted Control VM the host reverse proxy / nftables replace it.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys


async def pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    with contextlib.suppress(Exception):
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    with contextlib.suppress(Exception):
        w.close()


async def main(listen: str, target: str) -> None:
    lh, lp = listen.rsplit(":", 1)
    th, tp = target.rsplit(":", 1)

    async def handle(cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        try:
            ur, uw = await asyncio.wait_for(asyncio.open_connection(th, int(tp)), timeout=10)
        except Exception:
            cw.close()
            return
        await asyncio.gather(pipe(cr, uw), pipe(ur, cw))

    srv = await asyncio.start_server(handle, lh, int(lp))
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
