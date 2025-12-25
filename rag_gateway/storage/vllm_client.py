from __future__ import annotations

import httpx
from typing import Any, Dict


class VLLMClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def stream_chat_completions(self, payload: Dict[str, Any]):
        url = f"{self.base_url}/v1/chat/completions"
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", url, json=payload) as r:
                r.raise_for_status()
                async for chunk in r.aiter_bytes():
                    yield chunk

    async def chat_completions(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/v1/chat/completions"
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            return r.json()

