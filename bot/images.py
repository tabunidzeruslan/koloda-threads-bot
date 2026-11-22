"""Image generation providers (first that works wins) and public hosting for Threads."""
from __future__ import annotations

import base64
import random
from urllib.parse import quote
from urllib.request import Request, urlopen

from pathlib import Path

from . import media, net

REF_DIR = Path(__file__).resolve().parent.parent / "data" / "ref"
REF_MODEL = "@cf/black-forest-labs/flux-2-klein-4b"   # FLUX.2 takes reference images, keeps the face and outfit
FAST_MODEL = "@cf/black-forest-labs/flux-1-schnell"


SAFE_PROMPT = ("A stocky smiling man with a grey mustache in a black puffer vest stands next to a neat "
               "stack of firewood. Stylized 3D animated character render, warm light, plain beige background.")


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

    def generate(self, prompt: str, log=print, character: bool = True) -> dict:
        """Returns {"bytes": ..., "provider": ..., "url": public url or ""}.
        character=True draws Ivanovych from the reference images; False draws a scene without him."""
        errors = []
        for p in self.providers():
            try:
                if p == "cloudflare":
                    refs = self.references() if character else []
                    if refs:
                        try:
                            return {"bytes": self._cloudflare_ref(prompt, refs), "provider": "cloudflare-ref", "url": ""}
                        except Exception as e:
                            errors.append(f"cloudflare-ref: {net.scrub(str(e))[:160]}")
                            log("reference model failed, falling back:", errors[-1])
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

    @staticmethod
    def references() -> list[Path]:
        return sorted(REF_DIR.glob("*.png"))[:4] if REF_DIR.exists() else []

    def _cloudflare_ref(self, prompt: str, refs: list[Path]) -> bytes:
        lead = ("Use the man from the reference images as the main character: keep exactly the same face, grey hair, "
                "bushy grey mustache, body shape and outfit (black puffer vest with orange KOLODA, orange shirt, brown "
                "cargo trousers, dark boots). New scene: ")
        body, ctype = net.multipart(
            {"prompt": (lead + prompt)[:2000], "width": "1024", "height": "1024"},
            {f"input_image_{i}": (r.name, "image/png", r.read_bytes()) for i, r in enumerate(refs)})
        res = net.request_raw("POST", f"https://api.cloudflare.com/client/v4/accounts/{self.cf_account}/ai/run/{REF_MODEL}",
                              body, ctype, timeout=180, headers={"Authorization": f"Bearer {self.cf_token}"})
        img = (res.get("result") or {}).get("image") or res.get("image")
        if not img:
            raise RuntimeError(f"нет картинки в ответе: {str(res)[:150]}")
        return base64.b64decode(img)

    def _cloudflare(self, prompt: str) -> bytes:
        res = net.request("POST", f"https://api.cloudflare.com/client/v4/accounts/{self.cf_account}/ai/run/{FAST_MODEL}",
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
