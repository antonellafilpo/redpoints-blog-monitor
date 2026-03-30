"""
Red Points Blog Monitor
=======================
Weekly script that:
  1. Fetches last_updated dates from WordPress REST API for all KEEP posts
  2. Pulls Google Search Console data for KEEP posts
  3. Pulls Omnia citation data per blog post URL
  4. Applies 3 flags: traffic drop, LLM drop, stale content
  5. Generates a filterable HTML report and saves weekly JSON data
  6. Sends 3-bullet executive summary to Slack (#blog-monitor)
  7. Emails the HTML report to the distribution list

Flags:
  🔴 Traffic drop  — weekly clicks ≥30% below 12-week average AND absolute drop ≥50 clicks
  🟡 LLM drop      — citations drop ≥15 AND ≥40% week over week (Omnia)
  📅 Stale content — not updated in 6+ months AND post score ≥10/14

Timing rules (per PDF best practices):
  - Always analyse last complete Mon–Sun week (never current partial week)
  - Respect GSC 3-day reporting lag
  - Suppress traffic alerts for first 4 weeks (baseline not yet reliable)
  - 6-week cooldown for score 12–14 posts after update (high-value, needs clean signal)
  - 4-week cooldown for score 8–11 posts after update
  - 4-week cooldown for newly merged posts after 301 redirect
  - Exclude seasonal periods from baseline AND suppress alerts during them
  - WordPress API called at start of run (scheduled 6am UTC = low traffic window)

Run: python monitor.py
Deploy: GitHub Actions (see blog_monitor.yml)
"""

import os
import json
import datetime
import logging
import smtplib
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
import asana
from asana.rest import ApiException

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

GSC_SERVICE_ACCOUNT_FILE = os.getenv("GSC_SERVICE_ACCOUNT_FILE", "gsc_service_account.json")
GSC_SITE_URL             = os.getenv("GSC_SITE_URL", "https://www.redpoints.com/")

ASANA_TOKEN              = os.getenv("ASANA_TOKEN")
ASANA_PROJECT_GID        = os.getenv("ASANA_PROJECT_GID")
ASANA_WORKSPACE_GID      = os.getenv("ASANA_WORKSPACE_GID")

SLACK_WEBHOOK_URL        = os.getenv("SLACK_WEBHOOK_URL")
REPORT_URL               = os.getenv("REPORT_URL", "https://lwoue-collab.github.io/redpoints-blog-monitor")

GMAIL_SENDER             = os.getenv("GMAIL_SENDER", "lwoue@redpoints.com")
GMAIL_APP_PASSWORD       = os.getenv("GMAIL_APP_PASSWORD")
GMAIL_RECIPIENTS         = os.getenv("GMAIL_RECIPIENTS", "afilpo@redpoints.com,wbecerra@redpoints.com")

OMNIA_TOKEN              = os.getenv("OMNIA_TOKEN")
OMNIA_BRAND_ID           = os.getenv("OMNIA_BRAND_ID", "03adaaca-5265-404e-b4b1-bbaea0ce73f9")

ANTHROPIC_API_KEY        = os.getenv("ANTHROPIC_API_KEY")

# WordPress REST API — auto-fetches last_updated for each post
# No credentials needed — public API for published posts
WP_API_BASE              = os.getenv("WP_API_BASE", "https://www.redpoints.com/wp-json/wp/v2/posts")
WP_API_TIMEOUT           = 10  # seconds per request

DATA_DIR                 = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# Alert thresholds
TRAFFIC_DROP_PCT         = float(os.getenv("TRAFFIC_DROP_PCT", "0.30"))    # 30%
TRAFFIC_DROP_ABS         = float(os.getenv("TRAFFIC_DROP_ABS", "50"))      # 50 clicks
LLM_DROP_ABS             = int(os.getenv("LLM_DROP_ABS", "15"))            # 15 citations
LLM_DROP_PCT             = float(os.getenv("LLM_DROP_PCT", "0.40"))        # 40%
STALE_MONTHS             = int(os.getenv("STALE_MONTHS", "6"))             # 6 months
STALE_MIN_SCORE          = int(os.getenv("STALE_MIN_SCORE", "10"))         # score ≥10/14

# Minimum weeks of GSC baseline before traffic alerts fire
MIN_BASELINE_WEEKS       = 4


# ---------------------------------------------------------------------------
# SEASONAL EXCLUSIONS
# Format: (month-day start, month-day end) — spans year boundary if start > end
# ---------------------------------------------------------------------------

SEASONAL_PERIODS = [
    {"name": "Christmas / New Year", "start": (12, 20), "end": (1, 10)},
    {"name": "Summer",               "start": (7,  15), "end": (8, 31)},
    {"name": "Thanksgiving",         "start": (11, 25), "end": (12, 1)},
    # Easter is calculated dynamically below
]

