"""
Red Points Blog Monitor
=======================
Weekly script that:
  1. Fetches last_updated dates from WordPress REST API for all KEEP posts
  2. Pulls Google Search Console data for KEEP posts (clicks + query breakdown)
  3. Pulls Omnia citation data per blog post URL
  4. Applies 3 flags: traffic drop, LLM drop, stale content
  5. For each traffic drop flag: runs full diagnosis via GSC + Semrush
     - Position change, impressions pattern → root cause verdict
     - Top queries driving the drop (GSC query-level data)
     - Competitor movement on primary keyword (Semrush)
     - Fetches our post + competitor post content
     - Sends both to Claude API → auto-generates content brief
  6. Generates a filterable HTML report and saves weekly JSON data
  7. Sends 3-bullet executive summary + diagnosis to Slack (#blog-monitor)
  8. Emails the HTML report with content briefs to the distribution list

Flags:
  🔴 Traffic drop  — weekly clicks ≥30% below 12-week average AND absolute drop ≥50 clicks
  🟡 LLM drop      — citations drop ≥15 AND ≥40% week over week (Omnia)
  📅 Stale content — not updated in 6+ months AND post score ≥10/14

Timing rules:
  - Always analyse last complete Mon–Sun week (never current partial week)
  - Respect GSC 3-day reporting lag
  - Suppress traffic alerts for first 4 weeks (baseline not yet reliable)
  - 6-week cooldown for score 12–14 posts after update (high-value, needs clean signal)
  - 4-week cooldown for score 8–11 posts after update
  - 4-week cooldown for newly merged posts after 301 redirect
  - Exclude seasonal periods from baseline AND suppress alerts during them
  - WordPress API called at start of run (scheduled 6am UTC = low traffic window)
  - Semrush: ~20 units per flagged post, well within Business plan 3,000/day limit

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
from monitor_dashboard_addition import write_dashboard_data

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
REPORT_URL               = os.getenv("REPORT_URL", "https://antonellafilpo.github.io/redpoints-blog-monitor")

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

# Semrush API — competitor analysis for traffic drop flags
SEMRUSH_API_KEY          = os.getenv("SEMRUSH_API_KEY")
SEMRUSH_DATABASE         = os.getenv("SEMRUSH_DATABASE", "us")
SEMRUSH_API_BASE         = "https://api.semrush.com/"

# Alert thresholds
TRAFFIC_DROP_PCT         = float(os.getenv("TRAFFIC_DROP_PCT", "0.30"))    # 30%
TRAFFIC_DROP_ABS         = float(os.getenv("TRAFFIC_DROP_ABS", "50"))      # 50 clicks
LLM_DROP_ABS             = int(os.getenv("LLM_DROP_ABS", "15"))            # 15 citations
LLM_DROP_PCT             = float(os.getenv("LLM_DROP_PCT", "0.30"))        # 30%
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
# PHASE 2 — TRAFFIC DROP DIAGNOSIS + CONTENT BRIEF
# ---------------------------------------------------------------------------

def fetch_gsc_query_breakdown(service, url: str, start_date: str, end_date: str,
                               prev_start: str, prev_end: str) -> dict:
    """
    Pulls query-level click data for a specific URL.
    Returns current week vs 4 weeks ago for top 5 queries.
    """
    def query_gsc(s_date, e_date):
        body = {
            "startDate": s_date, "endDate": e_date,
            "dimensions": ["query"],
            "rowLimit": 10,
            "dimensionFilterGroups": [{"filters": [{
                "dimension": "page", "operator": "equals", "expression": url,
            }]}],
        }
        try:
            resp = service.searchanalytics().query(siteUrl=GSC_SITE_URL, body=body).execute()
            return {r["keys"][0]: {"clicks": r["clicks"], "position": r["position"]}
                    for r in resp.get("rows", [])}
        except Exception as e:
            log.warning(f"  GSC query breakdown failed for {url}: {e}")
            return {}

    current = query_gsc(start_date, end_date)
    previous = query_gsc(prev_start, prev_end)

    breakdown = []
    all_queries = set(list(current.keys())[:5]) | set(list(previous.keys())[:5])
    for q in sorted(all_queries,
                    key=lambda x: previous.get(x, {}).get("clicks", 0)
                    - current.get(x, {}).get("clicks", 0),
                    reverse=True)[:5]:
        curr_clicks = current.get(q, {}).get("clicks", 0)
        prev_clicks = previous.get(q, {}).get("clicks", 0)
        breakdown.append({
            "query": q,
            "prev_clicks": round(prev_clicks),
            "curr_clicks": round(curr_clicks),
            "change": round(curr_clicks - prev_clicks),
        })
    return {"queries": breakdown, "primary_keyword": breakdown[0]["query"] if breakdown else ""}


def fetch_semrush_competitors(keyword: str) -> list[dict]:
    """
    Queries Semrush for top 5 organic results on a given keyword.
    Returns list of {url, position, domain} sorted by position.
    Uses ~10 Semrush units.
    """
    if not SEMRUSH_API_KEY:
        log.warning("  SEMRUSH_API_KEY not set — skipping competitor analysis")
        return []
    try:
        params = {
            "type": "phrase_organic",
            "key": SEMRUSH_API_KEY,
            "phrase": keyword,
            "database": SEMRUSH_DATABASE,
            "display_limit": 5,
            "export_columns": "Dn,Ur,Po",
        }
        resp = requests.get(SEMRUSH_API_BASE, params=params, timeout=15)
        resp.raise_for_status()
        lines = resp.text.strip().split("\n")
        if len(lines) < 2:
            return []
        competitors = []
        for line in lines[1:]:
            parts = line.split(";")
            if len(parts) >= 3:
                competitors.append({
                    "domain": parts[0].strip(),
                    "url": parts[1].strip(),
                    "position": int(parts[2].strip()) if parts[2].strip().isdigit() else 99,
                })
        log.info(f"  Semrush: {len(competitors)} competitors for '{keyword}'")
        return competitors
    except Exception as e:
        log.error(f"  Semrush failed for '{keyword}': {e}")
        return []


def fetch_semrush_position_history(url: str, keyword: str) -> dict:
    """
    Gets current vs previous position for our URL on a keyword.
    Uses ~10 Semrush units.
    """
    if not SEMRUSH_API_KEY:
        return {}
    try:
        params = {
            "type": "url_organic",
            "key": SEMRUSH_API_KEY,
            "url": url,
            "database": SEMRUSH_DATABASE,
            "display_limit": 10,
            "export_columns": "Ph,Po,Pp",
        }
        resp = requests.get(SEMRUSH_API_BASE, params=params, timeout=15)
        resp.raise_for_status()
        lines = resp.text.strip().split("\n")
        for line in lines[1:]:
            parts = line.split(";")
            if len(parts) >= 3 and parts[0].strip().lower() == keyword.lower():
                return {
                    "keyword": keyword,
                    "position_now": int(parts[1].strip()) if parts[1].strip().isdigit() else None,
                    "position_prev": int(parts[2].strip()) if parts[2].strip().isdigit() else None,
                }
        return {}
    except Exception as e:
        log.warning(f"  Semrush position history failed: {e}")
        return {}


def fetch_post_content(url: str, timeout: int = 15) -> str:
    """
    Fetches the text content of a webpage for content brief generation.
    Returns plain text, truncated to ~4,000 chars for Claude context efficiency.
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; RedPointsBlogMonitor/1.0)"}
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        # Basic HTML → text stripping (no beautifulsoup dependency)
        import re
        text = resp.text
        # Remove scripts, styles, nav, footer
        text = re.sub(r'<(script|style|nav|footer|header)[^>]*>.*?</\1>', ' ', text, flags=re.S)
        # Remove all HTML tags
        text = re.sub(r'<[^>]+>', ' ', text)
        # Collapse whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:4000]
    except Exception as e:
        log.warning(f"  Could not fetch content from {url}: {e}")
        return ""


