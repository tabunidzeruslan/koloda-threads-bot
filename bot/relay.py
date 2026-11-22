"""Cloudflare Worker relay: Telegram webhook -> KV queue -> wakes the GitHub Actions bot.
Deployed by .github/workflows/worker.yml; the bot reads updates from it instead of getUpdates."""
from __future__ import annotations

import hashlib
import hmac
import json

from . import net

NAME = "koloda-tg"
API = "https://api.cloudflare.com/client/v4/accounts/{acc}"


def derive(tg_token: str, purpose: str) -> str:
    return hmac.new(tg_token.encode(), purpose.encode(), hashlib.sha256).hexdigest()[:40]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def subdomain(acc: str, token: str) -> str:
    return net.request("GET", API.format(acc=acc) + "/workers/subdomain", headers=_h(token))["result"]["subdomain"]


def url(acc: str, token: str) -> str:
    return f"https://{NAME}.{subdomain(acc, token)}.workers.dev"


def deploy(acc: str, token: str, *, tg_token: str, dispatch_token: str, repo: str, script: str, log=print) -> str:
    base = API.format(acc=acc)
    # workers.dev subdomain (first Worker on the account needs one)
    try:
        sub = subdomain(acc, token)
    except net.NetError:
        sub = "koloda-" + hashlib.sha1(acc.encode()).hexdigest()[:6]
        net.request("PUT", base + "/workers/subdomain", json_body={"subdomain": sub}, headers=_h(token))
    # KV namespace
    spaces = net.request("GET", base + "/storage/kv/namespaces?per_page=100", headers=_h(token))["result"]
    ns = next((x["id"] for x in spaces if x["title"] == NAME + "-queue"), None)
    if not ns:
        ns = net.request("POST", base + "/storage/kv/namespaces", json_body={"title": NAME + "-queue"},
                         headers=_h(token))["result"]["id"]
    meta = {"main_module": "worker.js", "compatibility_date": "2025-01-01", "bindings": [
        {"type": "kv_namespace", "name": "Q", "namespace_id": ns},
        {"type": "secret_text", "name": "WEBHOOK_SECRET", "text": derive(tg_token, "webhook")},
        {"type": "secret_text", "name": "PULL_SECRET", "text": derive(tg_token, "pull")},
        {"type": "secret_text", "name": "DISPATCH_TOKEN", "text": dispatch_token},
        {"type": "plain_text", "name": "REPO", "text": repo}]}
    import uuid  # multipart by hand: the module file needs its own content type
    b = uuid.uuid4().hex
    parts = (f"--{b}\r\nContent-Disposition: form-data; name=\"metadata\"\r\nContent-Type: application/json\r\n\r\n"
             f"{json.dumps(meta)}\r\n--{b}\r\nContent-Disposition: form-data; name=\"worker.js\"; filename=\"worker.js\"\r\n"
             f"Content-Type: application/javascript+module\r\n\r\n").encode() + script.encode() + f"\r\n--{b}--\r\n".encode()
    net.request_raw("PUT", base + f"/workers/scripts/{NAME}", parts, f"multipart/form-data; boundary={b}",
                    timeout=60, headers=_h(token))
    net.request("POST", base + f"/workers/scripts/{NAME}/subdomain", json_body={"enabled": True}, headers=_h(token))
    u = f"https://{NAME}.{sub}.workers.dev"
    log("worker deployed:", u)
    return u


class Relay:
    def __init__(self, base_url: str, tg_token: str):
        self.base = base_url.rstrip("/")
        self.secret = derive(tg_token, "pull")
        net.register_secret(self.secret)

    def updates(self) -> list[dict]:
        res = net.request("GET", self.base + "/updates", headers={"Authorization": f"Bearer {self.secret}"}, timeout=20)
        return res if isinstance(res, list) else []

    def ack(self, ids: list[int]) -> None:
        if ids:
            net.request("POST", self.base + "/ack", json_body={"ids": ids},
                        headers={"Authorization": f"Bearer {self.secret}"}, timeout=20)

    def healthy(self) -> bool:
        try:
            from urllib.request import urlopen
            with urlopen(self.base + "/health", timeout=10) as r:
                return r.read().strip() == b"ok"
        except Exception:
            return False