def easter_dates(year):
    """Returns Good Friday and Easter Monday dates for given year."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    easter_sunday = datetime.date(year, month, day)
    good_friday   = easter_sunday - datetime.timedelta(days=2)
    easter_monday = easter_sunday + datetime.timedelta(days=1)
    return good_friday, easter_monday


def is_seasonal(check_date: datetime.date) -> str | None:
    """Returns season name if date falls in a seasonal period, else None."""
    year = check_date.year

    # Easter — dynamic
    good_friday, easter_monday = easter_dates(year)
    if good_friday <= check_date <= easter_monday:
        return "Easter"

    for period in SEASONAL_PERIODS:
        sy, sm = period["start"]
        ey, em = period["end"]
        start = datetime.date(year, sy, sm)
        # Handle year boundary (e.g. Dec 20 – Jan 10)
        if sy > ey or (sy == ey and sm > em):
            end = datetime.date(year + 1, ey, em)
        else:
            end = datetime.date(year, ey, em)
        if start <= check_date <= end:
            return period["name"]

    return None


# ---------------------------------------------------------------------------
# WORDPRESS API — auto-fetch last_updated for each KEEP post
# ---------------------------------------------------------------------------

def fetch_wordpress_last_updated() -> dict[str, str]:
    """
    Queries WordPress REST API for the modified date of every KEEP post.
    Returns a dict of {slug: "YYYY-MM-DD"}.
    Falls back to hardcoded last_updated in KEEP_POSTS if the API call fails.
    Called once at the start of the pipeline (6am UTC = low-traffic window).
    """
    wp_dates = {}
    slugs = [path.strip("/").split("/")[-1] for path in KEEP_POSTS.keys()]
    log.info(f"Fetching WordPress last_updated for {len(slugs)} posts...")

    for slug in slugs:
        try:
            resp = requests.get(
                WP_API_BASE,
                params={"slug": slug, "_fields": "slug,modified"},
                timeout=WP_API_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            if data and isinstance(data, list) and len(data) > 0:
                modified_raw = data[0].get("modified", "")
                if modified_raw:
                    # WordPress returns ISO 8601: "2026-03-27T12:19:35"
                    wp_dates[slug] = modified_raw[:10]
                    log.debug(f"  WP {slug}: {modified_raw[:10]}")
        except Exception as e:
            log.warning(f"  WP API failed for {slug}: {e} — will use hardcoded date")

    log.info(f"WordPress dates fetched for {len(wp_dates)}/{len(slugs)} posts")
    return wp_dates


def get_last_updated(path: str, wp_dates: dict) -> str | None:
    """
    Returns the best available last_updated date for a post.
    Priority: WordPress API (live) > hardcoded in KEEP_POSTS (fallback).
    """
    slug = path.strip("/").split("/")[-1]
    if slug in wp_dates:
        return wp_dates[slug]
    # Fallback to hardcoded value
    return KEEP_POSTS.get(path, {}).get("last_updated")


# ---------------------------------------------------------------------------
# KEEP POSTS
# Loaded from blog audit. Each entry: URL path → metadata
# last_updated is used as fallback only — live date comes from WordPress API.
# Add update_cooldown_until when forcing a manual cooldown override.
# Add merge_cooldown_until when a post completes a 301 merge.
# Cooldown periods:
#   - Score 12–14 (Tier 1): 6 weeks after update (needs clean traffic signal)
#   - Score 8–11  (Tier 2): 4 weeks after update
#   - Merged posts:         4 weeks after 301 redirect goes live
# ---------------------------------------------------------------------------

KEEP_POSTS = {
    "/blog/how-to-take-down-a-fake-website/": {
        "title": "How to take down a fake website before it destroys your brand",
        "cluster": "Website Takedown", "score": 14,
        "last_updated": "2025-02-04", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/report-infringement-amazon/": {
        "title": "How to report copyright and trademark infringement on Amazon",
        "cluster": "Marketplace Protection", "score": 14,
        "last_updated": "2025-02-06", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/unauthorized-sellers-on-walmart/": {
        "title": "How to remove unauthorized sellers on Walmart Marketplace",
        "cluster": "Marketplace Protection", "score": 14,
        "last_updated": "2023-03-27", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/cloudflare-dmca-takedown/": {
        "title": "How to effectively submit DMCA takedown request to Cloudflare",
        "cluster": "Copyright Infringement", "score": 13,
        "last_updated": "2025-01-28", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/how-to-track-a-fake-instagram-account/": {
        "title": "Smart way to track a fake Instagram account",
        "cluster": "Social Media Takedown", "score": 13,
        "last_updated": "2025-11-03", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/how-to-legally-take-down-a-website/": {
        "title": "How to legally take down a website: 5 expert-approved steps",
        "cluster": "Website Takedown", "score": 13,
        "last_updated": "2025-09-19", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/legal-action-against-counterfeit-goods/": {
        "title": "How to take legal action against counterfeit goods' sellers",
        "cluster": "Counterfeit Goods Protection", "score": 13,
        "last_updated": "2024-11-21", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/how-to-get-someones-tiktok-video-taken-down/": {
        "title": "How to get someone else's TikTok video taken down",
        "cluster": "Social Media Takedown", "score": 13,
        "last_updated": "2025-11-03", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/tiktok-dmca-takedown/": {
        "title": "How to successfully remove stolen content from TikTok with a DMCA takedown",
        "cluster": "Copyright Infringement", "score": 12,
        "last_updated": "2025-05-08", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/how-to-report-a-scammer-on-telegram/": {
        "title": "How to report a scammer on Telegram",
        "cluster": "Platform Scams", "score": 12,
        "last_updated": "2025-06-01", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/how-to-report-copyright-infringement-on-tiktok/": {
        "title": "How to report a copyright infringement on TikTok",
        "cluster": "Social Media Takedown", "score": 12,
        "last_updated": "2025-04-15", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/how-to-file-a-dmca-takedown-notice/": {
        "title": "How to file a DMCA takedown notice to Google to stop copyright infringement",
        "cluster": "Copyright Infringement", "score": 12,
        "last_updated": "2025-03-10", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/website-cloning/": {
        "title": "Website cloning: How to identify, prevent, and respond",
        "cluster": "Website Takedown", "score": 12,
        "last_updated": "2025-01-15", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/ban-fake-instagram-accounts/": {
        "title": "A step-by-step guide to banning fake Instagram accounts permanently",
        "cluster": "Social Media Takedown", "score": 12,
        "last_updated": "2025-08-20", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/alibaba-scams/": {
        "title": "9 tips to avoid Alibaba scams",
        "cluster": "Marketplace Protection", "score": 12,
        "last_updated": "2025-02-01", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/how-to-take-down-a-tiktok-account/": {
        "title": "How to take down a TikTok account",
        "cluster": "Social Media Takedown", "score": 12,
        "last_updated": "2025-05-20", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/how-to-report-and-take-down-a-fraud-website/": {
        "title": "How to report and take down a fraud website: a step-by-step guide",
        "cluster": "Website Takedown", "score": 12,
        "last_updated": "2025-03-01", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/best-brand-protection-software/": {
        "title": "7 Best Brand Protection Tools for 2026: Ranked & reviewed",
        "cluster": "Brand Protection", "score": 10,
        "last_updated": "2026-01-15", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    # Add remaining KEEP posts here following the same pattern
}

# Cluster → Asana assignee GID mapping
CLUSTER_ASSIGNEES = {
    "Website Takedown":           "ASANA_USER_GID_DANIEL",
    "Social Media Takedown":      "ASANA_USER_GID_DANIEL",
    "Copyright Infringement":     "ASANA_USER_GID_DANIEL",
    "Marketplace Protection":     "ASANA_USER_GID_TEAM",
    "Counterfeit Goods Protection": "ASANA_USER_GID_TEAM",
    "Brand Protection":           "ASANA_USER_GID_TEAM",
    "Platform Scams":             "ASANA_USER_GID_DANIEL",
}

TIER_DUE_DAYS = {"tier1": 7, "tier2": 14, "tier3": 30}

def get_tier(score):
    if score >= 12: return "tier1"
    if score >= 8:  return "tier2"
    return "tier3"

def due_date(tier):
    days = TIER_DUE_DAYS.get(tier, 14)
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# DATE HELPERS
# ---------------------------------------------------------------------------

def last_complete_week() -> tuple[datetime.date, datetime.date]:
    """Returns (Monday, Sunday) of the last complete Mon–Sun week,
    accounting for the GSC 3-day reporting lag."""
    today = datetime.date.today()
    # Find last Sunday that ended at least 3 days ago
    days_since_sunday = (today.weekday() + 1) % 7
    last_sunday = today - datetime.timedelta(days=days_since_sunday)
    if (today - last_sunday).days < 3:
        last_sunday -= datetime.timedelta(weeks=1)
    last_monday = last_sunday - datetime.timedelta(days=6)
    return last_monday, last_sunday


def week_key(monday: datetime.date) -> str:
    return monday.strftime("%Y-%m-%d")


def date_range_for_week_n(current_monday: datetime.date, weeks_ago: int) -> tuple[datetime.date, datetime.date]:
    """Returns (monday, sunday) for N weeks before current_monday."""
    monday = current_monday - datetime.timedelta(weeks=weeks_ago)
    sunday = monday + datetime.timedelta(days=6)
    return monday, sunday


def load_historical_data() -> dict:
    """Loads all previously saved weekly JSON files."""
    history = {}
    for f in sorted(DATA_DIR.glob("week-*.json")):
        try:
            with open(f) as fp:
                data = json.load(fp)
            history[data["week_start"]] = data
        except Exception as e:
            log.warning(f"Could not load {f}: {e}")
    return history


def save_week_data(week_start: str, data: dict):
    path = DATA_DIR / f"week-{week_start}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    log.info(f"Saved week data to {path}")


# ---------------------------------------------------------------------------
# GOOGLE SEARCH CONSOLE
# ---------------------------------------------------------------------------

def build_gsc_service():
    creds = service_account.Credentials.from_service_account_file(
        GSC_SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/webmasters.readonly"],
    )
    return build("searchconsole", "v1", credentials=creds)


def fetch_gsc_clicks(service, start_date: str, end_date: str) -> dict[str, float]:
    """Fetches clicks per blog post URL for a given date range."""
    body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": ["page"],
        "rowLimit": 5000,
        "dimensionFilterGroups": [{"filters": [{
            "dimension": "page",
            "operator": "contains",
            "expression": "/blog/",
        }]}],
    }
    try:
        response = service.searchanalytics().query(
            siteUrl=GSC_SITE_URL, body=body
        ).execute()
        rows = response.get("rows", [])
        result = {}
        for row in rows:
            path = row["keys"][0].replace(GSC_SITE_URL.rstrip("/"), "")
            result[path] = row.get("clicks", 0)
        log.info(f"GSC: {len(result)} URLs fetched ({start_date} → {end_date})")
        return result
    except Exception as e:
        log.error(f"GSC fetch failed: {e}")
        return {}


def get_12_week_average(path: str, history: dict, current_monday: datetime.date) -> float | None:
    """Calculates the average weekly clicks for a post over the last 12 weeks,
    excluding seasonal periods and the current week."""
    weekly_clicks = []
    for weeks_ago in range(1, 13):
        monday, sunday = date_range_for_week_n(current_monday, weeks_ago)
        wk = week_key(monday)
        season = is_seasonal(monday)
        if season:
            log.debug(f"Excluding {wk} from baseline ({season})")
            continue
        if wk in history:
            clicks = history[wk].get("gsc_data", {}).get(path, 0)
            weekly_clicks.append(clicks)
    if len(weekly_clicks) < 2:
        return None
    return sum(weekly_clicks) / len(weekly_clicks)


# ---------------------------------------------------------------------------
# OMNIA
# ---------------------------------------------------------------------------

def fetch_omnia_citations(start_date: str, end_date: str) -> dict[str, int]:
    """Fetches citation counts per owned blog URL from Omnia API."""
    if not OMNIA_TOKEN:
        log.warning("OMNIA_TOKEN not set — skipping LLM flag")
        return {}

    url = f"https://app.useomnia.com/api/v1/brands/{OMNIA_BRAND_ID}/citations/aggregates"
    headers = {"Authorization": f"Bearer {OMNIA_TOKEN}"}
    params = {
        "startDate": start_date,
        "endDate": end_date,
        "sourceType": "owned",
        "pageSize": 100,
        "sortBy": "total_citations",
        "sortDirection": "desc",
    }

    result = {}
    page = 1
    while True:
        params["page"] = page
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for agg in data.get("data", {}).get("aggregates", []):
                post_url = agg.get("url", "")
                if "/blog/" in post_url:
                    path = "/" + post_url.split("redpoints.com/")[1] if "redpoints.com/" in post_url else post_url
                    result[path] = agg.get("totalCitations", 0)
            total = data.get("pagination", {}).get("totalItems", 0)
            if page * 100 >= total:
                break
            page += 1
        except Exception as e:
            log.error(f"Omnia fetch failed (page {page}): {e}")
            break

    log.info(f"Omnia: {len(result)} blog URLs with citations ({start_date} → {end_date})")
    return result


# ---------------------------------------------------------------------------
# FLAG LOGIC
# ---------------------------------------------------------------------------

def check_in_cooldown(meta: dict, today: datetime.date) -> str | None:
    """Returns cooldown reason if post is in a cooldown period."""
    for field, reason in [
        ("update_cooldown_until", "recently updated"),
        ("merge_cooldown_until", "recently merged"),
    ]:
        cooldown = meta.get(field)
        if cooldown:
            cooldown_date = datetime.date.fromisoformat(cooldown)
            if today <= cooldown_date:
                return reason
    return None


def run_flags(
    current_monday: datetime.date,
    gsc_current: dict,
    omnia_current: dict,
    omnia_previous: dict,
    history: dict,
    baseline_weeks_available: int,
    wp_dates: dict | None = None,
) -> list[dict]:
    """Runs all 3 flags across KEEP posts and returns list of flagged posts."""
    today = datetime.date.today()
    season = is_seasonal(current_monday)
    flagged = []
    if wp_dates is None:
        wp_dates = {}

    for path, meta in KEEP_POSTS.items():
        full_url = GSC_SITE_URL.rstrip("/") + path
        flags = []

        # ── Cooldown check ──────────────────────────────────────────────────
        cooldown_reason = check_in_cooldown(meta, today)
        if cooldown_reason:
            log.debug(f"Skipping {path} — {cooldown_reason}")
            continue

        # ── FLAG 1: Traffic drop ─────────────────────────────────────────────
        if season:
            log.debug(f"Traffic flag suppressed for {path} — {season}")
        elif baseline_weeks_available < MIN_BASELINE_WEEKS:
            log.debug(f"Traffic flag suppressed — only {baseline_weeks_available} baseline weeks available")
        else:
            avg_clicks = get_12_week_average(path, history, current_monday)
            current_clicks = gsc_current.get(path, 0)
            if avg_clicks and avg_clicks > 0:
                drop_pct = (avg_clicks - current_clicks) / avg_clicks
                drop_abs = avg_clicks - current_clicks
                if drop_pct >= TRAFFIC_DROP_PCT and drop_abs >= TRAFFIC_DROP_ABS:
                    flags.append({
                        "type": "traffic",
                        "label": "🔴 Traffic Drop",
                        "detail": (
                            f"Clicks dropped to {current_clicks:.0f} this week vs "
                            f"{avg_clicks:.0f} 12-week average "
                            f"(−{drop_pct*100:.0f}%, −{drop_abs:.0f} clicks)"
                        ),
                        "current": current_clicks,
                        "baseline": round(avg_clicks, 1),
                        "drop_pct": round(drop_pct * 100, 1),
                    })

        # ── FLAG 2: LLM citations drop ───────────────────────────────────────
        curr_citations = omnia_current.get(path, 0)
        prev_citations = omnia_previous.get(path, 0)
        if prev_citations > 0:
            citation_drop_abs = prev_citations - curr_citations
            citation_drop_pct = citation_drop_abs / prev_citations
            if citation_drop_abs >= LLM_DROP_ABS and citation_drop_pct >= LLM_DROP_PCT:
                flags.append({
                    "type": "llm",
                    "label": "🟡 LLM Visibility Drop",
                    "detail": (
                        f"LLM citations dropped from {prev_citations} → {curr_citations} "
                        f"(−{citation_drop_abs}, −{citation_drop_pct*100:.0f}%)"
                    ),
                    "current": curr_citations,
                    "previous": prev_citations,
                    "drop_pct": round(citation_drop_pct * 100, 1),
                })

        # ── FLAG 3: Stale content ────────────────────────────────────────────
        # Uses WordPress API date if available, falls back to hardcoded last_updated
        last_updated_str = get_last_updated(path, wp_dates)
        if last_updated_str and meta.get("score", 0) >= STALE_MIN_SCORE:
            last_updated = datetime.date.fromisoformat(last_updated_str)
            months_since = (today - last_updated).days / 30
            source = "WP" if path.strip("/").split("/")[-1] in wp_dates else "audit"
            if months_since >= STALE_MONTHS:
                flags.append({
                    "type": "stale",
                    "label": "📅 Stale Content",
                    "detail": (
                        f"Not updated in {months_since:.0f} months "
                        f"(last updated: {last_updated_str} [{source}], score {meta['score']}/14)"
                    ),
                    "months_since_update": round(months_since, 1),
                    "last_updated": last_updated_str,
                    "last_updated_source": source,
                })

        if flags:
            flagged.append({
                "path": path,
                "url": full_url,
                "title": meta["title"],
                "cluster": meta["cluster"],
                "score": meta.get("score", 0),
                "tier": get_tier(meta.get("score", 0)),
                "flags": flags,
                "gsc_clicks_this_week": gsc_current.get(path, 0),
                "llm_citations_this_week": omnia_current.get(path, 0),
            })

    flagged.sort(key=lambda x: -x["score"])
    log.info(f"{len(flagged)} posts flagged this week")
    return flagged


# ---------------------------------------------------------------------------
# CLAUDE API — EXECUTIVE SUMMARY
# ---------------------------------------------------------------------------

def generate_executive_summary(flagged: list[dict], week_end: str, season: str | None) -> str:
    """Calls Claude API to generate a 3-bullet plain-language summary."""
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY not set — skipping executive summary")
        return "Executive summary unavailable — API key not configured."

    if season:
        return f"⏸️ No alerts this week — {season} period excluded from baseline. All monitoring resumes next week."

    if not flagged:
        return "✅ All KEEP posts are stable this week. No traffic drops, LLM visibility losses, or stale content alerts. No action needed."

    flags_summary = []
    for p in flagged[:10]:
        for f in p["flags"]:
            flags_summary.append(f"- [{f['label']}] {p['title']} ({p['cluster']}): {f['detail']}")

    prompt = f"""You are a content analyst at Red Points, a brand protection company.
