"""Public image hosting for Threads (Meta downloads images by URL): a public GitHub repo."""
from __future__ import annotations

import base64
import time

from . import net


def upload(repo: str, token: str, data: bytes, name: str) -> str:
    path = f"img/{time.strftime('%Y%m')}/{name}.png"
    net.request("PUT", f"https://api.github.com/repos/{repo}/contents/{path}",
                json_body={"message": f"image {name}", "content": base64.b64encode(data).decode()},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    return f"https://raw.githubusercontent.com/{repo}/main/{path}"