def diagnose_traffic_drop(
    path: str,
    full_url: str,
    gsc_current_clicks: float,
    gsc_avg_clicks: float,
    gsc_service,
    week_start: str,
    week_end: str,
    prev_week_start: str,
    prev_week_end: str,
) -> dict:
    """
    Runs full diagnosis for a traffic drop flag:
    1. GSC query breakdown (top queries, which ones dropped)
    2. Impression check (ranking loss vs CTR problem vs indexing issue)
    3. Semrush competitor movement
    4. Root cause verdict
    5. Recommended action
    Returns a diagnosis dict to be attached to the flag.
    """
    log.info(f"  Running diagnosis for {path}")
    diagnosis = {
        "query_breakdown": [],
        "primary_keyword": "",
        "impressions_signal": "",
        "competitors": [],
        "our_position_now": None,
        "our_position_prev": None,
        "root_cause": "",
        "verdict": "",
        "recommended_action": "",
    }

    # ── Step 1: GSC query breakdown ──────────────────────────────────────────
    qb = fetch_gsc_query_breakdown(
        gsc_service, full_url,
        week_start, week_end,
        prev_week_start, prev_week_end,
    )
    diagnosis["query_breakdown"] = qb.get("queries", [])
    diagnosis["primary_keyword"] = qb.get("primary_keyword", "")

    # ── Step 2: Impressions check ────────────────────────────────────────────
    try:
        imp_body = {
            "startDate": week_start, "endDate": week_end,
            "dimensions": ["page"],
            "dimensionFilterGroups": [{"filters": [{
                "dimension": "page", "operator": "equals", "expression": full_url,
            }]}],
        }
        imp_resp = gsc_service.searchanalytics().query(
            siteUrl=GSC_SITE_URL, body=imp_body
        ).execute()
        curr_imp = imp_resp.get("rows", [{}])[0].get("impressions", 0) if imp_resp.get("rows") else 0

        prev_imp_body = {**imp_body, "startDate": prev_week_start, "endDate": prev_week_end}
        prev_imp_resp = gsc_service.searchanalytics().query(
            siteUrl=GSC_SITE_URL, body=prev_imp_body
        ).execute()
        prev_imp = prev_imp_resp.get("rows", [{}])[0].get("impressions", 0) if prev_imp_resp.get("rows") else 0

        curr_pos = imp_resp.get("rows", [{}])[0].get("position", 0) if imp_resp.get("rows") else 0
        prev_pos = prev_imp_resp.get("rows", [{}])[0].get("position", 0) if prev_imp_resp.get("rows") else 0

        diagnosis["our_position_now"] = round(curr_pos, 1)
        diagnosis["our_position_prev"] = round(prev_pos, 1)

        if prev_imp > 0:
            imp_change_pct = (curr_imp - prev_imp) / prev_imp
            if imp_change_pct < -0.30:
                diagnosis["impressions_signal"] = "collapse"  # indexing or major ranking loss
            elif imp_change_pct < -0.10:
                diagnosis["impressions_signal"] = "drop"      # ranking loss
            elif imp_change_pct > -0.05:
                diagnosis["impressions_signal"] = "stable"    # CTR problem or SERP feature
            else:
                diagnosis["impressions_signal"] = "slight_drop"
    except Exception as e:
        log.warning(f"  Impressions check failed: {e}")

    # ── Step 3: Semrush competitor movement ──────────────────────────────────
    if diagnosis["primary_keyword"]:
        competitors = fetch_semrush_competitors(diagnosis["primary_keyword"])
        diagnosis["competitors"] = competitors

    # ── Step 4: Root cause verdict + recommended action ──────────────────────
    pos_now = diagnosis["our_position_now"]
    pos_prev = diagnosis["our_position_prev"]
    imp_signal = diagnosis["impressions_signal"]
    competitors = diagnosis["competitors"]

    # Find if we are in the top 5 and who overtook us
    our_domain = "redpoints.com"
    our_serp_pos = next((c["position"] for c in competitors if our_domain in c["url"]), None)
    overtakers = [c for c in competitors if our_domain not in c["url"]
                  and c["position"] < (our_serp_pos or 99)]

    if imp_signal == "collapse":
        diagnosis["root_cause"] = "indexing_issue"
        diagnosis["verdict"] = (
            "Impressions collapsed — possible indexing or crawl issue. "
            "This pattern is abnormal for a ranking shift and may indicate "
            "a noindex tag, robots.txt block, or redirect chain introduced during recent site changes."
        )
        diagnosis["recommended_action"] = (
            "Check GSC Coverage report for this URL immediately. Verify the page is not blocked "
            "by robots.txt or a noindex tag. Submit for reindex if needed."
        )
    elif imp_signal in ("drop", "slight_drop") and pos_now and pos_prev and pos_now > pos_prev + 1.5:
        if overtakers:
            top_overtaker = overtakers[0]
            diagnosis["root_cause"] = "competitor_displacement"
            diagnosis["verdict"] = (
                f"Ranking loss — {top_overtaker['domain']} has moved above us on "
                f"'{diagnosis['primary_keyword']}'. Position dropped from "
                f"{pos_prev} → {pos_now}. Impressions fell alongside clicks, "
                "confirming this is a ranking problem, not a CTR problem."
            )
            diagnosis["recommended_action"] = (
                f"Review {top_overtaker['url']} — compare their structure, word count, "
                "and sub-topics vs. ours. Add any missing sections, update screenshots, "
                f"and target reclaiming position {int(pos_prev)} within 6 weeks."
            )
        else:
            diagnosis["root_cause"] = "algorithm_quality_signal"
            diagnosis["verdict"] = (
                f"Ranking loss — position dropped from {pos_prev} → {pos_now} "
                f"on '{diagnosis['primary_keyword']}'. No single competitor moved "
                "significantly — likely a content quality or freshness signal from Google."
            )
            diagnosis["recommended_action"] = (
                "Refresh the content — update statistics, screenshots, and examples. "
                "Strengthen the introduction and ensure the post directly answers the "
                "primary search intent in the first 150 words."
            )
    elif imp_signal == "stable":
        diagnosis["root_cause"] = "ctr_problem"
        diagnosis["verdict"] = (
            "CTR problem — impressions are stable but clicks fell. "
            "This means we still rank in the same position but fewer people are clicking. "
            "Likely cause: a SERP feature (AI Overview, featured snippet, or knowledge panel) "
            "now appears above organic results and is absorbing clicks."
        )
        diagnosis["recommended_action"] = (
            "Search the primary keyword in Google and check what SERP features appear. "
            "If an AI Overview is present, reformat the post intro as a direct, concise answer "
            "to the query. Update the title tag and meta description to be more click-worthy."
        )
    else:
        diagnosis["root_cause"] = "unknown"
        diagnosis["verdict"] = (
            "Traffic dropped but the pattern is unclear — impressions and position data "
            "are insufficient to confirm a single root cause this week. "
            "Monitor for a second consecutive week before taking action."
        )
        diagnosis["recommended_action"] = (
            "Watch this post next Monday. If it drops again, run a manual GSC check "
            "and compare against competitor rankings on the primary keyword."
        )

    return diagnosis