Below are the blog post anomalies detected this week (week ending {week_end}).
Write exactly 3 bullet points summarising the most important findings for the marketing team.
Rules:
- Plain language only — no jargon, no technical terms
- Each bullet: what happened + why it matters + what to do
- Keep each bullet to 1-2 sentences maximum
- Start each bullet with an emoji (🔴 for urgent, 🟡 for attention, 📅 for scheduled)
- Do NOT include headers, preamble or postamble — just the 3 bullets

Flagged posts this week:
{chr(10).join(flags_summary)}"""

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        text = "".join(
            block["text"] for block in data.get("content", [])
            if block.get("type") == "text"
        )
        log.info("Executive summary generated")
        return text.strip()
    except Exception as e:
        log.error(f"Claude API failed: {e}")
        return f"Executive summary unavailable this week ({len(flagged)} posts flagged — see full report)."


# ---------------------------------------------------------------------------
# ASANA TASKS
# ---------------------------------------------------------------------------

def create_asana_tasks(flagged: list[dict], week_end: str):
    """Creates Asana tasks for flagged posts."""
    if not ASANA_TOKEN or not flagged:
        return

    configuration = asana.Configuration()
    configuration.access_token = ASANA_TOKEN
    client = asana.ApiClient(configuration)
    tasks_api = asana.TasksApi(client)

    for post in flagged:
        flags_text = "\n".join(f"  • {f['detail']}" for f in post["flags"])
        notes = f"""🚨 Blog post flagged — week ending {week_end}

