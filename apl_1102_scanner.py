#!/usr/bin/env python3
"""
APL 1102 Role Scanner
=====================
Scans the Johns Hopkins University Applied Physics Laboratory (APL) careers site
for open positions aligned to the federal 1102 / acquisition-and-contracting
career field, scores each posting, scrapes the posted pay range, filters out
intern / entry-level roles, and writes a sortable HTML dashboard sorted by pay.

APL's careers site (careers.jhuapl.edu) runs on the Phenom People platform.
Listings come from the Phenom `/widgets` `refineSearch` endpoint (API-first);
pay is read from each job page's JSON-LD JobPosting schema, with a fallback to
APL's labeled "Minimum Rate / Maximum Rate" text (HTML fallback).

Usage:
    pip install requests
    python3 apl_1102_scanner.py --html dashboard.html

Common options:
    --keyword "acquisition"     extra search term to seed the Phenom query
    --max-pages 5               how many pages of results to pull (size 20 each)
    --min-score 3               drop postings scoring below this
    --include-junior            do NOT filter out intern/entry-level roles
    --no-pay-scrape             skip per-job pay scraping (faster, less data)
    --json out.json             also dump raw scored results to JSON

NOTE: If results come back empty, confirm REFNUM below against the live site
(auto-discovery is attempted first). View source on careers.jhuapl.edu and look
for "refNum":"...".
"""

import argparse
import html
import json
import re
import sys
import time
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("This tool needs the 'requests' library. Run:  pip install requests")

from dashboard_template import render_dashboard

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DOMAIN = "careers.jhuapl.edu"
# Leave REFNUM = None to auto-discover from the site HTML. If discovery fails,
# set it manually (view-source the careers site and search for "refNum").
REFNUM = None

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REQUEST_TIMEOUT = 25
POLITE_DELAY = 0.6  # seconds between per-job detail fetches

# Weighted keyword scoring. Title hits are worth more than description hits.
# Tuned for the 1102 (Contracting / Acquisition) federal career field.
ROLE_KEYWORDS = {
    # term: weight
    "1102": 6,
    "contracting officer": 6,
    "contract specialist": 5,
    "acquisition": 4,
    "procurement": 4,
    "source selection": 4,
    "subcontract": 3,
    "contract administration": 3,
    "contracts": 3,
    "far ": 3,          # Federal Acquisition Regulation (trailing space avoids "far-")
    "dfars": 3,
    "cost and price": 3,
    "price analysis": 3,
    "cost analysis": 2,
    "proposal": 2,
    "rfp": 2,
    "solicitation": 2,
    "negotiation": 2,
    "acquisition management": 4,
    "contract management": 4,
    "purchasing": 2,
    "supplier": 1,
    "vendor management": 1,
    "grants": 1,
    "compliance": 1,
}

# Titles/terms that mark a posting as junior / not a fit for a senior 1102.
JUNIOR_MARKERS = [
    "intern", "internship", "co-op", "co op", "coop", "student",
    "entry level", "entry-level", "early career", "apprentice",
    "summer", "trainee", "graduate program", "new grad", "recent graduate",
]

# Phenom widgets refineSearch payload (paginated job listings).
def _widgets_payload(keyword, page, size):
    return {
        "lang": "en_us",
        "deviceType": "desktop",
        "country": "us",
        "pageName": "search-results",
        "ddoKey": "refineSearch",
        "size": size,
        "from": page * size,
        "clearAll": False,
        "jdsource": "facets",
        "isSliderEnable": False,
        "jobs": True,
        "counts": False,
        "all_fields": ["category", "country", "state", "city", "type"],
        "pageId": "page20",
        "keywords": keyword or "",
        "global": True,
        "selected_fields": {},
        "sort": {"order": "", "field": ""},
    }


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------
def _session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": f"https://{DOMAIN}",
        "Referer": f"https://{DOMAIN}/us/en/search-results",
    })
    return s