def generate_content_brief(
    post_title: str,
    post_url: str,
    primary_keyword: str,
    our_content: str,
    competitor_url: str,
    competitor_content: str,
    diagnosis: dict,
) -> str:
    """
    Calls Claude API to generate a structured content brief comparing
    our post vs the top-ranking competitor. Returns HTML-formatted brief.
    """
    if not ANTHROPIC_API_KEY:
        return "<p>Content brief unavailable — ANTHROPIC_API_KEY not set.</p>"
    if not our_content or not competitor_content:
        return "<p>Content brief unavailable — could not fetch post content.</p>"

    prompt = f"""You are a senior SEO and LLM content strategist at Red Points, the AI Brand Protection Company. Red Points offers a fully managed brand protection service with IP-Ops specialists handling enforcement on behalf of clients — this is the core differentiator vs. competitors who offer self-serve software.

TARGET AUDIENCE: B2B brand protection teams at enterprise and mid-market companies. Buyers are evaluating vendors or looking for how-to guidance on brand protection problems.

A blog post has lost organic traffic. Compare our post against the top-ranking competitor and produce a structured brief telling the writer exactly what to change. Apply BOTH SEO and LLM optimization principles.

POST DETAILS:
- Title: {post_title}
- URL: {post_url}
- Primary keyword: {primary_keyword}
- Root cause: {diagnosis.get('root_cause', 'unknown')}
- Diagnosis: {diagnosis.get('verdict', '')}

OUR POST (first 3500 chars):
{our_content[:3500]}

TOP COMPETITOR ({competitor_url}):
{competitor_content[:3500]}

BRAND RULES — the brief must respect these:
1. Red Points = "AI Brand Protection Company with fully managed service" — never just "software" or "tool"
2. CTAs and product pitches belong AFTER the post has fully solved the user's problem — never in the intro or mid-content
3. No first-person ("we", "our") in sections designed for LLM extraction — use "Red Points" as the subject
4. Differentiators to use where relevant: unlimited enforcement, flat-fee pricing, IP-Ops specialists, 5,000+ marketplaces, 2.7B monthly data points
5. Do not suggest removing the Red Points CTA — only reposition it

LLM OPTIMIZATION RULES — apply these to every structural suggestion:
1. First 50 words must be a factual, entity-grounding definition that can be cited standalone without the rest of the article
2. FAQ answers must be 4+ self-contained sentences — no "see above" or "read more" answers — minimum 8 questions total
3. Comparison tables must use text labels not symbols or icons
4. Each section should open with a direct answer to its heading before adding context or evidence
5. TL;DR or summary block should be present and independently citable
6. Step-by-step sections should use HowTo schema-compatible structure (numbered, each step self-contained)

SEO RULES:
1. Search intent must be matched in the first paragraph — diagnose whether our post answers what users actually want vs what we offer, and flag the gap explicitly
2. Primary keyword should appear naturally in H1, first 100 words, and at least one H2
3. Internal links should point to cluster pillar pages where relevant
4. If the post is a how-to guide, it should cover the manual steps fully before introducing Red Points as a more efficient solution

Produce a content brief in this EXACT format (plain text, no markdown, no preamble):

SEARCH_INTENT_VERDICT: [One sentence — what do users actually want when they search this keyword? Does our post answer it first?]

WORD_COUNT_GAP: [Our estimated word count vs competitor. e.g. "Ours: ~800 words. Competitor: ~2,100 words."]

LLM_EXTRACTABILITY_GAPS:
1. [Specific LLM optimization missing — e.g. "Intro is not a standalone definition — rewrite first 50 words as a factual entity-grounding statement"]
2. [Second LLM gap if present]

TOP_3_CONTENT_GAPS:
1. [Most important missing topic the competitor covers that we don't — be specific about what to add and why]
2. [Second gap]
3. [Third gap]

STRUCTURE_CHANGES:
1. [Specific structural change — e.g. "Move Red Points CTA to after the 5-step manual section"]
2. [Second structural change if needed]

SPECIFIC_EDITS:
1. [Exact edit the writer can implement directly — reference actual content from both posts]
2. [Second edit]
3. [Third edit]
4. [Fourth edit]
5. [Fifth edit]

TARGET_OUTCOME: [Realistic position and traffic recovery in 6 weeks if changes are made]

WRITER_TIME: [Estimated hours to implement these changes]

Every suggestion must reference actual content from both posts. No vague advice like "improve quality" or "add more detail". No suggestions that contradict the brand rules above. Every edit must be something the writer can implement directly."""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=45,
        )
        resp.raise_for_status()
        text = "".join(b["text"] for b in resp.json().get("content", []) if b.get("type") == "text")
        return _format_brief_as_html(text.strip(), competitor_url)
    except Exception as e:
        log.error(f"  Content brief generation failed: {e}")
        return f"<p>Content brief unavailable — API error: {e}</p>"


