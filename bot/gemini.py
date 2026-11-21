"""Gemini API client with automatic model choice (models come and go)."""
from __future__ import annotations

import json
import re
import time

from . import net

BASE = "https://generativelanguage.googleapis.com/v1beta"
BAD = ("image", "tts", "audio", "live", "embedding", "vision", "learnlm", "gemma", "robotics", "computer")


def rank_models(names: list[str]) -> list[str]:
    """Best text 'flash' models first: newest version, stable before preview, full before lite."""
    def key(n: str):
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        ver = float(m.group(1)) if m else 0.0
        unstable = any(w in n for w in ("preview", "exp", "-0", "latest"))
        lite = "lite" in n
        return (-ver, unstable, lite, n)
    pool = [n for n in names if "gemini" in n and "flash" in n and not any(b in n for b in BAD)]
    return sorted(pool, key=key)


class Gemini:
    def __init__(self, key: str, preferred: str | None = None):
        self.key = key
        net.register_secret(key)
        self.preferred = preferred
        self.model: str | None = None
        self._models: list[str] | None = None

    def _h(self) -> dict:
        return {"x-goog-api-key": self.key}

    def list_models(self) -> list[str]:
        if self._models is None:
            names, token = [], None
            for _ in range(5):
                res = net.request("GET", f"{BASE}/models", params={"pageSize": 200, **({"pageToken": token} if token else {})},
                                  headers=self._h())
                for m in res.get("models", []):
                    if "generateContent" in m.get("supportedGenerationMethods", []):
                        names.append(m["name"].removeprefix("models/"))
                token = res.get("nextPageToken")
                if not token:
                    break
            ranked = rank_models(names)
            if self.preferred in ranked:
                ranked.remove(self.preferred)
                ranked.insert(0, self.preferred)
            self._models = ranked
        return self._models

    def generate(self, prompt: str, *, system: str = "", json_mode: bool = False, search: bool = False,
                 temperature: float = 0.9) -> str:
        body: dict = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                      "generationConfig": {"temperature": temperature}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if json_mode and not search:
            body["generationConfig"]["responseMimeType"] = "application/json"
        if search:
            body["tools"] = [{"google_search": {}}]
        last: Exception | None = None
        models = self.list_models()[:4]
        if not models:
            raise RuntimeError("Gemini: нет доступных моделей flash для этого ключа")
        for model in models:
            for attempt in range(2):
                try:
                    res = net.request("POST", f"{BASE}/models/{model}:generateContent", json_body=body,
                                      headers=self._h(), timeout=75 if search else 45)
                    parts = res["candidates"][0]["content"]["parts"]
                    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                    if text.strip():
                        self.model = model
                        return text
                    last = RuntimeError("пустой ответ")
                    break
                except net.NetError as e:
                    last = e
                    if e.status == 429 and attempt == 0:
                        time.sleep(20)
                        continue
                    break  # 404/400/5xx -> next model
                except (KeyError, IndexError) as e:
                    last = RuntimeError(f"неожиданный ответ: {e}")
                    break
        raise RuntimeError(f"Gemini не ответил: {last}")


def parse_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
        if m:
            return json.loads(m.group(1))
        raise
