#!/usr/bin/env python3
"""
APL 1102 Role Scanner
=====================
Scans the Johns Hopkins University Applied Physics Laboratory (APL) careers site
for open positions aligned to the federal 1102 / acquisition-and-contracting
career field, scores each posting, scrapes the posted pay range, filters out
intern / entry-level roles, and writes a sortable HTML dashboard sorted by pay.

APL's careers site (careers.jhuapl.edu) runs on the Jibe platform (an iCIMS
career-site product). Listings come from the Jibe JSON endpoint at /api/jobs.
Job detail / deep links use the confirmed-working careers.jhuapl.edu/jobs/{id}
?lang=en-us format. Pay is read from the posting's own description text, then
from each job page's JSON-LD JobPosting schema, then from APL's labeled
"Minimum Rate / Maximum Rate" text (HTML fallback).

Usage:
    pip install requests
    python3 apl_1102_scanner.py --html dashboard.html

Common options:
    --keyword "acquisition"     keep only postings whose text contains this
    --max-pages 15              how many pages of results to pull (100 each)
    --min-score 2               drop postings scoring below this
    --include-junior            do NOT filter out intern/entry-level roles
    --no-pay-scrape             skip per-job pay page fetches (faster)
    --json out.json             also dump raw scored results to JSON
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
JOBS_API = f"https://{DOMAIN}/api/jobs"     # Jibe career-site listings endpoint
PAGE_SIZE = 100

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REQUEST_TIMEOUT = 25
POLITE_DELAY = 0.5  # seconds between requests

# Weighted keyword scoring. Title hits count double (see score()).
ROLE_KEYWORDS = {
    "1102": 6,
    "contracting officer": 6,
    "contract specialist": 5,
    "acquisition": 4,
    "procurement": 4,
    "source selection": 4,
    "acquisition management": 4,
    "contract management": 4,
    "subcontract": 3,
    "contract administration": 3,
    "contracts": 3,
    "far ": 3,
    "dfars": 3,
    "cost and price": 3,
    "price analysis": 3,
    "cost analysis": 2,
    "proposal": 2,
    "rfp": 2,
    "solicitation": 2,
    "negotiation": 2,
    "purchasing": 2,
    "supplier": 1,
    "vendor management": 1,
    "grants": 1,
    "compliance": 1,
    "agreements": 4,
    "buyer": 4,
    "sourcing": 3,
    "supply chain": 2,
    "trade compliance": 3,
    "export control": 3,
    "cost estimating": 2,
    "cost estimator": 2,
}

# Terms that, when present in a TITLE, reliably indicate an acquisition /
# contracting (1102-family) role. Used as the precision gate in the filter.
ACQ_TITLE_TERMS = [
    "1102", "contracting officer", "contract specialist", "contract administrat",
    "contract management", "contract negotiat", "contracts", "subcontract",
    "procurement", "agreements", "buyer", "sourcing", "supply chain",
    "trade compliance", "export control", "cost and price", "price analyst",
    "cost estimat", "purchasing",
]


def is_acquisition_title(title):
    """True if the job TITLE signals an acquisition/contracting role."""
    t = title.lower()
    # "acquisition" in the procurement sense, not data/signal/image acquisition.
    if "acquisition" in t and not re.search(
            r"(data|signal|image|target|talent|requirements?)\s+acquisition", t):
        return True
    return any(term in t for term in ACQ_TITLE_TERMS)

JUNIOR_MARKERS = [
    "intern", "internship", "co-op", "co op", "coop", "student",
    "entry level", "entry-level", "early career", "apprentice",
    "summer", "trainee", "graduate", "graduate program",
    "new grad", "recent graduate",
]
# Match markers as whole words so "intern" does NOT fire on "International".
_JUNIOR_RE = re.compile(
    r"\b(" + "|".join(re.escape(m) for m in JUNIOR_MARKERS) + r")\b")


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
def _session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Referer": f"https://{DOMAIN}/jobs",
    })
    return s


def fetch_jobs(session, max_pages):
    """Pull paginated listings from the Jibe /api/jobs endpoint."""
    collected = []
    total = None
    for page in range(1, max_pages + 1):
        params = {"page": page, "limit": PAGE_SIZE,
                  "sortBy": "relevance", "descending": "false"}
        try:
            r = session.get(JOBS_API, params=params, timeout=REQUEST_TIMEOUT)
            if page == 1:
                print(f"  GET {r.url} -> HTTP {r.status_code}")
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"  ! page {page} fetch failed: {e}", file=sys.stderr)
            break

        batch = _extract_jobs_list(data)
        if total is None:
            total = _extract_total(data)
            print(f"  API reports total jobs: {total if total is not None else 'unknown'}")
            if not batch and isinstance(data, dict):
                print(f"  (top-level response keys: {list(data.keys())})")
        if not batch:
            break
        collected.extend(batch)
        if total is not None and len(collected) >= total:
            break
        if len(batch) < PAGE_SIZE:
            break
        time.sleep(POLITE_DELAY)
    return collected


def _extract_jobs_list(data):
    """Jibe shapes vary; find the list of job records."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for path in (("jobs",), ("data", "jobs"), ("results",), ("data",)):
        node = data
        ok = True
        for k in path:
            if isinstance(node, dict) and k in node:
                node = node[k]
            else:
                ok = False
                break
        if ok and isinstance(node, list):
            return node
    return []


