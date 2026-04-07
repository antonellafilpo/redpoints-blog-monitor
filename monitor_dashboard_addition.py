"""
DASHBOARD DATA WRITER
─────────────────────
Appended to monitor.py. Called once per week from main().
Writes/updates dashboard_data.json on GitHub Pages.
"""

import os
import json
import logging
import calendar
import requests
from datetime import date, timedelta

log = logging.getLogger(__name__)

WEEKLY_GOAL    = 8
MONTHLY_GOAL   = 32
DASHBOARD_JSON = "dashboard_data.json"
WP_BASE        = "https://www.redpoints.com/wp-json/wp/v2/posts"
GSC_SITE_URL   = os.environ.get("GSC_SITE_URL", "https://www.redpoints.com/")


def last_complete_week():
    today  = date.today()
    sunday = today - timedelta(days=today.weekday() + 1)
    monday = sunday - timedelta(days=6)
    return monday, sunday


def week_label(d):
    return f"{d.strftime('%b')} w{(d.day - 1) // 7 + 1}"


def weeks_in_month(year, month):
    _, days = calendar.monthrange(year, month)
    seen, labels = set(), []
    for day in range(1, days + 1):
        lbl = week_label(date(year, month, day))
        if lbl not in seen:
            seen.add(lbl)
            labels.append(lbl)
    return labels


def month_name(year, month):
    return date(year, month, 1).strftime("%B %Y")


