"""
A chat worker whose agent streams a fixed answer slowly, for the integration test
(tests/chat/test_integration.py). Run from code_atlas/: python tests/chat/stub_worker.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agent  # noqa: E402

CHUNKS = int(os.environ.get("STUB_CHUNKS", "20"))
DELAY = float(os.environ.get("STUB_DELAY", "0.3"))


async def stream_answer(payload, raise_errors=False):
    for i in range(CHUNKS):
        await asyncio.sleep(DELAY)
        yield f"chunk{i} "


agent.stream_answer = stream_answer

from chat.worker import main  # noqa: E402

if __name__ == "__main__":
    asyncio.run(main())
