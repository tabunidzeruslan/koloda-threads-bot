"""Tiny HTTP helper on the standard library. Never logs URLs (they may carry tokens)."""
from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_SECRETS: list[str] = []


def register_secret(value: str | None) -> None:
    if value and len(value) > 6 and value not in _SECRETS:
        _SECRETS.append(value)


def scrub(text: str) -> str:
    text = str(text)
    for s in _SECRETS:
        text = text.replace(s, "***")
    return text


class NetError(Exception):
    def __init__(self, status: int, body: dict | str):
        self.status = status
        self.body = body
        super().__init__(scrub(f"HTTP {status}: {str(body)[:400]}"))


def request(method: str, url: str, *, params: dict | None = None, form: dict | None = None,
            json_body: dict | None = None, headers: dict | None = None, timeout: float = 45) -> dict:
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    data = None
    hdrs = {"User-Agent": "koloda-threads-bot/1.0"}
    hdrs.update(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode()
        hdrs["Content-Type"] = "application/json"
    elif form is not None:
        data = urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    req = Request(url, data=data, method=method, headers=hdrs)
    try:
        with urlopen(req, timeout=timeout) as r:
            raw, status = r.read(), r.status
    except HTTPError as e:
        raw, status = e.read(), e.code
    except (URLError, TimeoutError, OSError) as e:
        raise NetError(0, f"network: {e}") from None
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        body = {"raw": raw[:500].decode("utf-8", "replace")}
    if status >= 400:
        raise NetError(status, body)
    return body


def env(name: str, default: str = "") -> str:
    v = os.environ.get(name, default).strip()
    register_secret(v if name.endswith(("TOKEN", "KEY", "SECRET")) else None)
    return v


def multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    """files: name -> (filename, mime, bytes)."""
    import uuid
    boundary = uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        if v is None:
            continue
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for k, (fn, mime, data) in files.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fn}\"\r\n"
                f"Content-Type: {mime}\r\n\r\n").encode() + data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def request_raw(method: str, url: str, data: bytes, content_type: str, timeout: float = 60) -> dict:
    req = Request(url, data=data, method=method, headers={"Content-Type": content_type,
                                                          "User-Agent": "koloda-threads-bot/1.0"})
    try:
        with urlopen(req, timeout=timeout) as r:
            raw, status = r.read(), r.status
    except HTTPError as e:
        raw, status = e.read(), e.code
    except (URLError, TimeoutError, OSError) as e:
        raise NetError(0, f"network: {e}") from None
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        body = {"raw": raw[:300].decode("utf-8", "replace")}
    if status >= 400:
        raise NetError(status, body)
    return body
