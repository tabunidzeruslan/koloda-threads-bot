from __future__ import annotations

import json

from . import net


class Telegram:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, **params) -> dict:
        payload = {k: v for k, v in params.items() if v is not None}
        res = net.request("POST", f"{self.base}/{method}", json_body=payload, timeout=40)
        if not res.get("ok"):
            raise net.NetError(200, res)
        return res.get("result")

    def get_me(self) -> dict:
        return self.call("getMe")

    def updates(self, offset: int | None, wait: int = 0) -> list[dict]:
        """wait > 0 = long polling: Telegram holds the request until a message arrives (instant replies)."""
        params = {"offset": offset, "timeout": wait, "allowed_updates": ["message", "callback_query"]}
        res = net.request("POST", f"{self.base}/getUpdates", json_body={k: v for k, v in params.items() if v is not None},
                          timeout=wait + 15)
        if not res.get("ok"):
            raise net.NetError(200, res)
        return res.get("result") or []

    def set_webhook(self, url: str, secret: str) -> None:
        self.call("setWebhook", url=url, secret_token=secret, allowed_updates=["message", "callback_query"],
                  drop_pending_updates=False)

    def delete_webhook(self) -> None:
        self.call("deleteWebhook", drop_pending_updates=False)

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        self.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in commands])

    def send(self, chat_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None,
             keyboard: list[list[str]] | None = None) -> int:
        markup = None
        if buttons:
            markup = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}
        elif keyboard:
            markup = {"keyboard": [[{"text": t} for t in row] for row in keyboard], "resize_keyboard": True,
                      "is_persistent": True}
        res = self.call("sendMessage", chat_id=chat_id, text=text[:4000],
                        reply_markup=markup, disable_web_page_preview=True)
        return res["message_id"]

    def send_photo(self, chat_id: int, photo: bytes | str, caption: str,
                   buttons: list[list[tuple[str, str]]] | None = None) -> tuple[int, str]:
        """photo: raw bytes or an existing Telegram file_id. Returns (message_id, file_id)."""
        markup = None
        if buttons:
            markup = json.dumps({"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]})
        fields = {"chat_id": chat_id, "caption": caption[:1024], "reply_markup": markup}
        if isinstance(photo, str):
            fields["photo"] = photo
            body, ctype = net.multipart(fields, {})
        else:
            body, ctype = net.multipart(fields, {"photo": ("image.png", "image/png", photo)})
        res = net.request_raw("POST", f"{self.base}/sendPhoto", body, ctype)
        if not res.get("ok"):
            raise net.NetError(200, res)
        r = res["result"]
        return r["message_id"], r["photo"][-1]["file_id"]

    def edit(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text[:4000],
                      disable_web_page_preview=True)
        except net.NetError:
            try:  # photo messages have a caption instead of text
                self.call("editMessageCaption", chat_id=chat_id, message_id=message_id, caption=text[:1024])
            except net.NetError:
                pass  # message too old or unchanged: not important

    def answer(self, callback_id: str, text: str = "") -> None:
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])
        except net.NetError:
            pass  # callbacks expire if the bot answers late; harmless

    def download(self, file_id: str) -> bytes:
        from urllib.request import urlopen
        path = self.call("getFile", file_id=file_id)["file_path"]
        with urlopen(self.base.replace("/bot", "/file/bot") + "/" + path, timeout=60) as r:
            return r.read()

    def download_photo(self, file_id: str) -> tuple[str, str] | None:
        """Returns (mime, base64) of a photo the owner sent, or None."""
        import base64
        from urllib.request import urlopen
        try:
            path = self.call("getFile", file_id=file_id)["file_path"]
            with urlopen(self.base.replace("/bot", "/file/bot") + "/" + path, timeout=30) as r:
                data = r.read()
        except Exception:
            return None
        return ("image/png" if path.endswith(".png") else "image/jpeg", base64.b64encode(data).decode())

    def delete(self, chat_id: int, message_id: int) -> None:
        try:
            self.call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except net.NetError:
            pass


def dumps(x) -> str:
    return json.dumps(x, ensure_ascii=False)
