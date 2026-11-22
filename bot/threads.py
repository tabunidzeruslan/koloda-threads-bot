"""Threads Graph API wrapper (graph.threads.net)."""
from __future__ import annotations

import time

from . import net

BASE = "https://graph.threads.net/v1.0"


class Threads:
    def __init__(self, token: str):
        self.token = token
        net.register_secret(token)

    def _get(self, path: str, **params) -> dict:
        params["access_token"] = self.token
        return net.request("GET", f"{BASE}/{path}", params=params)

    def _post(self, path: str, **form) -> dict:
        form["access_token"] = self.token
        return net.request("POST", f"{BASE}/{path}", form=form)

    def me(self) -> dict:
        return self._get("me", fields="id,username")

    def publishing_limit(self) -> dict:
        return self._get("me/threads_publishing_limit", fields="quota_usage,config")

    def my_posts(self, limit: int = 10) -> list[dict]:
        return self._get("me/threads", fields="id,text,timestamp,permalink", limit=limit).get("data", [])

    def replies(self, post_id: str) -> list[dict]:
        return self._get(f"{post_id}/replies", fields="id,text,username,timestamp").get("data", [])

    def insights(self) -> dict:
        return self._get("me/threads_insights", metric="views")

    def account_stats(self, since: int, until: int) -> dict:
        """Totals for the period: views, likes, replies, reposts, quotes, followers_count."""
        data = self._get("me/threads_insights", metric="views,likes,replies,reposts,quotes,followers_count",
                         since=since, until=until).get("data", [])
        return {m["name"]: metric_value(m) for m in data}

    def post_stats(self, post_id: str) -> dict:
        data = self._get(f"{post_id}/insights", metric="views,likes,replies,reposts,quotes").get("data", [])
        return {m["name"]: metric_value(m) for m in data}


def metric_value(m: dict) -> int:
    if isinstance(m.get("total_value"), dict):
        return int(m["total_value"].get("value") or 0)
    return sum(int(v.get("value") or 0) for v in m.get("values", []))

    def publish_text(self, text: str, wait: float = 4.0) -> dict:
        container = self._post("me/threads", media_type="TEXT", text=text)["id"]
        time.sleep(wait)
        post_id = self._post("me/threads_publish", creation_id=container)["id"]
        link = ""
        try:
            link = self._get(post_id, fields="permalink").get("permalink", "")
        except net.NetError:
            pass
        return {"id": post_id, "permalink": link}

    def publish_image(self, text: str, image_url: str) -> dict:
        container = self._post("me/threads", media_type="IMAGE", image_url=image_url, text=text)["id"]
        for _ in range(12):  # images need processing on Meta's side
            time.sleep(5)
            st = self._get(container, fields="status,error_message")
            if st.get("status") == "FINISHED":
                break
            if st.get("status") in ("ERROR", "EXPIRED"):
                raise net.NetError(400, st)
        post_id = self._post("me/threads_publish", creation_id=container)["id"]
        link = ""
        try:
            link = self._get(post_id, fields="permalink").get("permalink", "")
        except net.NetError:
            pass
        return {"id": post_id, "permalink": link}

    def refresh(self) -> dict:
        return net.request("GET", "https://graph.threads.net/refresh_access_token",
                           params={"grant_type": "th_refresh_token", "access_token": self.token})