def _extract_total(data):
    if not isinstance(data, dict):
        return None
    for k in ("totalCount", "total", "count", "totalJobs"):
        if isinstance(data.get(k), int):
            return data[k]
    meta = data.get("meta") or {}
    if isinstance(meta, dict):
        for k in ("totalCount", "total", "count"):
            if isinstance(meta.get(k), int):
                return meta[k]
    return None


# ---------------------------------------------------------------------------
# Normalize / score / filter
# ---------------------------------------------------------------------------
def normalize(item):
    """Map a raw Jibe job record (often wrapped in {'data': {...}}) to our fields."""
    d = item.get("data") if isinstance(item, dict) and isinstance(item.get("data"), dict) else item
    if not isinstance(d, dict):
        d = {}

    def g(*keys, default=""):
        for k in keys:
            v = d.get(k)
            if v:
                return v
        return default

    job_id = str(g("req_id", "reqId", "id", "jobId", "requisition_id"))
    title = str(g("title", "name", "job_title"))
    location = str(g("full_location", "location_name", "location", "city_state",
                     default=", ".join(x for x in [str(d.get("city", "")),
                                                   str(d.get("state", ""))] if x)))
    desc = str(g("description", "job_description", "description_teaser", "summary"))
    posted = str(g("posted_date", "create_date", "createDate", "date_posted",
                   "update_date", "postedDate"))
    # Pay is returned directly in the listing on this site.
    api_min = _to_int(d.get("salary_min_value"))
    api_max = _to_int(d.get("salary_max_value"))
    if api_max is None:
        api_max = _to_int(d.get("salary_value"))
    # Confirmed-working deep link format for APL.
    url = f"https://{DOMAIN}/jobs/{job_id}?lang=en-us" if job_id else \
          str(g("apply_url", "url"))

    return {
        "id": job_id,
        "title": title.strip(),
        "location": re.sub(r"\s+", " ", location).strip().strip(","),
        "desc_text": re.sub(r"<[^>]+>", " ", desc),
        "posted": _clean_date(posted),
        "url": url,
        "_raw": d,
        "min_pay": api_min,
        "max_pay": api_max,
        "score": 0,
        "matched": [],
    }


def _clean_date(s):
    s = (s or "").strip()
    m = re.search(r"\d{4}-\d{2}-\d{2}", s)
    if m:
        return m.group(0)
    m = re.search(r"[A-Z][a-z]+ \d{1,2},? \d{4}", s)
    return m.group(0) if m else s[:10]


def score(job):
    title = job["title"].lower()
    blob = (job["title"] + " " + job["desc_text"]).lower()
    total = 0
    matched = []
    for term, weight in ROLE_KEYWORDS.items():
        if term in blob:
            hits = weight + (weight if term in title else 0)
            total += hits
            matched.append(term.strip())
    job["score"] = total
    job["matched"] = matched
    return total


def is_junior(job):
    # Judge seniority from the TITLE only, matching whole words so "intern"
    # does not fire on "International" and "graduate" not on "undergraduate".
    return bool(_JUNIOR_RE.search(job["title"].lower()))


