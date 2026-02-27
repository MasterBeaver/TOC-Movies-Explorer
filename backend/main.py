# backend/main.py
from __future__ import annotations

import csv
import io
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

APP_TITLE = "TOC Movies API"
MOVIES_FILE = os.getenv("MOVIES_FILE", "movies.json")

app = FastAPI(title=APP_TITLE)

# ensure movies are loaded (and seeded when missing) on application startup
@app.on_event("startup")
def _startup_load_data():
    # calling load_movies here avoids referencing it before it's defined
    load_movies()

# Allow your Next.js frontend (localhost:3000) to call FastAPI (localhost:8000)
# and the deployed frontend on Render
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://toc-movies-explorer.vercel.app"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# -----------------------------
# Load data (in-memory cache)
# -----------------------------
_MOVIES: List[Dict[str, Any]] = []


def load_movies() -> List[Dict[str, Any]]:
    global _MOVIES
    if _MOVIES:
        return _MOVIES

    # If the file does not exist yet, try seeding it using the
    # helper script bundled with the repo. This allows a fresh
    # checkout or container to automatically generate movies.json
    # on the first API call.
    if not os.path.exists(MOVIES_FILE):
        try:
            # import locally so that running the API does not require
            # the merge script unless seeding is needed
            from merge_movies import main as _merge_main

            _merge_main()
        except Exception:
            # ignore failures, we'll just return empty list below
            pass

    if not os.path.exists(MOVIES_FILE):
        _MOVIES = []
        return _MOVIES

    with open(MOVIES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        _MOVIES = []
        return _MOVIES

    _MOVIES = data
    return _MOVIES


# -----------------------------
# Money helpers
# -----------------------------
def parse_money_to_usd(text: Optional[str]) -> Optional[float]:
    """
    Extracts a best-effort USD number from box_office strings.
    Supports:
      "$407.7 million", "$1.2 billion", "US$ 3,500,000", "$10–12 million" (takes first)
    Returns raw USD value (e.g., 407700000.0) or None.
    """
    if not text:
        return None

    s = text.lower()
    s = s.replace(",", "")
    s = s.replace("us$", "$").replace("usd", "$")

    # Handle ranges like "10–12 million" -> take first number
    s = re.sub(r"(\d)\s*[–-]\s*(\d)", r"\1", s)

    m = re.search(r"(\d+(?:\.\d+)?)\s*(billion|bn|million|m)?", s)
    if not m:
        return None

    num = float(m.group(1))
    unit = (m.group(2) or "").strip()

    if unit in ("billion", "bn"):
        return num * 1_000_000_000
    if unit in ("million", "m"):
        return num * 1_000_000
    return num


def format_usd_as_millions(usd_value: Optional[float]) -> Optional[str]:
    """
    Always display in "$X.X million" (even if original was billion).
    """
    if usd_value is None:
        return None
    million = usd_value / 1_000_000
    if million >= 1000:
        return f"${million:.0f} million"
    return f"${million:.1f} million"


# -----------------------------
# Query parsing (regex-based)
# -----------------------------
Token = Tuple[str, str]  # (field, value/op)


def tokenize_query(q: str) -> List[str]:
    """
    Tokenize while keeping key:"quoted value" as ONE token.
    Examples:
      director:"James Cameron"
      title:"The Hulk"
      country:"United Kingdom"
    """
    if not q:
        return []
    return re.findall(r'\w+:"[^"]*"|"[^"]*"|\S+', q)


def strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s


def parse_filters(q: str) -> Dict[str, Any]:
    """
    Supported patterns (examples):
      title:Hulk
      director:"James Cameron"
      country:"Japan"
      language:"English"
      release:2003  (matches year prefix)
      runtime>=120  runtime<=90  runtime=134
      boxoffice>100M  boxoffice>=500M  boxoffice>1B
    Also supports free-text tokens (fallback search).
    """
    tokens = tokenize_query(q)
    out: Dict[str, Any] = {
        "title": None,
        "director": None,
        "country": None,     # NEW
        "language": None,    # NEW
        "release": None,
        "runtime": None,     # (op, minutes)
        "boxoffice": None,   # (op, usd_value)
        "free": [],          # list[str]
    }

    for t in tokens:
        raw = strip_quotes(t)

        m = re.match(r"(?i)^title:(.+)$", raw)
        if m:
            out["title"] = strip_quotes(m.group(1).strip())
            continue

        m = re.match(r"(?i)^director:(.+)$", raw)
        if m:
            out["director"] = strip_quotes(m.group(1).strip())
            continue

        # NEW: country:...
        m = re.match(r"(?i)^country:(.+)$", raw)
        if m:
            out["country"] = strip_quotes(m.group(1).strip())
            continue

        # NEW: language:...
        m = re.match(r"(?i)^language:(.+)$", raw)
        if m:
            out["language"] = strip_quotes(m.group(1).strip())
            continue

        m = re.match(r"(?i)^release:(.+)$", raw)
        if m:
            out["release"] = m.group(1).strip()
            continue

        m = re.match(r"(?i)^runtime\s*(>=|<=|=|>|<)\s*(\d{1,3})$", raw)
        if m:
            out["runtime"] = (m.group(1), int(m.group(2)))
            continue

        m = re.match(
            r"(?i)^boxoffice\s*(>=|<=|=|>|<)\s*(\d+(?:\.\d+)?)([mb])$", raw)
        if m:
            op = m.group(1)
            num = float(m.group(2))
            unit = m.group(3).lower()
            usd = num * (1_000_000_000 if unit == "b" else 1_000_000)
            out["boxoffice"] = (op, usd)
            continue

        out["free"].append(raw)

    return out


def compare_num(op: str, a: float, b: float) -> bool:
    if op == ">":
        return a > b
    if op == "<":
        return a < b
    if op == ">=":
        return a >= b
    if op == "<=":
        return a <= b
    if op == "=":
        return a == b
    return False


def match_prefix_or_contains(field_value: str, query: str) -> bool:
    """
    If query is short (1–2 chars, no spaces) => prefix match (starts with).
    Otherwise => contains match.
    """
    fv = (field_value or "").lower().strip()
    q = (query or "").lower().strip()

    if not q:
        return True

    short_simple = (len(q) <= 2 and " " not in q)

    if short_simple:
        return fv.startswith(q)
    return q in fv


def match_movie(m: Dict[str, Any], filters: Dict[str, Any]) -> bool:
    release = (m.get("release_date") or "").lower()

    # title
    if filters.get("title"):
        if not match_prefix_or_contains(m.get("title") or "", filters["title"]):
            return False

    # director
    if filters.get("director"):
        if not match_prefix_or_contains(m.get("director") or "", filters["director"]):
            return False

    # NEW: country
    if filters.get("country"):
        if not match_prefix_or_contains(m.get("country") or "", filters["country"]):
            return False

    # NEW: language
    if filters.get("language"):
        if not match_prefix_or_contains(m.get("language") or "", filters["language"]):
            return False

    # release (year or date prefix contains)
    if filters.get("release"):
        if filters["release"].lower() not in release:
            return False

    # runtime
    if filters.get("runtime"):
        op, val = filters["runtime"]
        rt = m.get("running_time")
        try:
            rt_num = float(rt) if rt is not None else None
        except Exception:
            rt_num = None
        if rt_num is None:
            return False
        if not compare_num(op, rt_num, float(val)):
            return False

    # boxoffice
    if filters.get("boxoffice"):
        op, val = filters["boxoffice"]
        bo_usd = m.get("box_office_usd")
        if bo_usd is None:
            bo_usd = parse_money_to_usd(m.get("box_office"))
        if bo_usd is None:
            return False
        if not compare_num(op, float(bo_usd), float(val)):
            return False

    # free text: must match all tokens somewhere in the haystack
    free_tokens: List[str] = filters.get("free") or []
    if free_tokens:
        hay = " ".join(
            [
                (m.get("title") or ""),
                (m.get("director") or ""),
                (m.get("country") or ""),    # NEW
                (m.get("language") or ""),   # NEW
                (m.get("release_date") or ""),
                (m.get("box_office") or ""),
            ]
        ).lower()
        for tok in free_tokens:
            if tok.lower() not in hay:
                return False

    return True


# -----------------------------
# Sorting (IMPORTANT: sort BEFORE pagination)
# -----------------------------
def sort_key(movie: Dict[str, Any], sort: str):
    t = movie.get("title") or ""
    r = movie.get("release_date") or ""
    rt = movie.get("running_time")
    try:
        rt_num = float(rt) if rt is not None else -1.0
    except Exception:
        rt_num = -1.0

    bo_usd = movie.get("box_office_usd")
    if bo_usd is None:
        bo_usd = parse_money_to_usd(movie.get("box_office"))
    try:
        bo_num = float(bo_usd) if bo_usd is not None else -1.0
    except Exception:
        bo_num = -1.0

    if sort == "release_desc":
        return r
    if sort == "release_asc":
        return r
    if sort == "runtime_desc":
        return rt_num
    if sort == "runtime_asc":
        return rt_num
    if sort == "box_desc":
        return bo_num
    if sort == "box_asc":
        return bo_num
    if sort == "title_asc":
        return t.lower()

    return t.lower()


def apply_sort(movies: List[Dict[str, Any]], sort: str) -> List[Dict[str, Any]]:
    sort = (sort or "").strip()

    aliases = {
        "boxoffice_desc": "box_desc",
        "boxoffice_asc": "box_asc",
        "default": "",
    }
    sort = aliases.get(sort, sort)

    if sort == "release_desc":
        return sorted(movies, key=lambda x: sort_key(x, sort), reverse=True)
    if sort == "release_asc":
        return sorted(movies, key=lambda x: sort_key(x, sort), reverse=False)

    if sort == "runtime_desc":
        return sorted(movies, key=lambda x: sort_key(x, sort), reverse=True)
    if sort == "runtime_asc":
        return sorted(movies, key=lambda x: sort_key(x, sort), reverse=False)

    if sort == "box_desc":
        return sorted(movies, key=lambda x: sort_key(x, sort), reverse=True)
    if sort == "box_asc":
        return sorted(movies, key=lambda x: sort_key(x, sort), reverse=False)

    if sort == "title_asc":
        return sorted(movies, key=lambda x: sort_key(x, sort), reverse=False)

    return movies


# -----------------------------
# API
# -----------------------------
@app.get("/health")
def health():
    return {"ok": True, "count": len(load_movies())}


@app.get("/movies")
def list_movies(
    page: int = Query(1, ge=1),
    limit: int = Query(15, ge=1, le=100),
    q: str = Query("", max_length=500),
    sort: str = Query("default"),
):
    movies = load_movies()

    filters = parse_filters(q.strip())
    filtered = [m for m in movies if match_movie(m, filters)]

    # Sort across ALL filtered results (before pagination)
    filtered = apply_sort(filtered, sort)

    total = len(filtered)
    start = (page - 1) * limit
    end = start + limit
    page_items = filtered[start:end]

    # Clean up box office display: always "$X.X million"
    out_items: List[Dict[str, Any]] = []
    for m in page_items:
        mm = dict(m)

        bo_usd = mm.get("box_office_usd")
        if bo_usd is None:
            bo_usd = parse_money_to_usd(mm.get("box_office"))

        mm["box_office"] = format_usd_as_millions(bo_usd) or None
        mm["box_office_usd"] = bo_usd

        out_items.append(mm)

    return {
        "total": total,
        "page": page,
        "limit": limit,
        "items": out_items,
    }


@app.get("/export.csv")
def export_csv(
    q: str = Query("", max_length=500),
    sort: str = Query("default"),
):
    movies = load_movies()

    filters = parse_filters(q.strip())
    filtered = [m for m in movies if match_movie(m, filters)]
    filtered = apply_sort(filtered, sort)

    def iter_csv():
        buf = io.StringIO()
        writer = csv.writer(buf)

        writer.writerow(
            [
                "title",
                "director",
                "release_date",
                "running_time_min",
                "box_office_usd",
                "box_office_display",
                "country",
                "language",
                "url",
            ]
        )
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)

        for m in filtered:
            bo_usd = m.get("box_office_usd")
            if bo_usd is None:
                bo_usd = parse_money_to_usd(m.get("box_office"))

            box_display = format_usd_as_millions(bo_usd) or ""

            writer.writerow(
                [
                    m.get("title") or "",
                    m.get("director") or "",
                    m.get("release_date") or "",
                    m.get("running_time") or "",
                    bo_usd if bo_usd is not None else "",
                    box_display,
                    m.get("country") or "",
                    m.get("language") or "",
                    m.get("url") or "",
                ]
            )
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    filename = "movies_export.csv"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}

    return StreamingResponse(iter_csv(), media_type="text/csv", headers=headers)


def safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def get_year_from_release(release_date: Optional[str]) -> Optional[int]:
    if not release_date:
        return None
    m = re.match(r"^\s*(\d{4})", str(release_date))
    if not m:
        return None
    y = int(m.group(1))
    return y if 1800 <= y <= 2100 else None


@app.get("/stats")
def stats():
    movies = load_movies()
    total = len(movies)

    country_counts: Dict[str, int] = {}

    def normalize_country(raw: str) -> str:
        s = (raw or "").strip()
        if not s:
            return "Unknown"
        s_lower = s.lower()
        if "united states" in s_lower:
            return "United States"
        if "united kingdom" in s_lower:
            return "United Kingdom"

        s = re.sub(r"\[\d+\]", "", s)
        s = re.sub(r"<[^>]+>", "", s)
        s = s.replace("=", " ").strip()
        s = re.sub(r"\s+", " ", s).strip()
        s = re.split(r",|;|/|\band\b|&", s, maxsplit=1, flags=re.I)[0].strip()
        return s if s else "Unknown"

    for m in movies:
        c = normalize_country(m.get("country") or "")
        country_counts[c] = country_counts.get(c, 0) + 1

    top = sorted(country_counts.items(), key=lambda kv: kv[1], reverse=True)
    TOP_N = 10
    movies_by_country = [{"country": k, "count": v} for k, v in top[:TOP_N]]
    other_sum = sum(v for _, v in top[TOP_N:])
    if other_sum > 0:
        movies_by_country.append({"country": "Other", "count": other_sum})

    def has_value(v: Any) -> bool:
        return v is not None and str(v).strip() != ""

    director_ok = 0
    runtime_ok = 0
    release_ok = 0
    boxoffice_ok = 0

    key_fields = ["title", "director",
                  "release_date", "running_time", "box_office"]
    missing_fields_total = 0

    for m in movies:
        if has_value(m.get("director")):
            director_ok += 1

        if safe_float(m.get("running_time")) is not None:
            runtime_ok += 1

        if has_value(m.get("release_date")):
            release_ok += 1

        bo = m.get("box_office_usd")
        if bo is None:
            bo = parse_money_to_usd(m.get("box_office"))
        if safe_float(bo) is not None:
            boxoffice_ok += 1

        for k in key_fields:
            if not has_value(m.get(k)):
                missing_fields_total += 1

    def pct(x: int) -> float:
        return round((x / total) * 100, 1) if total else 0.0

    quality = {
        "director": {"pct": pct(director_ok), "filled": director_ok, "total": total},
        "runtime": {"pct": pct(runtime_ok), "filled": runtime_ok, "total": total},
        "release": {"pct": pct(release_ok), "filled": release_ok, "total": total},
        "boxoffice": {"pct": pct(boxoffice_ok), "filled": boxoffice_ok, "total": total},
        "missing_fields_total": missing_fields_total,
    }

    decade_counts: Dict[int, int] = {}
    for m in movies:
        y = get_year_from_release(m.get("release_date"))
        if y is None:
            continue
        decade = (y // 10) * 10
        decade_counts[decade] = decade_counts.get(decade, 0) + 1

    movies_by_decade = [{"decade": f"{d}s", "count": decade_counts[d]}
                        for d in sorted(decade_counts.keys())]

    buckets = {"<90": 0, "90–119": 0, "120–149": 0, "150+": 0}
    for m in movies:
        rt = safe_float(m.get("running_time"))
        if rt is None:
            continue
        if rt < 90:
            buckets["<90"] += 1
        elif rt < 120:
            buckets["90–119"] += 1
        elif rt < 150:
            buckets["120–149"] += 1
        else:
            buckets["150+"] += 1

    runtime_distribution = [{"bucket": k, "count": v}
                            for k, v in buckets.items()]

    return {
        "total_movies": total,
        "quality": quality,
        "charts": {
            "movies_by_decade": movies_by_decade,
            "runtime_distribution": runtime_distribution,
            "movies_by_country": movies_by_country,
        },
    }
