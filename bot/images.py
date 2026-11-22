"""Image generation providers (first that works wins) and public hosting for Threads."""
from __future__ import annotations

import base64
import random
from urllib.parse import quote
from urllib.request import Request, urlopen

from . import media, net


SAFE_PROMPT = ("A small wooden firewood log cartoon character with white dot eyes and stick arms stands next to a neat "
               "stack of firewood. Minimalist black ink doodle on a plain white background, small orange accents.")


class Images:
    def __init__(self, *, cf_account: str = "", cf_token: str = "", gemini=None, media_repo: str = "",
                 media_token: str = "", pollinations: bool = False):
        self.cf_account, self.cf_token = cf_account, cf_token
        self.gemini = gemini
        self.media_repo, self.media_token = media_repo, media_token
        self.pollinations = pollinations
        net.register_secret(cf_token)
        net.register_secret(media_token)

    @property
    def can_host(self) -> bool:
        return bool(self.media_repo and self.media_token)

    def providers(self) -> list[str]:
        out = []
        if self.cf_account and self.cf_token:
            out.append("cloudflare")
        if self.gemini:
            out.append("gemini")
        if self.pollinations:
            out.append("pollinations")
        return out

    def generate(self, prompt: str, log=print) -> dict:
        """Returns {"bytes": ..., "provider": ..., "url": public url or ""}."""
        errors = []
        for p in self.providers():
            try:
                if p == "cloudflare":
                    try:
                        data = self._cloudflare(prompt)
                    except Exception as e:
                        if "nsfw" not in str(e).lower():
                            raise
                        log("cloudflare safety filter, retrying with a plain prompt")
                        data = self._cloudflare(SAFE_PROMPT)
                    return {"bytes": data, "provider": p, "url": ""}
                if p == "gemini":
                    _, b64 = self.gemini.image(prompt)
                    return {"bytes": base64.b64decode(b64), "provider": p, "url": ""}
                if p == "pollinations":
                    url = self._pollinations_url(prompt)
                    return {"bytes": _get(url, 120), "provider": p, "url": url}
            except Exception as e:
                errors.append(f"{p}: {net.scrub(str(e))[:120]}")
                log("image provider failed:", errors[-1])
        raise RuntimeError("; ".join(errors) or "нет доступных генераторов")

    def _cloudflare(self, prompt: str) -> bytes:
        res = net.request("POST", f"https://api.cloudflare.com/client/v4/accounts/{self.cf_account}/ai/run/"
                                  "@cf/black-forest-labs/flux-1-schnell",
                          json_body={"prompt": prompt[:2000], "steps": 8},
                          headers={"Authorization": f"Bearer {self.cf_token}"}, timeout=120)
        return base64.b64decode(res["result"]["image"])

    @staticmethod
    def _pollinations_url(prompt: str) -> str:
        seed = random.randint(1, 10 ** 6)
        return (f"https://image.pollinations.ai/prompt/{quote(prompt[:1500])}"
                f"?width=1024&height=1024&model=flux&nologo=true&seed={seed}")

    def host(self, data: bytes, name: str) -> str:
        return media.upload(self.media_repo, self.media_token, data, name)


def _get(url: str, timeout: float) -> bytes:
    with urlopen(Request(url, headers={"User-Agent": "koloda-threads-bot/1.0"}), timeout=timeout) as r:
        data = r.read()
    if len(data) < 5000:
        raise RuntimeError("слишком маленький ответ, не картинка")
    return data