# ---------------------------------------------------------------------------
# Pay extraction (description text -> JSON-LD -> labeled HTML rate)
# ---------------------------------------------------------------------------
_MONEY = r"\$?\s*([0-9]{2,3}(?:,[0-9]{3})+|[0-9]{4,7})(?:\.\d{2})?"


def pay_from_text(text):
    lo = _find_labeled(text, r"min(?:imum)?\s*(?:rate|salary|pay|annual)")
    hi = _find_labeled(text, r"max(?:imum)?\s*(?:rate|salary|pay|annual)")
    if lo or hi:
        return (lo, hi or lo)
    return (None, None)


def pay_from_page(session, url):
    if not url:
        return (None, None)
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT,
                        headers={"Accept": "text/html,application/xhtml+xml"})
        r.raise_for_status()
        page = r.text
    except requests.RequestException:
        return (None, None)

    # JSON-LD JobPosting baseSalary (structured, preferred)
    for block in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            page, re.DOTALL | re.IGNORECASE):
        try:
            obj = json.loads(block.strip())
        except ValueError:
            continue
        for node in (obj if isinstance(obj, list) else [obj]):
            if isinstance(node, dict) and "JobPosting" in str(node.get("@type", "")):
                val = (node.get("baseSalary") or {}).get("value") or {}
                if isinstance(val, dict):
                    lo = _to_int(val.get("minValue"))
                    hi = _to_int(val.get("maxValue"))
                    if lo or hi:
                        return (lo, hi or lo)
    # Labeled "Minimum Rate / Maximum Rate" text fallback
    return pay_from_text(page)


def _find_labeled(text, label_pat):
    m = re.search(label_pat + r"[^$0-9]{0,80}" + _MONEY, text, re.IGNORECASE)
    return _to_int(m.group(1)) if m else None


# Text fields in the API record that may carry the posted pay range.
PAY_TEXT_FIELDS = ("description", "qualifications", "responsibilities",
                   "salary_value", "promotion_value", "meta_data")


def _record_text(d):
    parts = []
    for k in PAY_TEXT_FIELDS:
        v = d.get(k)
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, (dict, list)):
            parts.append(json.dumps(v))
    return re.sub(r"<[^>]+>", " ", " ".join(parts))