def discover_refnum(session):
    """Best-effort extraction of the Phenom site refNum from the career page HTML."""
    global REFNUM
    if REFNUM:
        return REFNUM
    try:
        r = session.get(f"https://{DOMAIN}/us/en/search-results", timeout=REQUEST_TIMEOUT)
        for pat in (r'"refNum"\s*:\s*"([^"]+)"',
                    r'refNum\s*=\s*"([^"]+)"',
                    r'"careerSiteName"\s*:\s*"([^"]+)"'):
            m = re.search(pat, r.text)
            if m:
                REFNUM = m.group(1)
                return REFNUM
    except requests.RequestException:
        pass
    return None


def fetch_jobs(session, keyword, max_pages, size=20):
    """Pull paginated listings from the Phenom widgets refineSearch endpoint."""
    refnum = discover_refnum(session)
    url = f"https://{DOMAIN}/widgets"
    collected = []
    for page in range(max_pages):
        payload = _widgets_payload(keyword, page, size)
        if refnum:
            payload["refNum"] = refnum
        try:
            r = session.post(url, data=json.dumps(payload), timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"  ! page {page} fetch failed: {e}", file=sys.stderr)
            break

        jobs = _extract_jobs_list(data)
        if not jobs:
            break
        collected.extend(jobs)
        if len(jobs) < size:
            break
        time.sleep(POLITE_DELAY)
    return collected


def _extract_jobs_list(data):
    """Phenom nests the job array a few ways; try the known shapes."""
    if not isinstance(data, dict):
        return []
    candidates = [
        ("refineSearch", "data", "jobs"),
        ("data", "jobs"),
        ("eagerLoadRefineSearch", "data", "jobs"),
    ]
    for path in candidates:
        node = data
        ok = True
        for key in path:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                ok = False
                break
        if ok and isinstance(node, list):
            return node
    # Last resort: any list of dicts that looks like jobs
    for v in data.values():
        if isinstance(v, list) and v and isinstance(v[0], dict) and "title" in v[0]:
            return v
    return []


# ---------------------------------------------------------------------------
# Normalize, score, filter
# ---------------------------------------------------------------------------
def normalize(job):
    """Map a raw Phenom job dict into the fields the dashboard needs."""
    def g(*keys, default=""):
        for k in keys:
            if isinstance(job, dict) and job.get(k):
                return job.get(k)
        return default

    job_id = str(g("jobId", "id", "jobSeqNo", "reqId"))
    title = g("title", "jobTitle")
    location = g("cityStateCountry", "locationDisplay", "location", "cityState",
                 default=g("city"))
    teaser = g("descriptionTeaser", "description", "jobDescription")
    posted = g("postedDate", "dateCreated", "postedOn")
    url = f"https://{DOMAIN}/jobs/{job_id}?lang=en-us" if job_id else ""

    return {
        "id": job_id,
        "title": title.strip(),
        "location": re.sub(r"\s+", " ", str(location)).strip(),
        "teaser": re.sub(r"<[^>]+>", " ", str(teaser)),
        "posted": str(posted),
        "url": url,
        "min_pay": None,
        "max_pay": None,
        "score": 0,
        "matched": [],
    }


def score(job):
    """Weighted keyword score. Title matches count double."""
    title = job["title"].lower()
    blob = (job["title"] + " " + job["teaser"]).lower()
    total = 0
    matched = []
    for term, weight in ROLE_KEYWORDS.items():
        if term in blob:
            hits = weight
            if term in title:
                hits += weight  # title match counts double
            total += hits
            matched.append(term.strip())
    job["score"] = total
    job["matched"] = matched
    return total


def is_junior(job):
    text = (job["title"] + " " + job["teaser"]).lower()
    return any(marker in text for marker in JUNIOR_MARKERS)


# ---------------------------------------------------------------------------
# Pay scraping (API-first via JSON-LD, HTML fallback)
# ---------------------------------------------------------------------------
_MONEY = r"\$?\s*([0-9]{2,3}(?:,[0-9]{3})+|[0-9]{4,7})(?:\.\d{2})?"


