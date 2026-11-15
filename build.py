"""Build docs/data.json: CS/robotics conferences whose submission deadline has not passed yet.

Source: ccfddl/ccf-deadlines (community-maintained YAML) + extra/*.yml (same format, for
conferences missing upstream). Places are geocoded with OpenStreetMap Nominatim and cached
in geocache.json so each place is looked up only once.
"""
import io
import json
import re
import sys
import tarfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
SRC_URL = "https://github.com/ccfddl/ccf-deadlines/archive/refs/heads/main.tar.gz"
CACHE = ROOT / "geocache.json"
OUT = ROOT / "docs" / "data.json"
UA = "conf-deadline-map/1.0 (https://github.com/kbh1795-sketch/conf-deadline-map)"

CATEGORIES = {
    "AI": "AI / ML / Vision / NLP",
    "RO": "Robotics",
    "DS": "Architecture / Parallel / Storage",
    "NW": "Networking",
    "SC": "Security",
    "SE": "Software Eng. / OS / PL",
    "DB": "Database / Data Mining / IR",
    "CT": "Theory",
    "CG": "Graphics / Multimedia",
    "HI": "HCI / Ubiquitous",
    "MX": "Interdisciplinary",
}
# Robotics venues live under "AI" upstream; pull them out into their own category.
ROBOTICS = {"ICRA", "IROS", "RSS", "CORL", "HRI", "CASE", "HUMANOIDS", "ISRR", "ICUAS", "ISER", "WAFR"}


def tz_offset(tz):
    tz = (tz or "AoE").strip().upper()
    if tz in ("AOE", "UTC-12"):
        return -12
    if tz in ("PT", "PST", "PDT"):
        return -8
    if tz in ("UTC", "GMT", "UTC+0", "UTC-0"):
        return 0
    m = re.fullmatch(r"(?:UTC|GMT)([+-]\d{1,2})(?::?(\d{2}))?", tz)
    if m:
        h = int(m.group(1))
        return h + (int(m.group(2) or 0) / 60) * (1 if h >= 0 else -1)
    return -12  # unknown -> assume the most lenient


def parse_deadline(s, tz):
    if not s or "TBD" in str(s).upper():
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    else:
        return None
    if len(s) == 10:
        dt = dt.replace(hour=23, minute=59, second=59)
    return (dt - timedelta(hours=tz_offset(tz))).replace(tzinfo=timezone.utc)


def load_sources():
    with urllib.request.urlopen(urllib.request.Request(SRC_URL, headers={"User-Agent": UA}), timeout=60) as r:
        data = r.read()
    entries = []
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        for m in tf.getmembers():
            if re.search(r"/conference/[A-Z]{2}/[^/]+\.ya?ml$", m.name):
                entries += yaml.safe_load(tf.extractfile(m)) or []
    for p in sorted((ROOT / "extra").glob("*.yml")):
        entries += yaml.safe_load(p.read_text()) or []
    return entries


def clean_place(place):
    p = re.sub(r"\((?:hybrid|virtual|online)[^)]*\)", "", place, flags=re.I)
    p = re.split(r"\s+and\s+(?:online|virtual)|\s*&\s*online", p, flags=re.I)[0]
    p = re.split(r"\s+and\s+|\s*/\s*", p)[0]  # multi-site: use the first
    p = re.sub(r"\s+", " ", p).strip(" ,")
    return p


def query_variants(place):
    p = clean_place(place)
    yield p
    no_paren = re.sub(r"\([^)]*\)", "", p)
    no_paren = re.sub(r"\s*,\s*", ", ", no_paren).strip(" ,")
    if no_paren != p:
        yield no_paren
    parts = [x.strip() for x in no_paren.split(",") if x.strip()]
    for i in range(1, len(parts)):  # drop venue/campus names from the left
        yield ", ".join(parts[i:])


_last_call = [0.0]


def nominatim(q):
    wait = 1.1 - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": q, "format": "json", "limit": 1, "accept-language": "en"})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30) as r:
            res = json.load(r)
    except Exception as e:  # network hiccup: don't cache, retry next run
        print(f"  geocode error {q!r}: {e}", file=sys.stderr)
        return "error"
    return [float(res[0]["lat"]), float(res[0]["lon"])] if res else None


def geocode(place, cache):
    if not place or re.fullmatch(r"\s*(tbd|online|virtual|n/?a)?\s*", place, flags=re.I):
        return None
    if place in cache:
        return cache[place]
    for q in query_variants(place):
        loc = nominatim(q)
        if loc == "error":
            return None
        if loc:
            cache[place] = loc
            print(f"  geocoded {place!r} -> {loc}")
            return loc
    cache[place] = None
    print(f"  no match for {place!r}")
    return None


def main():
    now = datetime.now(timezone.utc)
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    out = []
    for c in load_sources():
        title = str(c.get("title", "")).strip()
        sub = "RO" if title.upper() in ROBOTICS else c.get("sub", "MX")
        for conf in c.get("confs") or []:
            upcoming = []
            for t in conf.get("timeline") or []:
                dl = parse_deadline(t.get("deadline"), conf.get("timezone"))
                abs_dl = parse_deadline(t.get("abstract_deadline"), conf.get("timezone"))
                if dl and dl > now:
                    upcoming.append({
                        "deadline": dl.isoformat(),
                        "abstract": abs_dl.isoformat() if abs_dl and abs_dl > now else None,
                        "comment": t.get("comment"),
                    })
            if not upcoming:
                continue
            upcoming.sort(key=lambda x: x["deadline"])
            place = str(conf.get("place") or "")
            rank = c.get("rank") or {}
            out.append({
                "title": title,
                "year": conf.get("year"),
                "description": c.get("description", ""),
                "sub": sub,
                "rank": {k: rank.get(k) for k in ("ccf", "core") if rank.get(k) and rank.get(k) != "N"},
                "link": conf.get("link"),
                "date": conf.get("date"),
                "place": place,
                "loc": geocode(place, cache),
                "timezone": conf.get("timezone") or "AoE",
                "deadlines": upcoming,
            })
    out.sort(key=lambda x: x["deadlines"][0]["deadline"])
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"updated": now.isoformat(), "categories": CATEGORIES, "conferences": out},
                              ensure_ascii=False, indent=1))
    print(f"{len(out)} open conferences ({sum(1 for x in out if x['loc'])} on map)")


if __name__ == "__main__":
    main()