URL: {post['url']}
Cluster: {post['cluster']}
Score: {post['score']}/14

Flags triggered:
{flags_text}

--- Freshness checklist ---
[ ] Stats/data points still accurate?
[ ] Platform UI screenshots still current?
[ ] Year in title/meta is correct?
[ ] Internal links pointing to live posts?
[ ] External links still live?
[ ] Yoast score ≥ 70?
[ ] Meta description still matches content?
[ ] Resubmit to GSC after update
"""
        assignee_gid = CLUSTER_ASSIGNEES.get(post["cluster"])
        task_body = {
            "data": {
                "name": f"[Blog Review] {post['title']}",
                "notes": notes,
                "projects": [ASANA_PROJECT_GID],
                "due_on": due_date(post["tier"]),
                **({"assignee": assignee_gid} if assignee_gid else {}),
            }
        }
        try:
            tasks_api.create_task(task_body, {})
            log.info(f"Asana task created: {post['title']}")
        except ApiException as e:
            log.error(f"Asana task failed for {post['path']}: {e}")


# ---------------------------------------------------------------------------
# SLACK
# ---------------------------------------------------------------------------

def send_slack_alert(summary: str, flagged: list[dict], week_end: str, season: str | None):
    """Sends 3-bullet summary + report link to #blog-monitor."""
    if not SLACK_WEBHOOK_URL:
        log.warning("SLACK_WEBHOOK_URL not set — skipping Slack")
        return

    if season:
        payload = {"text": f"⏸️ *Blog Monitor — Week ending {week_end}*\n{summary}"}
    elif not flagged:
        payload = {
            "text": (
                f"✅ *Blog Monitor — Week ending {week_end}*\n"
                f"{summary}\n\n"
                f"<{REPORT_URL}|View live report>"
            )
        }
    else:
        traffic = sum(1 for p in flagged for f in p["flags"] if f["type"] == "traffic")
        llm     = sum(1 for p in flagged for f in p["flags"] if f["type"] == "llm")
        stale   = sum(1 for p in flagged for f in p["flags"] if f["type"] == "stale")

        payload = {
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"🚨 *Blog Monitor — Week ending {week_end}*\n"
                            f"*{len(flagged)} post(s) need attention* "
                            f"— 🔴 {traffic} traffic · 🟡 {llm} LLM · 📅 {stale} stale\n\n"
                            f"{summary}\n\n"
                            f"<{REPORT_URL}|→ View full live report>"
                        )
                    }
                }
            ]
        }

    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code == 200:
            log.info("Slack alert sent")
        else:
            log.error(f"Slack failed: {resp.status_code} {resp.text}")
    except Exception as e:
        log.error(f"Slack error: {e}")