def wp_fetch_by_slug(slug):
    try:
        r = requests.get(WP_BASE, params={"slug": slug, "_fields": "slug,title,link,modified,date"}, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data:
            p    = data[0]
            path = "/" + p["link"].replace("https://www.redpoints.com/", "").strip("/")
            return p["modified"][:10], p["title"]["rendered"], path, p.get("date", "")[:10]
    except Exception as e:
        log.debug(f"WP slug error ({slug}): {e}")
    return None, None, None, None


def fetch_wp_updates(gsc_service, week_start, week_end):
    body = {
        "startDate": week_start.isoformat(), "endDate": week_end.isoformat(),
        "dimensions": ["page"], "rowLimit": 5000,
        "dimensionFilterGroups": [{"filters": [{"dimension": "page", "operator": "contains", "expression": "/blog/"}]}],
    }
    try:
        resp = gsc_service.searchanalytics().query(siteUrl=GSC_SITE_URL, body=body).execute()
    except Exception as e:
        log.warning(f"GSC blog URL fetch error: {e}")
        return []

    def is_post(u):
        u = u.rstrip("/")
        after = u.split("/blog/")[-1].strip("/") if "/blog/" in u else ""
        return bool(after) and "/" not in after and len(after) > 3 and not any(x in after for x in ("page", "category", "tag", "author", "?"))

    slug_to_url = {}
    for row in resp.get("rows", []):
        u = row["keys"][0]
        if is_post(u):
            slug = u.rstrip("/").split("/")[-1]
            slug_to_url[slug] = u

    log.info(f"Dashboard: checking {len(slug_to_url)} blog slugs against WordPress")

    posts = []
    for slug, gsc_url in slug_to_url.items():
        modified_str, title, wp_path, created_str = wp_fetch_by_slug(slug)
        if not modified_str:
            continue
        try:
            modified_date = date.fromisoformat(modified_str)
        except Exception:
            continue
        if week_start <= modified_date <= week_end:
            # Determine if new post or update
            try:
                created_date = date.fromisoformat(created_str) if created_str else None
                days_old = (modified_date - created_date).days if created_date else 999
                post_type = "new" if days_old <= 7 else "update"
            except Exception:
                post_type = "update"

            posts.append({
                "slug": slug, "title": title or slug,
                "url": wp_path or gsc_url.replace("https://www.redpoints.com", ""),
                "modified": modified_str,
                "type": post_type,
            })

    log.info(f"WordPress {week_start} - {week_end}: {len(posts)} posts updated")
    return posts


def fetch_gsc_metrics(gsc_service, urls, start, end):
    if not urls:
        return {}
    try:
        body = {"startDate": start, "endDate": end, "dimensions": ["page"], "rowLimit": 5000}
        resp = gsc_service.searchanalytics().query(siteUrl=GSC_SITE_URL, body=body).execute()
        result = {}
        for row in resp.get("rows", []):
            path = row["keys"][0].replace("https://www.redpoints.com", "").rstrip("/") or "/"
            if path in urls:
                result[path] = {
                    "clicks": int(row.get("clicks", 0)), "impressions": int(row.get("impressions", 0)),
                    "ctr": round(row.get("ctr", 0) * 100, 1), "position": round(row.get("position", 0), 1),
                }
        return result
    except Exception as e:
        log.warning(f"GSC metrics error: {e}")
        return {}


def load_dashboard():
    try:
        with open(DASHBOARD_JSON, "r") as f:
            return json.load(f)
    except Exception:
        return {"months": []}


def find_month(data, year, month):
    for i, m in enumerate(data["months"]):
        if m["year"] == year and m["month"] == month:
            return i
    return None


def write_dashboard_data(gsc_service):
    log.info("=== Writing dashboard data ===")

    week_start, week_end = last_complete_week()
    today = date.today()

    wp_posts = fetch_wp_updates(gsc_service, week_start, week_end)

    urls     = [p["url"] for p in wp_posts]
    gsc_now  = fetch_gsc_metrics(gsc_service, urls, (week_start - timedelta(days=7)).isoformat(), today.isoformat())
    prev_end = (week_start - timedelta(weeks=4)).isoformat()
    prev_start = (week_start - timedelta(weeks=5)).isoformat()
    gsc_prev = fetch_gsc_metrics(gsc_service, urls, prev_start, prev_end)
    gsc_prev_pos = {u: v["position"] for u, v in gsc_prev.items()}

    enriched = []
    for p in wp_posts:
        url = p["url"]
        g   = gsc_now.get(url, {})
        # Only include posts actually modified in the target month
        try:
            mod_month = date.fromisoformat(p["modified"]).month
            mod_year  = date.fromisoformat(p["modified"]).year
        except Exception:
            mod_month, mod_year = 0, 0
        if mod_year != week_end.year or mod_month != week_end.month:
            continue
        enriched.append({
            "title": p["title"], "url": url, "modified": p["modified"],
            "type": p.get("type", "update"),
            "clicks": g.get("clicks", 0), "impressions": g.get("impressions", 0),
            "ctr": g.get("ctr", 0.0), "position": g.get("position", 0.0),
            "prev_position": gsc_prev_pos.get(url, 0.0),
        })

    # Use week_end to assign posts to the correct month (handles cross-month weeks)
    year, month = week_end.year, week_end.month
    lbl         = week_label(week_end)
    _, days     = calendar.monthrange(year, month)

    data = load_dashboard()
    idx  = find_month(data, year, month)

    if idx is None:
        all_lbls = weeks_in_month(year, month)
        weeks    = [{"label": l, "posts": None, "is_current": False} for l in all_lbls]
        data["months"].append({
            "name": month_name(year, month), "year": year, "month": month,
            "is_current": True, "days_total": days,
            "updated": len(wp_posts), "weeks": weeks, "posts": enriched,
        })
        idx = len(data["months"]) - 1
    else:
        m = data["months"][idx]
        for w in m["weeks"]:
            w["is_current"] = (w["label"] == lbl)
            if w["label"] == lbl:
                w["posts"] = len(wp_posts)
        m["updated"] = sum(w["posts"] for w in m["weeks"] if w["posts"] is not None)
        other    = [p for p in m.get("posts", []) if week_label(date.fromisoformat(p["modified"])) != lbl]
        m["posts"] = other + enriched

    for m in data["months"]:
        m["is_current"] = (m["year"] == today.year and m["month"] == today.month)

    data["months"]      = sorted(data["months"], key=lambda m: (m["year"], m["month"]))[-6:]
    data["generated_at"] = today.isoformat()
    data["weekly_goal"]  = WEEKLY_GOAL
    data["monthly_goal"] = MONTHLY_GOAL

    with open(DASHBOARD_JSON, "w") as f:
        json.dump(data, f, indent=2)

    log.info(f"dashboard_data.json updated — {len(wp_posts)} posts for {week_start} to {week_end}")
