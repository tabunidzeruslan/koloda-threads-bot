"""Cloudflare Workers AI text models: a backup brain when the free Gemini quota (about 20 requests a day per model) runs out."""
from __future__ import annotations

from . import net

MODELS = ["@cf/openai/gpt-oss-120b", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"]


class CFText:
    def __init__(self, account: str, token: str):
        self.account, self.token = account, token
        self.model: str | None = None

    def __bool__(self) -> bool:
        return bool(self.account and self.token)

    def chat(self, prompt: str, system: str = "", temperature: float = 0.9) -> str:
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        last: Exception | None = None
        for model in MODELS:
            try:
                res = net.request("POST", f"https://api.cloudflare.com/client/v4/accounts/{self.account}/ai/v1/chat/completions",
                                  json_body={"model": model, "messages": msgs, "temperature": temperature, "max_tokens": 3000},
                                  headers={"Authorization": f"Bearer {self.token}"}, timeout=120)
                text = (res["choices"][0]["message"].get("content") or "").strip()
                if text:
                    self.model = model
                    return text
                last = RuntimeError("порожня відповідь")
            except Exception as e:  # next model
                last = e
        raise RuntimeError(f"Cloudflare AI не відповів: {net.scrub(str(last))[:200]}")