def fetch_detail_pay(session, url):
    """Return (min_pay, max_pay) as ints, or (None, None) if not posted."""
    if not url:
        return (None, None)
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT,
                        headers={"Accept": "text/html,application/xhtml+xml"})
        r.raise_for_status()
        page = r.text
    except requests.RequestException:
        return (None, None)

    # 1) JSON-LD JobPosting schema (preferred / structured)
    for block in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            page, re.DOTALL | re.IGNORECASE):
        try:
            obj = json.loads(block.strip())
        except ValueError:
            continue
        for node in obj if isinstance(obj, list) else [obj]:
            if not isinstance(node, dict):
                continue
            if node.get("@type") in ("JobPosting", ["JobPosting"]):
                bs = node.get("baseSalary") or {}
                val = bs.get("value") if isinstance(bs, dict) else {}
                if isinstance(val, dict):
                    lo = _to_int(val.get("minValue"))
                    hi = _to_int(val.get("maxValue"))
                    if lo or hi:
                        return (lo, hi or lo)

    # 2) HTML fallback: APL's labeled "Minimum Rate / Maximum Rate" pattern
    lo = _find_labeled(page, r"min(?:imum)?\s*(?:rate|salary|pay)")
    hi = _find_labeled(page, r"max(?:imum)?\s*(?:rate|salary|pay)")
    if lo or hi:
        return (lo, hi or lo)

    return (None, None)


def _find_labeled(page, label_pat):
    m = re.search(label_pat + r"[^$0-9]{0,40}" + _MONEY, page, re.IGNORECASE)
    return _to_int(m.group(1)) if m else None


def _to_int(v):
    if v is None:
        return None
    try:
        return int(round(float(str(v).replace(",", "").replace("$", "").strip())))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Sort
# ---------------------------------------------------------------------------
def sort_key(job):
    # Pay descending (max first), score as tiebreaker, unpublished pay at bottom.
    has_pay = job["max_pay"] is not None
    return (
        0 if has_pay else 1,                 # published pay first
        -(job["max_pay"] or 0),              # higher max pay first
        -(job["score"] or 0),                # higher score first
        job["title"].lower(),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Scan APL careers for 1102/acquisition roles.")
    ap.add_argument("--html", metavar="FILE", default="dashboard.html",
                    help="Output HTML dashboard path (default: dashboard.html)")
    ap.add_argument("--keyword", default="acquisition contracting",
                    help="Seed search term sent to the careers site")
    ap.add_argument("--max-pages", type=int, default=6, help="Pages to pull (20/page)")
    ap.add_argument("--min-score", type=int, default=2, help="Drop postings below this score")
    ap.add_argument("--include-junior", action="store_true",
                    help="Do NOT filter intern/entry-level roles")
    ap.add_argument("--no-pay-scrape", action="store_true",
                    help="Skip per-job pay scraping")
    ap.add_argument("--json", metavar="FILE", help="Also write raw scored results to JSON")
    args = ap.parse_args()

    sess = _session()
    print(f"Scanning {DOMAIN} for 1102 / acquisition roles ...")
    raw = fetch_jobs(sess, args.keyword, args.max_pages)
    print(f"  pulled {len(raw)} raw postings")

    jobs = []
    seen = set()
    for r in raw:
        j = normalize(r)
        if not j["id"] or j["id"] in seen:
            continue
        seen.add(j["id"])
        score(j)
        if j["score"] < args.min_score:
            continue
        if not args.include_junior and is_junior(j):
            continue
        jobs.append(j)

    print(f"  {len(jobs)} postings after scoring/filtering")

    if not args.no_pay_scrape:
        print("  scraping posted pay ranges ...")
        for i, j in enumerate(jobs, 1):
            lo, hi = fetch_detail_pay(sess, j["url"])
            j["min_pay"], j["max_pay"] = lo, hi
            time.sleep(POLITE_DELAY)
            print(f"    [{i}/{len(jobs)}] {j['title'][:60]:<60} "
                  f"{'$%s-$%s' % (lo, hi) if hi else '(pay not posted)'}")

    jobs.sort(key=sort_key)

    generated = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    out_html = render_dashboard(jobs, generated)
    with open(args.html, "w", encoding="utf-8") as f:
        f.write(out_html)
    print(f"\nWrote dashboard -> {args.html}  ({len(jobs)} roles)")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(jobs, f, indent=2)
        print(f"Wrote raw results -> {args.json}")


if __name__ == "__main__":
    main()
