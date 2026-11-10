"""Barramento de eventos em tempo real (backend → navegador via Server-Sent Events)."""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any


class EventBus:
    def __init__(self, history: int = 300):
        self._subs: set[asyncio.Queue] = set()
        self._recent: dict[str, deque] = {}
        self._history = history
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, kind: str, data: Any) -> None:
        """Pode ser chamado de qualquer thread."""
        msg = {"kind": kind, "time": time.time(), "data": data}
        self._recent.setdefault(kind, deque(maxlen=self._history)).append(msg)
        if self._loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._fanout(msg)
        else:
            self._loop.call_soon_threadsafe(self._fanout, msg)

    def _fanout(self, msg: dict) -> None:
        for q in list(self._subs):
            if q.qsize() > 2000:          # cliente lento: descarta para não travar o servidor
                continue
            q.put_nowait(msg)

    def recent(self, kind: str, n: int = 100) -> list[dict]:
        return list(self._recent.get(kind, []))[-n:]

    async def stream(self):
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        try:
            yield ": conectado\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"event: {msg['kind']}\ndata: {json.dumps(msg, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            self._subs.discard(q)