# ---------------------------------------------------------------------------
# EMAIL
# ---------------------------------------------------------------------------

def build_email_body(flagged: list[dict], week_end: str, summary: str, season: str | None) -> str:
    """Builds a clean inline HTML email body — no JS, renders in Gmail/Outlook/Apple Mail."""
    traffic = sum(1 for p in flagged for f in p["flags"] if f["type"] == "traffic")
    llm     = sum(1 for p in flagged for f in p["flags"] if f["type"] == "llm")
    stale   = sum(1 for p in flagged for f in p["flags"] if f["type"] == "stale")
    stable  = len(KEEP_POSTS) - len(flagged)

    # Summary bullets — convert newlines to <br> for HTML
    summary_html = summary.replace("\n", "<br>") if summary else ""

    # Post cards — show first 5, note remainder
    post_cards = ""
    for p in flagged[:5]:
        for f in p["flags"]:
            badge_color = {"traffic": "#dc2626", "llm": "#d97706", "stale": "#2563eb"}.get(f["type"], "#64748b")
            badge_bg    = {"traffic": "#fef2f2",  "llm": "#fffbeb", "stale": "#eff6ff"}.get(f["type"], "#f8fafc")
            badge_label = {"traffic": "Traffic drop", "llm": "LLM drop", "stale": "Stale content"}.get(f["type"], f["type"])
            post_cards += f"""
            <tr>
              <td style="padding:0 0 10px 0;">
                <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e2e8f0;border-radius:8px;overflow:hidden;">
                  <tr>
                    <td style="background:{badge_bg};padding:8px 14px;border-bottom:1px solid #e2e8f0;">
                      <span style="font-size:11px;color:{badge_color};font-weight:600;">{badge_label}</span>
                      <span style="float:right;font-size:11px;color:#64748b;font-family:monospace;">Score {p['score']}/14</span>
                    </td>
                  </tr>
                  <tr>
                    <td style="background:#ffffff;padding:10px 14px;">
                      <div style="font-size:13px;font-weight:600;color:#0f172a;margin-bottom:4px;">{p['title']}</div>
                      <div style="font-size:12px;color:#64748b;">{f['detail']}</div>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>"""

    more_text = ""
    if len(flagged) > 5:
        more_text = f'<p style="text-align:center;font-size:12px;color:#94a3b8;margin:0 0 20px 0;">+ {len(flagged) - 5} more posts — see full report</p>'

    season_banner = ""
    if season:
        season_banner = f'<tr><td style="padding:0 0 20px 0;"><div style="background:#fef9c3;border-radius:8px;padding:12px 16px;font-size:13px;color:#713f12;">⏸️ Alert suppression active — {season} period. Traffic baseline paused.</div></td></tr>'

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f8fafc;padding:24px 0;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

          <!-- Header -->
          <tr>
            <td style="background:#0f172a;border-radius:10px 10px 0 0;padding:24px 28px;">
              <div style="color:#ffffff;font-size:16px;font-weight:600;">Red Points — blog monitor</div>
              <div style="color:#94a3b8;font-size:12px;margin-top:4px;">Week of {week_end} &nbsp;·&nbsp; KEEP posts only</div>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="background:#ffffff;border-left:1px solid #e2e8f0;border-right:1px solid #e2e8f0;padding:24px 28px;">
              <table width="100%" cellpadding="0" cellspacing="0">

                {season_banner}

                <!-- Summary cards -->
                <tr>
                  <td style="padding:0 0 20px 0;">
                    <table width="100%" cellpadding="0" cellspacing="0">
                      <tr>
                        <td width="25%" style="padding-right:8px;">
                          <div style="background:#fef2f2;border-radius:8px;padding:14px;text-align:center;">
                            <div style="font-size:24px;font-weight:600;color:#dc2626;font-family:monospace;">{traffic}</div>
                            <div style="font-size:11px;color:#991b1b;margin-top:3px;">traffic drops</div>
                          </div>
                        </td>
                        <td width="25%" style="padding-right:8px;">
                          <div style="background:#fffbeb;border-radius:8px;padding:14px;text-align:center;">
                            <div style="font-size:24px;font-weight:600;color:#d97706;font-family:monospace;">{llm}</div>
                            <div style="font-size:11px;color:#92400e;margin-top:3px;">LLM drops</div>
                          </div>
                        </td>
                        <td width="25%" style="padding-right:8px;">
                          <div style="background:#eff6ff;border-radius:8px;padding:14px;text-align:center;">
                            <div style="font-size:24px;font-weight:600;color:#2563eb;font-family:monospace;">{stale}</div>
                            <div style="font-size:11px;color:#1e40af;margin-top:3px;">stale posts</div>
                          </div>
                        </td>
                        <td width="25%">
                          <div style="background:#f0fdf4;border-radius:8px;padding:14px;text-align:center;">
                            <div style="font-size:24px;font-weight:600;color:#16a34a;font-family:monospace;">{stable}</div>
                            <div style="font-size:11px;color:#166534;margin-top:3px;">stable</div>
                          </div>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>

                <!-- Executive summary -->
                <tr>
                  <td style="padding:0 0 20px 0;">
                    <div style="background:#f8fafc;border-left:3px solid #2563eb;border-radius:0 8px 8px 0;padding:16px 18px;">
                      <div style="font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:10px;">Weekly summary</div>
                      <div style="font-size:13px;color:#334155;line-height:1.7;">{summary_html}</div>
                    </div>
                  </td>
                </tr>

                <!-- Flagged posts -->
                {"<tr><td style='padding:0 0 10px 0;'><div style='font-size:12px;font-weight:600;color:#475569;text-transform:uppercase;letter-spacing:0.05em;'>Posts needing attention</div></td></tr>" if flagged else ""}
                {post_cards}

              </table>
              {more_text}

              <!-- CTA button -->
              <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
                <tr>
                  <td align="center">
                    <a href="{REPORT_URL}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:12px 28px;border-radius:8px;font-size:13px;font-weight:500;text-decoration:none;">View full interactive report</a>
                    <div style="font-size:11px;color:#94a3b8;margin-top:8px;">{REPORT_URL}</div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background:#f1f5f9;border-radius:0 0 10px 10px;border:1px solid #e2e8f0;border-top:none;padding:16px 28px;text-align:center;">
              <div style="font-size:11px;color:#94a3b8;line-height:1.6;">
                Red Points Blog Monitor &nbsp;·&nbsp; Monitoring {len(KEEP_POSTS)} KEEP posts<br>
                Full interactive report attached as blog-monitor-{week_end}.html
              </div>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def send_email_report(html_content: str, week_end: str, flagged: list[dict], summary: str = "", season: str | None = None):
    """Sends email with inline HTML body + full interactive report as attachment."""
    if not GMAIL_APP_PASSWORD:
        log.warning("GMAIL_APP_PASSWORD not set — skipping email")
        return

    recipients = [r.strip() for r in GMAIL_RECIPIENTS.split(",") if r.strip()]
    subject = f"Red Points Blog Monitor — Week ending {week_end}"

    msg = MIMEMultipart("mixed")
    msg["From"]    = GMAIL_SENDER
    msg["To"]      = ", ".join(recipients)
    msg["Subject"] = subject

    # Inline HTML body — renders directly in Gmail/Outlook
    email_body = build_email_body(flagged, week_end, summary, season)
    msg.attach(MIMEText(email_body, "html", "utf-8"))

    # Attach HTML report
    attachment = MIMEBase("text", "html")
    attachment.set_payload(html_content.encode("utf-8"))
    encoders.encode_base64(attachment)
    attachment.add_header(
        "Content-Disposition",
        f"attachment; filename=blog-monitor-{week_end}.html"
    )
    msg.attach(attachment)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_SENDER, recipients, msg.as_string())
        log.info(f"Email sent to {recipients}")
    except Exception as e:
        log.error(f"Email failed: {e}")


