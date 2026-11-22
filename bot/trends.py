"""Free outside signals: Google Trends (Ukraine), Kyiv weather (Open-Meteo), calendar."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path
from urllib.request import Request, urlopen

from . import net

TRENDS_URL = "https://trends.google.com/trending/rss?geo=UA"
WEATHER_URL = ("https://api.open-meteo.com/v1/forecast?latitude=50.45&longitude=30.52"
               "&daily=temperature_2m_min,temperature_2m_max,snowfall_sum,precipitation_sum"
               "&timezone=Europe%2FKyiv&forecast_days=3")
DATA = Path(__file__).resolve().parent.parent / "data"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_trends(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    out = []
    for item in root.iter("item"):
        t = {"title": "", "traffic": "", "news": []}
        for el in item:
            name = _local(el.tag)
            if name == "title":
                t["title"] = (el.text or "").strip()
            elif name == "approx_traffic":
                t["traffic"] = (el.text or "").strip()
            elif name == "news_item":
                n = {_local(x.tag).replace("news_item_", ""): (x.text or "").strip() for x in el}
                t["news"].append({"title": n.get("title", ""), "url": n.get("url", ""), "source": n.get("source", "")})
        if t["title"]:
            out.append(t)
    return out


def fetch_trends() -> list[dict]:
    req = Request(TRENDS_URL, headers={"User-Agent": "Mozilla/5.0 koloda-threads-bot"})
    with urlopen(req, timeout=30) as r:
        return parse_trends(r.read().decode("utf-8", "replace"))


def fetch_weather() -> dict:
    d = net.request("GET", WEATHER_URL, timeout=30)["daily"]
    days = []
    for i, day in enumerate(d["time"]):
        days.append({"date": day, "tmin": d["temperature_2m_min"][i], "tmax": d["temperature_2m_max"][i],
                     "snow_cm": d["snowfall_sum"][i], "rain_mm": d["precipitation_sum"][i]})
    return {"days": days}


def weather_line(w: dict | None) -> str:
    if not w or not w.get("days"):
        return ""
    names = ["сьогодні", "завтра", "післязавтра"]
    parts = []
    for name, d in zip(names, w["days"]):
        s = f"{name} від {round(d['tmin'])} до {round(d['tmax'])}°C"
        if d.get("snow_cm", 0) >= 0.5:
            s += f", сніг {d['snow_cm']:.0f} см"
        parts.append(s)
    return "Погода в Києві: " + "; ".join(parts) + "."


def weather_event(w: dict | None) -> str | None:
    """A strong reason to post about the weather tomorrow, or None."""
    if not w or len(w.get("days", [])) < 2:
        return None
    today, tom = w["days"][0], w["days"][1]
    if tom.get("snow_cm", 0) >= 1:
        return f"Завтра в Києві сніг (~{tom['snow_cm']:.0f} см) і від {round(tom['tmin'])}°C."
    if tom["tmin"] <= -8:
        return f"Завтра в Києві мороз до {round(tom['tmin'])}°C."
    if today["tmin"] - tom["tmin"] >= 7:
        return f"Різке похолодання: завтра вночі {round(tom['tmin'])}°C, сьогодні {round(today['tmin'])}°C."
    return None


def calendar_note(today: date) -> str:
    try:
        cal = json.loads((DATA / "calendar.json").read_text(encoding="utf-8"))["events"]
    except (OSError, ValueError, KeyError):
        return ""
    notes = []
    for i in range(0, 4):
        d = today + timedelta(days=i)
        ev = cal.get(d.strftime("%m-%d"))
        if ev:
            notes.append(f"{'сьогодні' if i == 0 else d.strftime('%d.%m')}: {ev}")
    return ("Найближчі приводи: " + "; ".join(notes) + ".") if notes else ""


def parse_rss(xml_text: str, source: str, limit: int = 12) -> list[dict]:
    """RSS 2.0 or Atom -> [{"title", "url", "source"}]."""
    root = ET.fromstring(xml_text)
    out = []
    for item in root.iter():
        if _local(item.tag) not in ("item", "entry"):
            continue
        title, url = "", ""
        for el in item:
            n = _local(el.tag)
            if n == "title":
                title = (el.text or "").strip()
            elif n == "link":
                url = (el.text or "").strip() or el.attrib.get("href", "")
        if title:
            out.append({"title": title, "url": url, "source": source})
        if len(out) >= limit:
            break
    return out


def news_sources() -> list[dict]:
    try:
        return json.loads((DATA / "kyiv.json").read_text(encoding="utf-8")).get("trend_sources", [])
    except (OSError, ValueError):
        return []


def fetch_news(sources: list[dict] | None = None, per_source: int = 8, log=print) -> list[dict]:
    out = []
    for src in (sources if sources is not None else news_sources())[:15]:
        url = src.get("rss") or ""
        if not url.startswith("http"):
            continue
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0 koloda-threads-bot"})
            with urlopen(req, timeout=15) as r:
                out += parse_rss(r.read().decode("utf-8", "replace"), src.get("name", ""), per_source)
        except Exception as e:  # one dead feed must not stop the rest
            log("news feed failed:", src.get("name"), str(e)[:80])
    return out


def signals(log=print) -> list[dict]:
    """Google Trends Ukraine + Kyiv/Ukraine news headlines, in one list for the picker."""
    items = []
    try:
        items += fetch_trends()
    except Exception as e:
        log("google trends failed:", str(e)[:100])
    for n in fetch_news(log=log):
        items.append({"title": n["title"], "traffic": n["source"], "news": [n]})
    return items
