from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT = {
    "version": 1,
    "owner_chat_id": None,
    "tg_offset": None,
    "schedule_on": False,      # daily drafts start only after the owner sends /on
    "mode": "approve",         # every post needs the owner's button
    "paused_reason": None,
    "planned_total": 0,
    "days": {},
    "posts": [],
    "fail_streak": 0,
    "token": None,             # {"enc": ..., "src_fp": ..., "refreshed_at": ...}
    "gemini_model": None,
    "last_run": None,
}


def load(path: str | os.PathLike) -> dict:
    p = Path(path)
    data = {}
    if p.exists() and p.stat().st_size:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    state = json.loads(json.dumps(DEFAULT))
    state.update(data)
    return state


def save(path: str | os.PathLike, state: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(p)