def _format_brief_as_html(raw_brief: str, competitor_url: str) -> str:
    """Converts the plain-text Claude output into clean HTML for the email."""
    import re
    lines = raw_brief.split("\n")
    sections = {}
    current_key = None
    current_items = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        if ":" in line and line.split(":")[0].isupper() and "_" in line.split(":")[0]:
            if current_key:
                sections[current_key] = current_items
            key = line.split(":")[0].strip()
            val = line[len(key)+1:].strip()
            current_key = key
            current_items = [val] if val else []
        elif re.match(r"^\d+\.", line):
            current_items.append(re.sub(r"^\d+\.\s*", "", line))
        else:
            if current_items:
                current_items[-1] += " " + line
            elif current_key:
                current_items.append(line)

    if current_key:
        sections[current_key] = current_items

    html = f'<div style="background:#f8fafc;border-radius:8px;padding:16px;margin-top:10px;font-size:12px;">'
    html += f'<div style="font-size:11px;color:#64748b;margin-bottom:10px;">Content brief · competitor: <a href="{competitor_url}" style="color:#2563eb;">{competitor_url[:60]}...</a></div>'

    section_labels = {
        "SEARCH_INTENT_VERDICT":    ("Search intent verdict",          "#7c3aed", "#f5f3ff"),
        "WORD_COUNT_GAP":           ("Word count gap",                 "#d97706", "#fffbeb"),
        "LLM_EXTRACTABILITY_GAPS":  ("LLM extractability gaps",        "#0f172a", "#f1f5f9"),
        "TOP_3_CONTENT_GAPS":       ("Top content gaps vs competitor",  "#dc2626", "#fef2f2"),
        "STRUCTURE_CHANGES":        ("Structure changes",              "#2563eb", "#eff6ff"),
        "SPECIFIC_EDITS":           ("Specific edits for writer",      "#059669", "#f0fdf4"),
        "TARGET_OUTCOME":           ("Target outcome (6 weeks)",       "#0f172a", "#f1f5f9"),
        "WRITER_TIME":              ("Estimated writer time",           "#0f172a", "#f1f5f9"),
    }

    for key, (label, color, bg) in section_labels.items():
        if key not in sections:
            continue
        items = [i for i in sections[key] if i]
        if not items:
            continue
        html += f'<div style="margin-bottom:10px;padding:10px;background:{bg};border-radius:6px;border-left:3px solid {color};">'
        html += f'<div style="font-size:10px;font-weight:500;color:{color};margin-bottom:5px;">{label.upper()}</div>'
        if len(items) == 1:
            html += f'<div style="color:#1e293b;line-height:1.6;">{items[0]}</div>'
        else:
            for i, item in enumerate(items, 1):
                html += f'<div style="color:#1e293b;line-height:1.6;margin-bottom:3px;">{i}. {item}</div>'
        html += "</div>"

    html += "</div>"
    return html


