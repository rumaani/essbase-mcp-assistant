"""Optional LLM assist. Any OpenAI-compatible endpoint works (OpenAI, Ollama, LM Studio, vLLM).

The LLM receives a compact view of the context (intents, dimensions, glossary) and returns
JSON: {"intent": "...", "group_by": ["Market"]}. Its output is a hint only; the planner
validates everything against the context file.
"""
from __future__ import annotations

import json

import httpx

from essbase_mcp.config import Settings

PROMPT = """You map questions about an Oracle Essbase cube to an intent.
Intents: {intents}
Dimensions: {dims}
Glossary: {glossary}
Reply with JSON only: {{"intent": "<one intent>", "group_by": ["<dimension>", ...]}}"""


class OpenAICompatible:
    def __init__(self, base_url: str, model: str, api_key: str = ""):
        self.base_url, self.model, self.api_key = base_url.rstrip("/"), model, api_key

    def suggest(self, question: str, ctx: dict) -> dict | None:
        system = PROMPT.format(intents=list(ctx.get("intents", {})),
                               dims=[d["name"] for d in ctx.get("dimensions", [])],
                               glossary=ctx.get("glossary", {}))
        try:
            r = httpx.post(f"{self.base_url}/chat/completions", timeout=30,
                           headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
                           json={"model": self.model, "temperature": 0,
                                 "messages": [{"role": "system", "content": system},
                                              {"role": "user", "content": question}]})
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
            return json.loads(text[text.find("{"): text.rfind("}") + 1])
        except Exception:  # noqa: BLE001
            return None  # fall back to rules


def from_settings(s: Settings):
    if s.llm_provider == "openai_compatible" and s.llm_base_url:
        return OpenAICompatible(s.llm_base_url, s.llm_model, s.llm_api_key)
    return None
