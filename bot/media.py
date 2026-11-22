"""Public image hosting for Threads (Meta downloads images by URL).

Images go to the `media` branch of this (public) repo with the workflow's own GITHUB_TOKEN,
so no personal access token is needed."""
from __future__ import annotations

import base64
import time

from . import net

BRANCH = "media"


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


def ensure_branch(repo: str, token: str, branch: str = BRANCH) -> None:
    api = f"https://api.github.com/repos/{repo}"
    try:
        net.request("GET", f"{api}/branches/{branch}", headers=_headers(token))
        return
    except net.NetError as e:
        if "404" not in str(e):
            raise
    info = net.request("GET", api, headers=_headers(token))
    base = net.request("GET", f"{api}/git/ref/heads/{info.get('default_branch', 'main')}", headers=_headers(token))
    net.request("POST", f"{api}/git/refs", headers=_headers(token),
                json_body={"ref": f"refs/heads/{branch}", "sha": base["object"]["sha"]})


def upload(repo: str, token: str, data: bytes, name: str, branch: str = BRANCH) -> str:
    ensure_branch(repo, token, branch)
    path = f"img/{time.strftime('%Y%m')}/{name}.png"
    net.request("PUT", f"https://api.github.com/repos/{repo}/contents/{path}",
                json_body={"message": f"image {name}", "content": base64.b64encode(data).decode(), "branch": branch},
                headers=_headers(token))
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"