def run_traffic_diagnosis(
    flagged_posts: list[dict],
    gsc_service,
    week_start: str,
    week_end: str,
    prev_week_start: str,
    prev_week_end: str,
) -> list[dict]:
    """
    For every traffic-flagged post, runs full diagnosis and generates content brief.
    Runs for ALL traffic drop posts regardless of tier.
    Attaches diagnosis + brief to each flagged post's traffic flag dict.
    """
    for post in flagged_posts:
        traffic_flags = [f for f in post["flags"] if f["type"] == "traffic"]
        if not traffic_flags:
            continue

        log.info(f"Running Phase 2 diagnosis for: {post['title'][:50]}")
        full_url = post["url"]
        path = post["path"]

        try:
            diagnosis = diagnose_traffic_drop(
                path=path,
                full_url=full_url,
                gsc_current_clicks=post.get("gsc_clicks_this_week", 0),
                gsc_avg_clicks=traffic_flags[0].get("baseline", 0),
                gsc_service=gsc_service,
                week_start=week_start,
                week_end=week_end,
                prev_week_start=prev_week_start,
                prev_week_end=prev_week_end,
            )

            # Generate content brief
            content_brief_html = ""
            primary_keyword = diagnosis.get("primary_keyword", "")
            competitors = diagnosis.get("competitors", [])
            top_competitor = next(
                (c for c in competitors if "redpoints.com" not in c.get("url", "")), None
            )

            if primary_keyword and top_competitor:
                log.info(f"  Fetching content for brief: {full_url} vs {top_competitor['url']}")
                our_content = fetch_post_content(full_url)
                competitor_content = fetch_post_content(top_competitor["url"])
                content_brief_html = generate_content_brief(
                    post_title=post["title"],
                    post_url=full_url,
                    primary_keyword=primary_keyword,
                    our_content=our_content,
                    competitor_url=top_competitor["url"],
                    competitor_content=competitor_content,
                    diagnosis=diagnosis,
                )
            else:
                log.warning(f"  Skipping content brief — no keyword or competitor found for {path}")

            # Attach diagnosis and brief to the traffic flag
            for flag in traffic_flags:
                flag["diagnosis"] = diagnosis
                flag["content_brief_html"] = content_brief_html

        except Exception as e:
            log.error(f"  Phase 2 diagnosis failed for {path}: {e}")
            # Continue — email still sends without brief for this post

    return flagged_posts
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
    "/blog/remove-a-counterfeit-from-alibaba/": {
        "title": "How to remove a counterfeit from Alibaba",
        "cluster": "Marketplace Protection", "score": 13,
        "last_updated": "2025-10-02", "update_cooldown_until": None, "merge_cooldown_until": None,
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
    "/blog/how-to-report-scam-on-telegram/": {
        "title": "How to report a scammer on Telegram",
        "cluster": "Platform Scams", "score": 12,
        "last_updated": "2025-11-03", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/copyright-infringement-tiktok/": {
        "title": "How to report a copyright infringement on TikTok",
        "cluster": "Social Media Takedown", "score": 12,
        "last_updated": "2025-07-08", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/dmca-takedown-google/": {
        "title": "How to file a DMCA takedown notice to Google to stop copyright infringement",
        "cluster": "Copyright Infringement", "score": 12,
        "last_updated": "2025-01-22", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/how-to-remove-a-counterfeit-from-aliexpress/": {
        "title": "How to remove a counterfeit from AliExpress",
        "cluster": "Marketplace Protection", "score": 12,
        "last_updated": "2024-12-27", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/website-cloning/": {
        "title": "Website cloning: How to identify, prevent, and respond",
        "cluster": "Website Takedown", "score": 12,
        "last_updated": "2025-02-04", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/how-to-take-down-an-instagram-account/": {
        "title": "A step-by-step guide to banning fake Instagram accounts permanently",
        "cluster": "Social Media Takedown", "score": 12,
        "last_updated": "2025-11-03", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/alibaba-scams/": {
        "title": "9 tips to avoid Alibaba scams",
        "cluster": "Marketplace Protection", "score": 12,
        "last_updated": "2025-03-21", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/how-to-take-down-a-tiktok-account/": {
        "title": "How to take down a TikTok account",
        "cluster": "Social Media Takedown", "score": 12,
        "last_updated": "2025-05-19", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/report-fraud-website/": {
        "title": "How to report and take down a fraud website: a step-by-step guide",
        "cluster": "Website Takedown", "score": 12,
        "last_updated": "2025-11-03", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/best-corsearch-alternatives-and-competitors/": {
        "title": "Best Corsearch alternatives and competitors",
        "cluster": "Brand Protection", "score": 11,
        "last_updated": "2026-02-09", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/godaddy-dmca-takedown/": {
        "title": "How to file a GoDaddy DMCA takedown",
        "cluster": "Copyright Infringement", "score": 11,
        "last_updated": "2025-02-11", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/best-brand-protection-software/": {
        "title": "7 Best Brand Protection Tools for 2026: Ranked & reviewed",
        "cluster": "Brand Protection", "score": 10,
        "last_updated": "2026-02-18", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/best-marqvision-alternatives-and-competitors/": {
        "title": "Best MarqVision alternatives and competitors",
        "cluster": "Brand Protection", "score": 9,
        "last_updated": "2026-02-09", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/best-brandshield-alternatives-and-competitors/": {
        "title": "Best BrandShield alternatives and competitors",
        "cluster": "Brand Protection", "score": 8,
        "last_updated": "2026-02-09", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/best-saas-affiliate-programs/": {
        "title": "Best SaaS affiliate programs",
        "cluster": "Brand Protection", "score": 8,
        "last_updated": "2025-11-06", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/how-to-report-trademark-infringement/": {
        "title": "How to report trademark infringement",
        "cluster": "Copyright Infringement", "score": 7,
        "last_updated": "2026-02-12", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/pirated-software-subscriptions-global-investigation/": {
        "title": "Pirated software subscriptions: global investigation",
        "cluster": "Brand Protection", "score": 7,
        "last_updated": "2026-02-06", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/ebay-brand-protection/": {
        "title": "eBay brand protection: A step-by-step guide to removing counterfeits",
        "cluster": "Marketplace Protection", "score": 6,
        "last_updated": "2026-02-12", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/the-ultimate-guide-to-brand-protection/": {
        "title": "The ultimate guide to brand protection",
        "cluster": "Brand Protection", "score": 6,
        "last_updated": "2026-02-06", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/how-to-protect-your-brand-on-amazon/": {
        "title": "How to protect your brand on Amazon",
        "cluster": "Marketplace Protection", "score": 5,
        "last_updated": "2026-02-12", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/stop-account-sharing-software-piracy/": {
        "title": "How to stop account sharing and software piracy",
        "cluster": "Brand Protection", "score": 5,
        "last_updated": "2026-01-21", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/marketplace-protection/": {
        "title": "Marketplace protection: strategies to stop counterfeits",
        "cluster": "Marketplace Protection", "score": 4,
        "last_updated": "2026-02-11", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/choose-brand-protection-solution/": {
        "title": "How to choose a brand protection solution",
        "cluster": "Brand Protection", "score": 4,
        "last_updated": "2025-11-03", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
    "/blog/patent-protection/": {
        "title": "Patent protection: what brands need to know",
        "cluster": "Brand Protection", "score": 4,
        "last_updated": "2024-03-28", "update_cooldown_until": None, "merge_cooldown_until": None,
    },
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
                    # KEEP_POSTS and GSC paths are canonically stored WITH a
                    # trailing slash (e.g. "/blog/foo/"). Omnia's citation
                    # URLs come back WITHOUT one (e.g. "/blog/foo"). Without
                    # normalizing here, every dict lookup against this data
                    # silently misses, curr/prev_citations are always 0, and
                    # the LLM drop check (which requires prev_citations > 0)
                    # never runs for any post, ever.
                    if not path.endswith("/"):
                        path += "/"
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

            # Diagnosis block for traffic flags
            diagnosis_html = ""
            brief_html = ""
            if f["type"] == "traffic" and f.get("diagnosis"):
                d = f["diagnosis"]
                verdict = d.get("verdict", "")
                action = d.get("recommended_action", "")
                keyword = d.get("primary_keyword", "")
                pos_now = d.get("our_position_now", "")
                pos_prev = d.get("our_position_prev", "")
                imp_signal = d.get("impressions_signal", "")

                # Signal label
                signal_map = {
                    "collapse": ("Impressions collapsed — possible indexing issue", "#dc2626"),
                    "drop": ("Impressions dropped — ranking loss", "#d97706"),
                    "stable": ("Impressions stable — CTR/SERP feature problem", "#2563eb"),
                    "slight_drop": ("Slight impression drop — ranking softening", "#d97706"),
                }
                signal_label, signal_color = signal_map.get(imp_signal, ("Signal unclear", "#64748b"))

                # Top query drops
                query_rows = ""
                for q in d.get("query_breakdown", [])[:3]:
                    change = q.get("change", 0)
                    change_str = f"{change}" if change >= 0 else f"{change}"
                    change_color = "#16a34a" if change >= 0 else "#dc2626"
                    query_rows += f"""
                    <tr>
                      <td style="padding:3px 6px 3px 0;font-size:11px;color:#334155;">{q['query'][:45]}</td>
                      <td style="font-family:monospace;font-size:11px;text-align:right;padding:3px 6px;">{q['prev_clicks']}</td>
                      <td style="font-family:monospace;font-size:11px;text-align:right;padding:3px 6px;">{q['curr_clicks']}</td>
                      <td style="font-family:monospace;font-size:11px;text-align:right;color:{change_color};padding:3px 0;">{change_str}</td>
                    </tr>"""

                # Top competitors
                comp_rows = ""
                for c in d.get("competitors", [])[:3]:
                    is_us = "redpoints.com" in c.get("url", "")
                    row_bg = "#eff6ff" if is_us else ""
                    comp_rows += f"""
                    <tr style="background:{row_bg};">
                      <td style="padding:3px 6px 3px 0;font-size:11px;color:{'#2563eb' if is_us else '#334155'};">{c['domain']}</td>
                      <td style="font-family:monospace;font-size:11px;text-align:center;padding:3px 6px;">{c['position']}</td>
                    </tr>"""

                diagnosis_html = f"""
                <div style="background:#f8fafc;border-radius:6px;padding:12px;margin-top:10px;">
                  <div style="font-size:10px;font-weight:600;color:{signal_color};margin-bottom:8px;">{signal_label.upper()}</div>
                  <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:10px;">
                    <tr style="color:#64748b;">
                      <td style="font-size:10px;padding:0 6px 4px 0;">Query</td>
                      <td style="font-size:10px;text-align:right;padding:0 6px 4px;">4 wks ago</td>
                      <td style="font-size:10px;text-align:right;padding:0 6px 4px;">this week</td>
                      <td style="font-size:10px;text-align:right;padding:0 0 4px;">change</td>
                    </tr>
                    {query_rows}
                  </table>
                  {f'''<div style="margin-bottom:8px;">
                  <div style="font-size:10px;font-weight:600;color:#166534;margin-bottom:4px;">SEMRUSH — TOP RESULTS FOR "{keyword[:40]}"</div>
                  <table width="100%" cellpadding="0" cellspacing="0">
                    <tr style="color:#64748b;"><td style="font-size:10px;padding:0 6px 4px 0;">Domain</td><td style="font-size:10px;text-align:center;padding:0 6px 4px;">Position</td></tr>
                    {comp_rows}
                  </table></div>''' if comp_rows else ''}
                  <div style="background:#0f172a;border-radius:6px;padding:10px;">
                    <div style="font-size:10px;color:#94a3b8;margin-bottom:4px;">ROOT CAUSE</div>
                    <div style="font-size:11px;color:white;line-height:1.5;margin-bottom:6px;">{verdict}</div>
                    <div style="border-top:1px solid #334155;padding-top:6px;">
                      <div style="font-size:10px;color:#94a3b8;margin-bottom:3px;">RECOMMENDED ACTION</div>
                      <div style="font-size:11px;color:#60a5fa;line-height:1.5;">{action}</div>
                    </div>
                  </div>
                </div>"""

                brief_html = f.get("content_brief_html", "")

            post_cards += f"""
            <tr>
              <td style="padding:0 0 12px 0;">
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
                      {diagnosis_html}
                      {brief_html}
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
    write_dashboard_data(gsc_service)
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

    # ── Phase 2: Diagnosis + content brief for all traffic drop posts ─────────
    prev_monday, prev_sunday = date_range_for_week_n(current_monday, 1)
    try:
        flagged = run_traffic_diagnosis(
            flagged_posts=flagged,
            gsc_service=gsc_service,
            week_start=week_start,
            week_end=week_end,
            prev_week_start=week_key(prev_monday),
            prev_week_end=prev_sunday.strftime("%Y-%m-%d"),
        )
        log.info("Phase 2 diagnosis completed")
    except Exception as e:
        log.error(f"Phase 2 diagnosis failed — email will send without briefs: {e}")

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
