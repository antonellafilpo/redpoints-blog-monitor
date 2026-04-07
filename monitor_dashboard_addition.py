"""
DASHBOARD DATA WRITER
Add this to the bottom of monitor.py, and call write_dashboard_data()
at the end of main() just before the Slack notification.

Requires no new dependencies - uses google-auth and requests already installed.
"""

import json
import logging
import calendar
from datetime import datetime, timedelta, date

log = logging.getLogger(__name__)

WEEKLY_GOAL = 8
MONTHLY_GOAL = 32
DASHBOARD_JSON_PATH = "dashboard_data.json"


# ──────────────────────────────────────────────
# WordPress helpers
# ──────────────────────────────────────────────

def fetch_wordpress_by_slug(slug: str) -> tuple:
    """
    Query WordPress by slug — same approach as monitor.py which works on this site.
    Returns (modified_date_str, title, url_path) or (None, None, None).
    """
    import requests
    try:
        r = requests.get(
            "https://www.redpoints.com/wp-json/wp/v2/posts",
            params={"slug": slug, "_fields": "slug,title,link,modified"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if data:
            p = data[0]
            url = "/" + p["link"].replace("https://www.redpoints.com/", "").strip("/")
            return p["modified"][:10], p["title"]["rendered"], url
    except Exception as e:
        log.debug(f"WP slug error ({slug}): {e}")
    return None, None, None


def fetch_wordpress_updates_via_gsc(gsc_service, week_start: date, week_end: date) -> list[dict]:
    """
    Uses GSC to get blog post URLs for the week, then queries WordPress
    per slug to find which ones were modified in that date range.
    This bypasses the WP listing endpoint which is restricted on this site.
    """
    import os, requests

    site_url = os.environ.get("GSC_SITE_URL", "https://www.redpoints.com/")

    # Get all blog URLs that had impressions this week from GSC
    body = {
        "startDate": week_start.isoformat(),
        "endDate":   week_end.isoformat(),
        "dimensions": ["page"],
        "rowLimit": 5000,
        "dimensionFilterGroups": [{"filters": [{
            "dimension": "page",
            "operator":  "contains",
            "expression": "/blog/",
        }]}],
    }
    try:
        resp = gsc_service.searchanalytics().query(siteUrl=site_url, body=body).execute()
    except Exception as e:
        log.warning(f"GSC blog URL fetch error: {e}")
        return []

    # Filter to actual post slugs only (no pagination, categories, etc.)
    def is_post_url(u: str) -> bool:
        u = u.rstrip("/")
        after = u.split("/blog/")[-1].strip("/") if "/blog/" in u else ""
        if not after or "/" in after:
            return False
        if any(x in after for x in ("page", "category", "tag", "author", "?")):
            return False
        return len(after) > 3

    gsc_urls = [row["keys"][0] for row in resp.get("rows", [])]
    blog_urls = list({u.rstrip("/").split("/")[-1]: u for u in gsc_urls if is_post_url(u)}.values())
    log.info(f"Dashboard: checking {len(blog_urls)} blog slugs against WordPress")

    posts = []
    for url in blog_urls:
        slug = url.rstrip("/").split("/")[-1]
        modified_str, title, wp_url = fetch_wordpress_by_slug(slug)
        if not modified_str:
            continue
        try:
            modified_date = date.fromisoformat(modified_str)
        except Exception:
            continue
        if week_start <= modified_date <= week_end:
            path = wp_url or ("/" + url.replace("https://www.redpoints.com/", "").strip("/"))
            posts.append({
                "slug":     slug,
                "title":    title or slug,
                "url":      path,
                "modified": modified_str,
            })

    log.info(f"WordPress {week_start} – {week_end}: {len(posts)} posts updated")
    return posts


def fetch_gsc_metrics_for_urls(gsc_service, urls: list[str], start: str, end: str) -> dict:
    """
    Fetch clicks, impressions, ctr, position from GSC for a list of URLs.
    Returns dict keyed by URL path.
    """
    if not urls:
        return {}

    site_url = os.environ.get("GSC_SITE_URL", "https://www.redpoints.com/")
    metrics = {}

    try:
        body = {
            "startDate": start,
            "endDate": end,
            "dimensions": ["page"],
            "dimensionFilterGroups": [{
                "filters": [{
                    "dimension": "page",
                    "operator": "includingRegex",
                    "expression": "|".join(u.lstrip("/") for u in urls[:50])
                }]
            }],
            "rowLimit": 500,
        }
        resp = gsc_service.searchanalytics().query(
            siteUrl=site_url, body=body
        ).execute()

        for row in resp.get("rows", []):
            path = row["keys"][0].replace("https://www.redpoints.com", "")
            metrics[path] = {
                "clicks":      int(row.get("clicks", 0)),
                "impressions": int(row.get("impressions", 0)),
                "ctr":         round(row.get("ctr", 0) * 100, 1),
                "position":    round(row.get("position", 0), 1),
            }
    except Exception as e:
        log.warning(f"GSC metrics fetch error: {e}")

    return metrics


def fetch_gsc_prev_position(gsc_service, urls: list[str]) -> dict:
    """
    Fetch position for the same URLs 4 weeks ago (for trend arrows).
    """
    end = (datetime.utcnow() - timedelta(weeks=4)).strftime("%Y-%m-%d")
    start = (datetime.utcnow() - timedelta(weeks=5)).strftime("%Y-%m-%d")
    metrics = fetch_gsc_metrics_for_urls(gsc_service, urls, start, end)
    return {url: m["position"] for url, m in metrics.items()}


# ──────────────────────────────────────────────
# Month/week bucketing
# ──────────────────────────────────────────────

def month_key(d: date) -> str:
    return d.strftime("%B %Y")


def week_label_in_month(d: date) -> str:
    """Return 'Apr w1', 'Apr w2' etc. for the week containing date d."""
    month_abbr = d.strftime("%b")
    day = d.day
    week_num = (day - 1) // 7 + 1
    return f"{month_abbr} w{week_num}"


def weeks_in_month(year: int, month: int) -> list[str]:
    """Return all week labels for a given month."""
    _, days = calendar.monthrange(year, month)
    labels = []
    seen = []
    for day in range(1, days + 1):
        d = date(year, month, day)
        label = week_label_in_month(d)
        if label not in seen:
            seen.append(label)
            labels.append(label)
    return labels


def days_elapsed_in_month(year: int, month: int) -> int:
    today = date.today()
    if today.year == year and today.month == month:
        return today.day
    _, days = calendar.monthrange(year, month)
    return days


# ──────────────────────────────────────────────
# Load + merge existing dashboard data
# ──────────────────────────────────────────────

def load_existing_dashboard() -> dict:
    try:
        with open(DASHBOARD_JSON_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {"months": []}


def month_index(data: dict, year: int, month: int) -> int | None:
    for i, m in enumerate(data["months"]):
        if m["year"] == year and m["month"] == month:
            return i
    return None


# ──────────────────────────────────────────────
# Main dashboard writer
# ──────────────────────────────────────────────

def write_dashboard_data(gsc_service) -> None:
    """
    Called from main() once per week. Fetches the LAST COMPLETE week's WordPress updates
    + GSC metrics and writes/updates dashboard_data.json.
    Uses the same week logic as monitor.py — always last complete Mon–Sun.
    """
    log.info("=== Writing dashboard data ===")

    today = date.today()
    # Last complete Mon–Sun week (same logic as monitor.py)
    days_since_monday = today.weekday()  # Mon=0, Sun=6
    week_end   = today - timedelta(days=days_since_monday + 1)  # last Sunday
    week_start = week_end - timedelta(days=6)                   # last Monday

    # Pull WordPress updates using GSC URLs (listing endpoint is restricted on this site)
    wp_posts = fetch_wordpress_updates_via_gsc(gsc_service, week_start, week_end)
    log.info(f"WordPress: {len(wp_posts)} posts updated last week")

    # Pull GSC metrics for updated URLs
    urls = [p["url"] for p in wp_posts]
    gsc_current  = fetch_gsc_metrics_for_urls(
        gsc_service, urls,
        (week_start - timedelta(days=7)).isoformat(),
        today.isoformat(),
    )
    gsc_prev_pos = fetch_gsc_prev_position(gsc_service, urls)

    # Enrich posts with GSC data
    enriched = []
    for p in wp_posts:
        url = p["url"]
        gsc = gsc_current.get(url, {})
        enriched.append({
            "title":        p["title"],
            "url":          url,
            "modified":     p["modified"],
            "clicks":       gsc.get("clicks", 0),
            "impressions":  gsc.get("impressions", 0),
            "ctr":          gsc.get("ctr", 0.0),
            "position":     gsc.get("position", 0.0),
            "prev_position": gsc_prev_pos.get(url, 0.0),
        })

    # Load existing data and update
    data = load_existing_dashboard()
    year, month = today.year, today.month
    _, days_in_month = calendar.monthrange(year, month)
    week_lbl = week_label_in_month(week_start)
    all_week_labels = weeks_in_month(year, month)

    idx = month_index(data, year, month)
    if idx is None:
        # New month entry
        weeks_empty = [{"label": lbl, "posts": None, "is_current": False}
                       for lbl in all_week_labels]
        data["months"].append({
            "name":       month_key(today),
            "year":       year,
            "month":      month,
            "is_current": True,
            "days_total": days_in_month,
            "updated":    len(wp_posts),
            "weeks":      weeks_empty,
            "posts":      enriched,
        })
        idx = len(data["months"]) - 1
    else:
        m = data["months"][idx]
        # Update the week bucket
        for w in m["weeks"]:
            if w["label"] == week_lbl:
                w["posts"] = len(wp_posts)
                w["is_current"] = True
            else:
                if w["posts"] is not None:
                    w["is_current"] = False
        # Accumulate total (sum all non-None weeks)
        m["updated"] = sum(w["posts"] for w in m["weeks"] if w["posts"] is not None)
        # Replace this week's posts
        existing_other = [p for p in m.get("posts", [])
                          if p["modified"][:7] != today.strftime("%Y-%m")
                          or week_label_in_month(date.fromisoformat(p["modified"])) != week_lbl]
        m["posts"] = existing_other + enriched

    # Mark previous months as not current
    for i, m in enumerate(data["months"]):
        m["is_current"] = (m["year"] == year and m["month"] == month)

    # Keep only last 6 months
    data["months"] = sorted(
        data["months"], key=lambda m: (m["year"], m["month"])
    )[-6:]

    data["generated_at"] = datetime.utcnow().isoformat()
    data["weekly_goal"]  = WEEKLY_GOAL
    data["monthly_goal"] = MONTHLY_GOAL

    with open(DASHBOARD_JSON_PATH, "w") as f:
        json.dump(data, f, indent=2)

    log.info(f"dashboard_data.json written — {len(wp_posts)} posts this week")


# ──────────────────────────────────────────────
# HOW TO INTEGRATE INTO YOUR EXISTING main()
# ──────────────────────────────────────────────
#
# Inside main(), after build_gsc_service(), add:
#
#   gsc_service = build_gsc_service()
#   write_dashboard_data(gsc_service)   # <-- add this line
#   gsc_current = fetch_gsc_clicks(...)
#
# Also add dashboard_data.json to your GitHub Actions deploy step.
# In blog_monitor.yml, under the pages deploy step, include:
#
#   files: |
#     dashboard.html
#     dashboard_data.json
#     index.html          # your existing monitor output