def extract_pay(d):
    """Mine a (min, max) pay pair from the record's text fields."""
    text = _record_text(d)
    # 1) Labeled min / max (APL uses "Minimum Rate / Maximum Rate").
    lo = _find_labeled(text, r"min(?:imum)?\s*(?:annual\s*|base\s*)?"
                              r"(?:rate|salary|pay|compensation)")
    hi = _find_labeled(text, r"max(?:imum)?\s*(?:annual\s*|base\s*)?"
                              r"(?:rate|salary|pay|compensation)")
    if lo and hi:
        return (min(lo, hi), max(lo, hi))
    # 2) A range near a pay cue: "...salary range ... $X ... $Y ...".
    cue = re.search(r"(?i)(salary|pay\s*range|pay\s*rate|compensation|"
                    r"hiring\s*range|annual\s*rate|referenced\s*pay)", text)
    if cue:
        window = text[cue.start(): cue.start() + 240]
        nums = [n for n in (_to_int(x) for x in re.findall(_MONEY, window))
                if n and n >= 20000]
        if len(nums) >= 2:
            return (min(nums[0], nums[1]), max(nums[0], nums[1]))
    if lo or hi:
        return (lo, hi or lo)
    return (None, None)


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
    has_pay = job["max_pay"] is not None
    return (0 if has_pay else 1, -(job["max_pay"] or 0),
            -(job["score"] or 0), job["title"].lower())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Scan APL careers for 1102/acquisition roles.")
    ap.add_argument("--html", metavar="FILE", default="dashboard.html",
                    help="Output HTML dashboard path (default: dashboard.html)")
    ap.add_argument("--keyword", default="",
                    help="Keep only postings whose text contains this term")
    ap.add_argument("--max-pages", type=int, default=15, help="Pages to pull (100/page)")
    ap.add_argument("--min-score", type=int, default=12,
                    help="Score needed to keep a role that has NO acquisition "
                         "keyword in its title (title matches are always kept)")
    ap.add_argument("--include-junior", action="store_true",
                    help="Do NOT filter intern/entry-level roles")
    ap.add_argument("--no-pay-scrape", action="store_true",
                    help="Skip per-job pay page fetches")
    ap.add_argument("--json", metavar="FILE", help="Also write raw scored results to JSON")
    args = ap.parse_args()

    sess = _session()
    print(f"Scanning {DOMAIN} (Jibe /api/jobs) for 1102 / acquisition roles ...")
    raw = fetch_jobs(sess, args.max_pages)
    print(f"  pulled {len(raw)} raw postings")

    # One-time structure diagnostic: shows Jibe's actual field names.
    if raw:
        sample = raw[0]
        print("  --- SAMPLE RECORD (for field mapping) ---")
        print("  top-level keys:", list(sample.keys()) if isinstance(sample, dict) else type(sample))
        inner = sample.get("data") if isinstance(sample, dict) and isinstance(sample.get("data"), dict) else None
        if inner is not None:
            print("  data keys:", list(inner.keys()))
        print("  sample JSON:", json.dumps(sample)[:1200])
        print("  --- END SAMPLE ---")

    jobs, seen = [], set()
    kw = args.keyword.lower().strip()
    all_scored = []
    for item in raw:
        j = normalize(item)
        if not j["id"] or j["id"] in seen:
            continue
        seen.add(j["id"])
        score(j)
        all_scored.append(j)

    # Diagnostic: show the highest-scoring titles regardless of threshold,
    # so we can see APL's actual role taxonomy and tune ROLE_KEYWORDS.
    top = sorted(all_scored, key=lambda x: -x["score"])[:20]
    print("  --- TOP 20 BY SCORE (diagnostic) ---")
    for j in top:
        pay = f"${j['max_pay']:,}" if j["max_pay"] else "n/a"
        print(f"   score {j['score']:>3} | {j['title'][:55]:<55} | {pay}")
    print("  --- END TOP 20 ---")

    for j in all_scored:
        if kw and kw not in (j["title"] + " " + j["desc_text"]).lower():
            continue
        # Precision gate: keep if the TITLE signals acquisition, OR the overall
        # score is high enough that a description-saturated role qualifies.
        if not (is_acquisition_title(j["title"]) or j["score"] >= args.min_score):
            continue
        if not args.include_junior and is_junior(j):
            continue
        jobs.append(j)

    print(f"  {len(jobs)} postings after scoring/filtering")

    for j in jobs:
        if j["max_pay"] is None:
            lo, hi = extract_pay(j["_raw"])              # from the API text fields
            if hi is None and not args.no_pay_scrape:
                lo, hi = pay_from_page(sess, j["url"])     # else fetch the job page
                time.sleep(POLITE_DELAY)
            j["min_pay"], j["max_pay"] = lo, hi

    # PAY DIAGNOSTIC: show where/whether pay text appears for each kept role.
    print("  --- PAY DEBUG ---")
    money_re = re.compile(r"\$\s?\d{2,3}(?:,\d{3})+|\$\s?\d{4,7}")
    for j in jobs:
        raw = j.get("_raw", {})
        hit = None
        for k, v in raw.items():
            if isinstance(v, str):
                m = money_re.search(v) or re.search(
                    r"(?i)(minimum rate|maximum rate|salary|pay range|compensation)", v)
                if m:
                    s, e = max(0, m.start() - 50), min(len(v), m.start() + 110)
                    snip = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", v[s:e])).strip()
                    hit = f"[{k}] ...{snip}..."
                    break
        print(f"   {j['title'][:40]:<40} pay=({j['min_pay']},{j['max_pay']}) {hit or '(no $/pay text found)'}")
    print("  --- END PAY DEBUG ---")

    jobs.sort(key=sort_key)

    generated = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    with open(args.html, "w", encoding="utf-8") as f:
        f.write(render_dashboard(jobs, generated))
    print(f"\nWrote dashboard -> {args.html}  ({len(jobs)} roles)")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(jobs, f, indent=2)
        print(f"Wrote raw results -> {args.json}")


if __name__ == "__main__":
    main()