# ---------------------------------------------------------------------------
# HTML REPORT GENERATOR
# ---------------------------------------------------------------------------

def generate_html_report(
    flagged: list[dict],
    history: dict,
    week_start: str,
    week_end: str,
    summary: str,
    season: str | None,
) -> str:
    """Generates a self-contained filterable HTML report with last 4 weeks of data."""

    # Build last 4 weeks of summary data for the week switcher
    recent_weeks = sorted(history.keys(), reverse=True)[:4]
    weeks_data = {}
    for wk in recent_weeks:
        wdata = history[wk]
        weeks_data[wk] = {
            "week_end": wdata.get("week_end", ""),
            "flagged_count": len(wdata.get("flagged", [])),
            "traffic_count": sum(1 for p in wdata.get("flagged", []) for f in p.get("flags", []) if f["type"] == "traffic"),
            "llm_count": sum(1 for p in wdata.get("flagged", []) for f in p.get("flags", []) if f["type"] == "llm"),
            "stale_count": sum(1 for p in wdata.get("flagged", []) for f in p.get("flags", []) if f["type"] == "stale"),
            "summary": wdata.get("summary", ""),
            "flagged": wdata.get("flagged", []),
        }

    # Add current week
    weeks_data[week_start] = {
        "week_end": week_end,
        "flagged_count": len(flagged),
        "traffic_count": sum(1 for p in flagged for f in p["flags"] if f["type"] == "traffic"),
        "llm_count": sum(1 for p in flagged for f in p["flags"] if f["type"] == "llm"),
        "stale_count": sum(1 for p in flagged for f in p["flags"] if f["type"] == "stale"),
        "summary": summary,
        "flagged": flagged,
        "season": season,
    }

    weeks_json = json.dumps(weeks_data)
    clusters = sorted(set(p["cluster"] for p in flagged)) if flagged else []
    clusters_json = json.dumps(clusters)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Red Points Blog Monitor</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'DM Sans', sans-serif; background: #f8fafc; color: #1e293b; }}
  .header {{ background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 100%); padding: 32px; color: white; }}
  .header h1 {{ font-size: 22px; font-weight: 700; }}
  .header p {{ color: #94a3b8; margin-top: 4px; font-size: 14px; }}
  .container {{ max-width: 1100px; margin: 0 auto; padding: 24px; }}
  .week-bar {{ display: flex; gap: 12px; overflow-x: auto; margin-bottom: 24px; padding-bottom: 4px; }}
  .week-btn {{ background: white; border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px 16px; cursor: pointer; white-space: nowrap; font-family: 'DM Sans', sans-serif; font-size: 13px; font-weight: 500; color: #64748b; transition: all 0.15s; }}
  .week-btn:hover {{ border-color: #2563eb; color: #2563eb; }}
  .week-btn.active {{ background: #2563eb; border-color: #2563eb; color: white; }}
  .summary-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }}
  .summary-card {{ background: white; border-radius: 12px; padding: 20px; border: 1px solid #e2e8f0; text-align: center; }}
  .summary-card .num {{ font-family: 'JetBrains Mono', monospace; font-size: 32px; font-weight: 700; }}
  .summary-card .lbl {{ font-size: 12px; color: #64748b; margin-top: 4px; font-weight: 500; }}
  .summary-card.red .num {{ color: #dc2626; }}
  .summary-card.amber .num {{ color: #d97706; }}
  .summary-card.blue .num {{ color: #2563eb; }}
  .summary-card.green .num {{ color: #16a34a; }}
  .exec-summary {{ background: white; border-radius: 12px; padding: 20px 24px; margin-bottom: 24px; border: 1px solid #e2e8f0; border-left: 4px solid #2563eb; }}
  .exec-summary h3 {{ font-size: 13px; font-weight: 600; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; }}
  .exec-summary p {{ font-size: 14px; line-height: 1.7; color: #334155; white-space: pre-line; }}
  .filters {{ background: white; border-radius: 12px; padding: 16px 20px; margin-bottom: 20px; border: 1px solid #e2e8f0; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }}
  .filters label {{ font-size: 13px; font-weight: 600; color: #475569; margin-right: 4px; }}
  .filter-btn {{ background: #f1f5f9; border: 1px solid #e2e8f0; border-radius: 6px; padding: 6px 14px; cursor: pointer; font-size: 12px; font-weight: 500; color: #64748b; font-family: 'DM Sans', sans-serif; transition: all 0.15s; }}
  .filter-btn:hover {{ border-color: #2563eb; color: #2563eb; }}
  .filter-btn.active {{ background: #2563eb; border-color: #2563eb; color: white; }}
  .posts-list {{ display: flex; flex-direction: column; gap: 12px; }}
  .post-card {{ background: white; border-radius: 12px; padding: 18px 20px; border: 1px solid #e2e8f0; border-left: 4px solid #dc2626; }}
  .post-card[data-has-llm="true"] {{ border-left-color: #d97706; }}
  .post-card[data-has-stale="true"][data-has-traffic="false"][data-has-llm="false"] {{ border-left-color: #2563eb; }}
  .post-header {{ display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px; gap: 12px; flex-wrap: wrap; }}
  .post-badges {{ display: flex; gap: 6px; flex-wrap: wrap; }}
  .badge {{ font-family: 'JetBrains Mono', monospace; font-size: 11px; padding: 3px 8px; border-radius: 4px; color: white; font-weight: 500; }}
  .badge.score {{ background: #7c3aed; }}
  .badge.cluster {{ background: #0f172a; }}
  .post-title {{ font-size: 14px; font-weight: 600; color: #0f172a; text-decoration: none; display: block; margin-bottom: 10px; }}
  .post-title:hover {{ color: #2563eb; text-decoration: underline; }}
  .flag-list {{ list-style: none; display: flex; flex-direction: column; gap: 4px; }}
  .flag-item {{ font-size: 13px; color: #475569; padding: 6px 10px; background: #f8fafc; border-radius: 6px; }}
  .empty-state {{ background: white; border-radius: 12px; padding: 48px; text-align: center; border: 1px solid #e2e8f0; color: #94a3b8; font-size: 15px; }}
  .season-banner {{ background: #fef9c3; border: 1px solid #fde047; border-radius: 12px; padding: 16px 20px; margin-bottom: 20px; font-size: 14px; color: #713f12; }}
  .report-link {{ font-size: 12px; color: #94a3b8; margin-top: 4px; }}
  @media (max-width: 640px) {{
    .summary-grid {{ grid-template-columns: repeat(2, 1fr); }}
    .filters {{ flex-direction: column; align-items: flex-start; }}
  }}
  @media print {{
    body {{ background: white; }}
    .week-bar, .filters {{ display: none; }}
  }}
</style>
</head>
<body>

<div class="header">
  <div style="max-width:1100px;margin:0 auto">
    <h1>🔍 Red Points Blog Monitor</h1>
    <p>KEEP posts only · Updated weekly every Monday · <span style="font-family:'JetBrains Mono',monospace;font-size:12px">Generated {datetime.date.today().strftime('%B %d, %Y')}</span></p>
    <p class="report-link">Live report: <a href="{REPORT_URL}" style="color:#60a5fa">{REPORT_URL}</a></p>
  </div>
</div>

<div class="container">

  <!-- Week switcher -->
  <div class="week-bar" id="weekBar"></div>

  <!-- Summary cards -->
  <div class="summary-grid" id="summaryCards"></div>

  <!-- Executive summary -->
  <div class="exec-summary">
    <h3>📝 Weekly Executive Summary</h3>
    <p id="execSummary"></p>
  </div>

  <!-- Season banner -->
  <div class="season-banner" id="seasonBanner" style="display:none"></div>

  <!-- Filters -->
  <div class="filters" id="filtersBar">
    <div>
      <label>Flag type:</label>
      <button class="filter-btn active" onclick="setFilter('type','all',this)">All</button>
      <button class="filter-btn" onclick="setFilter('type','traffic',this)">🔴 Traffic</button>
      <button class="filter-btn" onclick="setFilter('type','llm',this)">🟡 LLM</button>
      <button class="filter-btn" onclick="setFilter('type','stale',this)">📅 Stale</button>
    </div>
    <div id="clusterFilters">
      <label>Cluster:</label>
      <button class="filter-btn active" onclick="setFilter('cluster','all',this)">All</button>
    </div>
  </div>

  <!-- Posts list -->
  <div class="posts-list" id="postsList"></div>

</div>

<script>
const WEEKS_DATA = {weeks_json};
const ALL_CLUSTERS = {clusters_json};
const CURRENT_WEEK = "{week_start}";

let activeWeek = CURRENT_WEEK;
let activeTypeFilter = 'all';
let activeClusterFilter = 'all';

function init() {{
  renderWeekBar();
  renderClusterFilters();
  renderWeek(activeWeek);
}}

function renderWeekBar() {{
  const bar = document.getElementById('weekBar');
  const weeks = Object.keys(WEEKS_DATA).sort().reverse();
  bar.innerHTML = weeks.map(wk => {{
    const d = WEEKS_DATA[wk];
    const label = `Week of ${{wk}}`;
    const flagTxt = d.flagged_count > 0 ? ` · ${{d.flagged_count}} flags` : ' · ✅ stable';
    return `<button class="week-btn ${{wk === activeWeek ? 'active' : ''}}" onclick="switchWeek('${{wk}}', this)">${{label}}${{flagTxt}}</button>`;
  }}).join('');
}}

function renderClusterFilters() {{
  const container = document.getElementById('clusterFilters');
  const allClusters = [...new Set(
    Object.values(WEEKS_DATA).flatMap(w => (w.flagged || []).map(p => p.cluster))
  )].sort();
  const btns = allClusters.map(c =>
    `<button class="filter-btn" onclick="setFilter('cluster','${{c}}',this)">${{c}}</button>`
  ).join('');
  container.innerHTML = `<label>Cluster:</label><button class="filter-btn active" onclick="setFilter('cluster','all',this)">All</button>${{btns}}`;
}}

function switchWeek(wk, btn) {{
  activeWeek = wk;
  document.querySelectorAll('.week-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderWeek(wk);
}}

function setFilter(type, value, btn) {{
  if (type === 'type') {{
    activeTypeFilter = value;
    document.querySelectorAll('.filters .filter-btn').forEach(b => {{
      if (b.closest('#filtersBar > div:first-child')) b.classList.remove('active');
    }});
  }} else {{
    activeClusterFilter = value;
    document.querySelectorAll('#clusterFilters .filter-btn').forEach(b => b.classList.remove('active'));
  }}
  btn.classList.add('active');
  renderPosts(WEEKS_DATA[activeWeek]?.flagged || []);
}}

function renderWeek(wk) {{
  const data = WEEKS_DATA[wk];
  if (!data) return;

  // Summary cards
  document.getElementById('summaryCards').innerHTML = `
    <div class="summary-card red"><div class="num">${{data.traffic_count}}</div><div class="lbl">🔴 Traffic Drops</div></div>
    <div class="summary-card amber"><div class="num">${{data.llm_count}}</div><div class="lbl">🟡 LLM Drops</div></div>
    <div class="summary-card blue"><div class="num">${{data.stale_count}}</div><div class="lbl">📅 Stale Posts</div></div>
    <div class="summary-card green"><div class="num">${{{len(KEEP_POSTS)} - data.flagged_count}}</div><div class="lbl">✅ Stable Posts</div></div>
  `;

  // Executive summary
  document.getElementById('execSummary').textContent = data.summary || 'No summary available.';

  // Season banner
  const banner = document.getElementById('seasonBanner');
  if (data.season) {{
    banner.textContent = `⏸️ Alert suppression active — ${{data.season}} period. Baseline calculation resumes next week.`;
    banner.style.display = 'block';
  }} else {{
    banner.style.display = 'none';
  }}

  renderPosts(data.flagged || []);
}}

function renderPosts(posts) {{
  const container = document.getElementById('postsList');

  const filtered = posts.filter(p => {{
    const typeMatch = activeTypeFilter === 'all' ||
      p.flags.some(f => f.type === activeTypeFilter);
    const clusterMatch = activeClusterFilter === 'all' ||
      p.cluster === activeClusterFilter;
    return typeMatch && clusterMatch;
  }});

  if (filtered.length === 0) {{
    container.innerHTML = `<div class="empty-state">✅ No posts match the current filters this week.</div>`;
    return;
  }}

  container.innerHTML = filtered.map(p => {{
    const hasTraffic = p.flags.some(f => f.type === 'traffic');
    const hasLlm = p.flags.some(f => f.type === 'llm');
    const hasStale = p.flags.some(f => f.type === 'stale');
    const flagItems = p.flags.map(f =>
      `<li class="flag-item">${{f.label}}: ${{f.detail}}</li>`
    ).join('');
    return `
      <div class="post-card"
           data-has-traffic="${{hasTraffic}}"
           data-has-llm="${{hasLlm}}"
           data-has-stale="${{hasStale}}">
        <div class="post-header">
          <div class="post-badges">
            <span class="badge score">Score ${{p.score}}/14</span>
            <span class="badge cluster">${{p.cluster}}</span>
            ${{hasTraffic ? '<span class="badge" style="background:#dc2626">🔴 Traffic</span>' : ''}}
            ${{hasLlm ? '<span class="badge" style="background:#d97706">🟡 LLM</span>' : ''}}
            ${{hasStale ? '<span class="badge" style="background:#2563eb">📅 Stale</span>' : ''}}
          </div>
        </div>
        <a href="${{p.url}}" class="post-title" target="_blank">${{p.title}}</a>
        <ul class="flag-list">${{flagItems}}</ul>
      </div>`;
  }}).join('');
}}

init();
</script>
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    log.info("=== Red Points Blog Monitor starting ===")

    current_monday, current_sunday = last_complete_week()
    prev_monday, prev_sunday = date_range_for_week_n(current_monday, 1)

    week_start = week_key(current_monday)
    week_end   = current_sunday.strftime("%Y-%m-%d")

    log.info(f"Analysing week: {week_start} → {week_end}")
    log.info(f"Previous week:  {week_key(prev_monday)} → {prev_sunday.strftime('%Y-%m-%d')}")

    season = is_seasonal(current_monday)
    if season:
        log.info(f"Seasonal period detected: {season}")

    # ── Step 1: WordPress API — fetch live last_updated dates (6am UTC, low traffic) ──
    wp_dates = fetch_wordpress_last_updated()

    # Load historical data
    history = load_historical_data()
    baseline_weeks_available = len(history)
    log.info(f"Historical baseline: {baseline_weeks_available} weeks available")

    # Fetch GSC data
    gsc_service = build_gsc_service()
    gsc_current = fetch_gsc_clicks(
        gsc_service, week_start, week_end
    )

    # Fetch Omnia citations (current week and previous week)
    omnia_current  = fetch_omnia_citations(week_start, week_end)
    omnia_previous = fetch_omnia_citations(
        week_key(prev_monday), prev_sunday.strftime("%Y-%m-%d")
    )

    # Run flags — pass WordPress dates so stale check uses live modified dates
    flagged = run_flags(
        current_monday=current_monday,
        gsc_current=gsc_current,
        omnia_current=omnia_current,
        omnia_previous=omnia_previous,
        history=history,
        baseline_weeks_available=baseline_weeks_available,
        wp_dates=wp_dates,
    )

    # Generate executive summary
    summary = generate_executive_summary(flagged, week_end, season)
    log.info(f"Summary:\n{summary}")

    # Save this week's data
    week_data = {
        "week_start": week_start,
        "week_end": week_end,
        "season": season,
        "flagged": flagged,
        "summary": summary,
        "gsc_data": gsc_current,
        "omnia_current": omnia_current,
        "generated_at": datetime.datetime.utcnow().isoformat(),
    }
    save_week_data(week_start, week_data)

    # Reload history including this week
    history = load_historical_data()

    # Generate HTML report
    html_report = generate_html_report(
        flagged=flagged,
        history=history,
        week_start=week_start,
        week_end=week_end,
        summary=summary,
        season=season,
    )

    # Save index.html for GitHub Pages
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_report)
    log.info("index.html written for GitHub Pages")

    # Create Asana tasks
    create_asana_tasks(flagged, week_end)

    # Send Slack alert
    send_slack_alert(summary, flagged, week_end, season)

    # Send email report
    send_email_report(html_report, week_end, flagged, summary=summary, season=season)

    log.info("=== Done ===")


if __name__ == "__main__":
    main()
