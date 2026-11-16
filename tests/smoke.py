"""Drive the real supervisor and validate a clip while its source is still recording."""
import asyncio
import tempfile
from pathlib import Path

from blynx.engine import Engine
from blynx.store import Store


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="blynx-smoke-") as root:
        store = Store(Path(root))
        engine = Engine(store)
        engine.running = False
        engine.demo = True
        supervisor = asyncio.create_task(engine.supervisor())
        try:
            async with asyncio.timeout(95):
                while True:
                    if supervisor.done():
                        supervisor.result()
                    clip = store.item("SELECT * FROM clip ORDER BY onset DESC LIMIT 1")
                    if clip and clip["status"] == "failed":
                        raise RuntimeError(clip["reason"])
                    if clip and clip["status"] == "ready":
                        assert engine.demo and not engine.workers["simulation"][0].done()
                        assert 1 <= clip["duration"] <= 35 and clip["thumbnail"]
                        print(f'PASS: {clip["duration"]:.3f}s MP4, thumbnail, source still recording', flush=True)
                        return
                    if any(act["level"] == "error" for act in engine.activities):
                        raise RuntimeError(engine.activities[0])
                    await asyncio.sleep(1)
        finally:
            await engine.shutdown()
            await supervisor


if __name__ == "__main__":
    asyncio.run(main())

