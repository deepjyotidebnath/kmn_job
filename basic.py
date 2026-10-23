# requirements.txt
#   streamlit>=1.50
#   pandas
#   requests
#   beautifulsoup4
#   reportlab
#   python-jobspy
#
# .streamlit/secrets.toml  (optional keys for the "(key)" sources)
#   ADZUNA_APP_ID = ""
#   ADZUNA_APP_KEY = ""
#   JOOBLE_KEY = ""
#   RAPIDAPI_KEY = ""
#   SERPAPI_KEY = ""

import hashlib
import io
import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, quote_plus, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

DEFAULT_KEYWORDS = [
    "machine learning",
    "artificial intelligence",
    "data science",
    "python developer",
    "generative AI",
]

SENIOR_WORDS = re.compile(
    r"\b(?:senior|sr|lead|principal|staff|manager|head|director|architect|vp|"
    r"iii|iv|expert|consultant)\b",
    re.I,
)
FRESHER_WORDS = re.compile(
    r"\b(?:intern|internship|fresher|graduate|entry|trainee|junior|associate|"
    r"early career|campus|new grad|university|2027|2026|apprentice)\b",
    re.I,
)
ROLE_WORDS = re.compile(
    r"\b(?:ai|ml|machine learning|data scien|data analy|deep learning|nlp|llm|"
    r"generative|python|computer vision|software|sde|research|applied scien|"
    r"data engineer|mlops)\b",
    re.I,
)

GREENHOUSE_BOARDS = {
    "anthropic": "Anthropic",
    "databricks": "Databricks",
    "scaleai": "Scale AI",
    "huggingface": "Hugging Face",
    "cohere": "Cohere",
    "stabilityai": "Stability AI",
    "runwayml": "Runway",
    "glean": "Glean",
}
LEVER_COMPANIES = {"cred": "CRED", "meesho": "Meesho", "palantir": "Palantir", "spotify": "Spotify"}
ASHBY_COMPANIES = {"openai": "OpenAI", "notion": "Notion", "ramp": "Ramp", "perplexity": "Perplexity"}
SIMPLIFY_URLS = {
    "SimplifyJobs New-Grad": "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
    "SimplifyJobs Internships 2027": "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
    "SimplifyJobs Internships 2026": "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
}
WWR_FEEDS = [
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "https://weworkremotely.com/remote-jobs.rss",
]

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
API_KEYS = {}


def secret(name):
    try:
        return st.secrets.get(name, "")
    except Exception:
        return ""


def load_keys():
    for k in ("ADZUNA_APP_ID", "ADZUNA_APP_KEY", "JOOBLE_KEY", "RAPIDAPI_KEY", "SERPAPI_KEY"):
        API_KEYS[k] = secret(k)


def _s(v):
    return "" if v is None or (isinstance(v, float) and v != v) else str(v)


def _first(*vals):
    for v in vals:
        s = _s(v)
        if s and s.lower() not in ("nan", "none"):
            return s
    return None


def _date(v):
    if v is None or (isinstance(v, float) and v != v):
        return ""
    try:
        if isinstance(v, (int, float)):
            v = v / 1000 if v > 1e11 else v
            return datetime.fromtimestamp(v).strftime("%Y-%m-%d")
        ts = pd.to_datetime(str(v), utc=True, errors="coerce")
        if pd.notna(ts):
            return ts.strftime("%Y-%m-%d")
    except Exception:
        pass
    return str(v)[:10]


def _row(title, company, loc, source, posted, link, kw):
    loc = _s(loc)
    if len(loc) > 80:
        loc = loc[:77] + "..."
    return {
        "Title": re.sub(r"<[^>]+>", "", _s(title))[:150],
        "Company": _s(company)[:60],
        "Location": loc or "Remote",
        "Source": source,
        "Posted": _date(posted),
        "Apply Link": link,
        "Keyword": kw,
    }


def _match(text, keywords):
    t = (text or "").lower()
    return any(k.lower() in t for k in keywords) or bool(ROLE_WORDS.search(t))


def _get(url, **kw):
    return requests.get(url, headers=HEADERS, timeout=kw.pop("timeout", 20), **kw)


# --------------------------------------------------------------------------
# Persistent storage: applied jobs + registry of current results
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
APPLIED_FILE = BASE_DIR / "applied_jobs.json"
REGISTRY_FILE = BASE_DIR / "job_registry.json"
TRACKER_PORT = 8765
LOCK = threading.Lock()


def job_key(url: str) -> str:
    return hashlib.md5(str(url).strip().encode()).hexdigest()[:12]


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(path: Path, data: dict):
    try:
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except Exception:
        pass


def use_file() -> bool:
    return bool(st.session_state.get("use_file", False))


def file_set_applied(key: str, info: dict, applied: bool):
    with LOCK:
        data = _load(APPLIED_FILE)
        if applied:
            data.setdefault(key, {**info, "applied_at": datetime.now().strftime("%d %b %Y %H:%M")})
        else:
            data.pop(key, None)
        _save(APPLIED_FILE, data)


def load_applied() -> dict:
    if use_file():
        with LOCK:
            return _load(APPLIED_FILE)
    return st.session_state.setdefault("applied", {})


def set_applied(key: str, info: dict, applied: bool):
    if use_file():
        file_set_applied(key, info, applied)
        return
    d = st.session_state.setdefault("applied", {})
    if applied:
        d.setdefault(key, {**info, "applied_at": datetime.now().strftime("%d %b %Y %H:%M")})
    else:
        d.pop(key, None)


def clear_applied():
    if use_file():
        with LOCK:
            _save(APPLIED_FILE, {})
    else:
        st.session_state["applied"] = {}


def register_jobs(df: pd.DataFrame):
    """Remember key -> job info so the redirect server can look the link up."""
    with LOCK:
        reg = _load(REGISTRY_FILE)
        for _, r in df.iterrows():
            reg[r["Key"]] = {
                "link": r["Apply Link"],
                "title": r["Title"],
                "company": r["Company"],
                "source": r["Source"],
            }
        _save(REGISTRY_FILE, reg)


class TrackerHandler(BaseHTTPRequestHandler):
    """GET /go?k=<key>  ->  mark job as applied, then 302 to the real job page."""

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        key = (q.get("k") or [""])[0]
        with LOCK:
            job = _load(REGISTRY_FILE).get(key)
        if not job:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Unknown job link. Run the search again.")
            return
        file_set_applied(key, job, True)
        self.send_response(302)
        self.send_header("Location", job["link"])
        self.end_headers()

    def log_message(self, *args):
        pass


@st.cache_resource
def start_tracker(port: int) -> bool:
    try:
        srv = HTTPServer(("127.0.0.1", port), TrackerHandler)
    except OSError:
        return True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return True


# --------------------------------------------------------------------------
# Layer 1a: main live scrapers
# --------------------------------------------------------------------------
def scrape_jobspy(keywords, location, per_kw, hours_old, sites):
    rows = []
    try:
        from jobspy import scrape_jobs
    except ImportError:
        st.warning("python-jobspy not installed - skipping LinkedIn/Indeed/Naukri.")
        return rows
    for kw in keywords:
        for site in sites:
            try:
                df = scrape_jobs(
                    site_name=[site],
                    search_term=f"{kw} fresher intern",
                    location=location,
                    results_wanted=per_kw,
                    hours_old=hours_old,
                    country_indeed="India",
                    linkedin_fetch_description=False,
                )
                for _, r in df.iterrows():
                    rows.append(
                        _row(
                            r.get("title"),
                            r.get("company"),
                            r.get("location"),
                            _s(r.get("site") or site).capitalize(),
                            r.get("date_posted"),
                            _first(r.get("job_url_direct"), r.get("job_url")),
                            kw,
                        )
                    )
            except Exception as e:
                st.toast(f"{site} failed for '{kw}': {str(e)[:80]}")
            time.sleep(0.5)
    return rows


def scrape_internshala(keywords, per_kw):
    rows = []
    for kw in keywords:
        slug = kw.lower().replace(" ", "-")
        for kind, path in (("Internship", "internships"), ("Job", "jobs")):
            url = f"https://internshala.com/{path}/keywords-{slug}/"
            try:
                html = requests.get(url, headers=HEADERS, timeout=15).text
                soup = BeautifulSoup(html, "html.parser")
                for c in soup.select("div.individual_internship")[:per_kw]:
                    a = c.select_one("a.job-title-href") or c.select_one("a[href*='/detail/']")
                    href = c.get("data-href") or (a.get("href") if a else None)
                    if not href:
                        continue
                    comp = c.select_one(".company-name, .company_name")
                    loc = c.select_one(".locations, .location_link")
                    rows.append(
                        _row(
                            a.get_text(strip=True) if a else kw.title(),
                            comp.get_text(strip=True) if comp else "",
                            loc.get_text(" ", strip=True) if loc else "",
                            f"Internshala ({kind})",
                            "",
                            "https://internshala.com" + href if href.startswith("/") else href,
                            kw,
                        )
                    )
            except Exception:
                continue
    return rows


def scrape_amazon(keywords, per_kw):
    rows = []
    for kw in keywords:
        try:
            r = requests.get(
                "https://www.amazon.jobs/en/search.json",
                params={
                    "base_query": kw,
                    "loc_query": "India",
                    "country": "IND",
                    "result_limit": per_kw,
                    "sort": "recent",
                },
                headers=HEADERS,
                timeout=20,
            ).json()
            for j in r.get("jobs", []):
                rows.append(
                    _row(
                        j.get("title"),
                        "Amazon",
                        j.get("normalized_location") or j.get("location"),
                        "Amazon Careers",
                        j.get("posted_date"),
                        "https://www.amazon.jobs" + j.get("job_path", ""),
                        kw,
                    )
                )
        except Exception:
            continue
    return rows


def scrape_greenhouse(keywords, boards):
    rows = []
    pat = re.compile("|".join(re.escape(k.split()[0]) for k in keywords) + "|intern|graduate", re.I)
    for token, name in boards.items():
        try:
            r = requests.get(
                f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
                headers=HEADERS,
                timeout=20,
            ).json()
            for j in r.get("jobs", []):
                title = j.get("title", "")
                loc = (j.get("location") or {}).get("name", "")
                if pat.search(title) and ("india" in loc.lower() or "remote" in loc.lower()):
                    rows.append(
                        _row(title, name, loc, "Company Careers", j.get("updated_at"),
                             j.get("absolute_url"), "AI company")
                    )
        except Exception:
            continue
    return rows


# --------------------------------------------------------------------------
# Layer 1b: remote / fresher sources (all take keywords, per_kw, days)
# --------------------------------------------------------------------------
def scrape_remotive(keywords, per_kw, days=30):
    rows = []
    for kw in keywords:
        try:
            r = _get("https://remotive.com/api/remote-jobs", params={"search": kw, "limit": per_kw}).json()
            for j in r.get("jobs", []):
                rows.append(_row(j.get("title"), j.get("company_name"), j.get("candidate_required_location"),
                                 "Remotive", j.get("publication_date"), j.get("url"), kw))
        except Exception:
            continue
    return rows


def scrape_remoteok(keywords, per_kw, days=30):
    rows = []
    try:
        data = _get("https://remoteok.com/api").json()[1:]
    except Exception:
        return rows
    for j in data:
        text = f"{j.get('position', '')} {' '.join(j.get('tags') or [])}"
        if _match(text, keywords):
            rows.append(_row(j.get("position"), j.get("company"), j.get("location"), "RemoteOK",
                             j.get("date"), j.get("url") or j.get("apply_url"), "remote"))
    return rows[: per_kw * max(len(keywords), 1)]


def scrape_himalayas(keywords, per_kw, days=30):
    rows = []
    for kw in keywords:
        try:
            r = _get("https://himalayas.app/jobs/api/search", params={"q": kw, "page": 1}).json()
            for j in r.get("jobs", [])[:per_kw]:
                loc = ", ".join(j.get("locationRestrictions") or []) or "Worldwide"
                rows.append(_row(j.get("title"), j.get("companyName"), loc, "Himalayas",
                                 j.get("pubDate"), j.get("applicationLink") or j.get("guid"), kw))
        except Exception:
            continue
    return rows


def scrape_wwr(keywords, per_kw, days=30):
    rows = []
    for feed in WWR_FEEDS:
        try:
            root = ET.fromstring(_get(feed).content)
            for it in root.iter("item"):
                raw = it.findtext("title") or ""
                company, _, title = raw.partition(":")
                if not title:
                    company, title = "", raw
                if _match(title, keywords):
                    rows.append(_row(title.strip(), company.strip(), it.findtext("region") or "Remote",
                                     "We Work Remotely", it.findtext("pubDate"), it.findtext("link"), "remote"))
        except Exception:
            continue
    return rows[: per_kw * max(len(keywords), 1)]


def scrape_arbeitnow(keywords, per_kw, days=30):
    rows = []
    try:
        data = _get("https://www.arbeitnow.com/api/job-board-api").json().get("data", [])
    except Exception:
        return rows
    for j in data:
        text = f"{j.get('title', '')} {' '.join(j.get('tags') or [])}"
        if _match(text, keywords):
            rows.append(_row(j.get("title"), j.get("company_name"),
                             "Remote" if j.get("remote") else j.get("location"),
                             "Arbeitnow", j.get("created_at"), j.get("url"), "remote"))
    return rows[: per_kw * max(len(keywords), 1)]


def scrape_adzuna(keywords, per_kw, days=30):
    app_id, app_key = API_KEYS.get("ADZUNA_APP_ID"), API_KEYS.get("ADZUNA_APP_KEY")
    rows = []
    if not (app_id and app_key):
        return rows
    for kw in keywords:
        try:
            r = _get(
                "https://api.adzuna.com/v1/api/jobs/in/search/1",
                params={"app_id": app_id, "app_key": app_key, "what": f"{kw} fresher",
                        "results_per_page": per_kw, "sort_by": "date", "max_days_old": days},
            ).json()
            for j in r.get("results", []):
                rows.append(_row(j.get("title"), (j.get("company") or {}).get("display_name"),
                                 (j.get("location") or {}).get("display_name"), "Adzuna",
                                 j.get("created"), j.get("redirect_url"), kw))
        except Exception:
            continue
    return rows


def scrape_jooble(keywords, per_kw, days=30):
    key = API_KEYS.get("JOOBLE_KEY")
    rows = []
    if not key:
        return rows
    for kw in keywords:
        try:
            r = requests.post(f"https://jooble.org/api/{key}",
                              json={"keywords": f"{kw} fresher", "location": "India"}, timeout=20).json()
            for j in r.get("jobs", [])[:per_kw]:
                rows.append(_row(j.get("title"), j.get("company"), j.get("location"), "Jooble",
                                 j.get("updated"), j.get("link"), kw))
        except Exception:
            continue
    return rows


def scrape_jsearch(keywords, per_kw, days=30):
    key = API_KEYS.get("RAPIDAPI_KEY")
    rows = []
    if not key:
        return rows
    for kw in keywords:
        try:
            r = requests.get(
                "https://jsearch.p.rapidapi.com/search",
                headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"},
                params={"query": f"{kw} fresher intern in India", "page": 1, "num_pages": 1,
                        "date_posted": "month"},
                timeout=25,
            ).json()
            for j in r.get("data", [])[:per_kw]:
                loc = ", ".join(x for x in (j.get("job_city"), j.get("job_state"), j.get("job_country")) if x)
                rows.append(_row(j.get("job_title"), j.get("employer_name"), loc,
                                 f"JSearch ({j.get('job_publisher', '')})",
                                 j.get("job_posted_at_datetime_utc"), j.get("job_apply_link"), kw))
        except Exception:
            continue
    return rows


def scrape_serpapi(keywords, per_kw, days=30):
    key = API_KEYS.get("SERPAPI_KEY")
    rows = []
    if not key:
        return rows
    for kw in keywords:
        try:
            r = _get("https://serpapi.com/search.json",
                     params={"engine": "google_jobs", "q": f"{kw} fresher intern",
                             "location": "India", "api_key": key}).json()
            for j in r.get("jobs_results", [])[:per_kw]:
                opts = j.get("apply_options") or []
                link = opts[0].get("link") if opts else j.get("share_link")
                rows.append(_row(j.get("title"), j.get("company_name"), j.get("location"), "Google Jobs",
                                 (j.get("detected_extensions") or {}).get("posted_at"), link, kw))
        except Exception:
            continue
    return rows


def _ats_pat(keywords):
    return re.compile("|".join(re.escape(k.split()[0]) for k in keywords) + "|intern|graduate|fresher", re.I)


def scrape_lever(keywords, per_kw, days=30):
    rows, pat = [], _ats_pat(keywords)
    for token, name in LEVER_COMPANIES.items():
        try:
            for j in _get(f"https://api.lever.co/v0/postings/{token}", params={"mode": "json"}).json():
                loc = (j.get("categories") or {}).get("location", "") or ""
                if pat.search(j.get("text", "")) and re.search("india|remote", loc, re.I):
                    rows.append(_row(j.get("text"), name, loc, "Lever", j.get("createdAt"),
                                     j.get("hostedUrl"), "Company"))
        except Exception:
            continue
    return rows


def scrape_ashby(keywords, per_kw, days=30):
    rows, pat = [], _ats_pat(keywords)
    for token, name in ASHBY_COMPANIES.items():
        try:
            r = _get(f"https://api.ashbyhq.com/posting-api/job-board/{token}").json()
            for j in r.get("jobs", []):
                loc = j.get("location") or ""
                if pat.search(j.get("title", "")) and (j.get("isRemote") or re.search("india|remote", loc, re.I)):
                    rows.append(_row(j.get("title"), name, loc, "Ashby", j.get("publishedAt"),
                                     j.get("jobUrl"), "Company"))
        except Exception:
            continue
    return rows


def scrape_hn(keywords, per_kw, days=30):
    rows = []
    try:
        s = _get("https://hn.algolia.com/api/v1/search_by_date",
                 params={"query": "Ask HN: Who is hiring?", "tags": "story,author_whoishiring",
                         "hitsPerPage": 5}).json()
        tid = next(h["objectID"] for h in s["hits"] if "who is hiring" in h["title"].lower())
    except Exception:
        return rows
    for kw in keywords:
        try:
            r = _get("https://hn.algolia.com/api/v1/search",
                     params={"query": kw, "tags": f"comment,story_{tid}", "hitsPerPage": per_kw}).json()
            for h in r.get("hits", []):
                text = BeautifulSoup(h.get("comment_text") or "", "html.parser").get_text("\n").strip()
                first = text.split("\n")[0] if text else ""
                parts = [p.strip() for p in first.split("|")]
                loc = next((p for p in parts if re.search("remote|india|onsite|hybrid", p, re.I)), "")
                title = " | ".join(parts[1:3]) if len(parts) > 1 else first[:100]
                rows.append(_row(title, parts[0][:60], loc, "HN Who is Hiring", h.get("created_at"),
                                 f"https://news.ycombinator.com/item?id={h['objectID']}", kw))
        except Exception:
            continue
    return rows


def scrape_simplify(keywords, per_kw, days=30):
    rows = []
    for name, url in SIMPLIFY_URLS.items():
        try:
            data = _get(url, timeout=40).json()
        except Exception:
            continue
        n = 0
        for j in data:
            if not (j.get("active") and j.get("is_visible", True)):
                continue
            title, loc = j.get("title", ""), ", ".join(j.get("locations") or [])
            if _match(title, keywords) and re.search("india|remote", loc, re.I):
                rows.append(_row(title, j.get("company_name"), loc, name, j.get("date_posted"),
                                 j.get("url"), "GitHub list"))
                n += 1
                if n >= per_kw * 5:
                    break
    return rows


EXTRA_SOURCES = {
    "Remotive": scrape_remotive,
    "RemoteOK": scrape_remoteok,
    "Himalayas": scrape_himalayas,
    "We Work Remotely": scrape_wwr,
    "Arbeitnow": scrape_arbeitnow,
    "Adzuna (key)": scrape_adzuna,
    "Jooble (key)": scrape_jooble,
    "JSearch (key)": scrape_jsearch,
    "SerpAPI Google Jobs (key)": scrape_serpapi,
    "Lever": scrape_lever,
    "Ashby": scrape_ashby,
    "HN Who is Hiring": scrape_hn,
    "SimplifyJobs GitHub": scrape_simplify,
}


def run_extra_sources(names, keywords, per_kw, days):
    rows = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(EXTRA_SOURCES[n], keywords, per_kw, days) for n in names]
        for f in futs:
            try:
                rows += f.result()
            except Exception:
                pass
    return rows


# --------------------------------------------------------------------------
# Layer 2: deep search links
# --------------------------------------------------------------------------
def deep_links(kw):
    q, qp, slug = quote(kw), quote_plus(kw), kw.lower().replace(" ", "-")
    return {
        "Job portals": {
            "LinkedIn (Internship + Entry)": f"https://www.linkedin.com/jobs/search/?keywords={qp}&location=India&f_E=1%2C2&f_TPR=r2592000",
            "Indeed India": f"https://in.indeed.com/jobs?q={qp}+fresher&l=India&sort=date",
            "Naukri": f"https://www.naukri.com/{slug}-fresher-jobs",
            "Internshala - Internships": f"https://internshala.com/internships/keywords-{slug}/",
            "Internshala - Jobs": f"https://internshala.com/jobs/keywords-{slug}/",
            "Unstop - Internships": f"https://unstop.com/internships?searchTerm={q}",
            "Unstop - Jobs": f"https://unstop.com/jobs?searchTerm={q}",
            "Wellfound (AngelList)": "https://wellfound.com/role/l/machine-learning-engineer/india",
            "Cutshort": f"https://cutshort.io/jobs/{slug}-jobs",
            "Instahyre": f"https://www.instahyre.com/search-jobs/?keywords={qp}",
            "Hirist": f"https://www.hirist.tech/k/{slug}-jobs",
            "FreshersWorld": f"https://www.freshersworld.com/jobs/jobsearch/{slug}-jobs",
        },
        "MNC / AI company careers": {
            "Google": f"https://www.google.com/about/careers/applications/jobs/results/?q={q}&location=India&target_level=INTERN_AND_APPRENTICE&target_level=EARLY",
            "Microsoft": f"https://jobs.careers.microsoft.com/global/en/search?q={q}&lc=India&exp=Students%20and%20graduates",
            "Amazon": f"https://www.amazon.jobs/en/search?base_query={qp}&loc_query=India&country=IND",
            "NVIDIA": f"https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite?q={q}",
            "Meta": "https://www.metacareers.com/jobs?roles[0]=Internship%20-%20Engineering%2C%20Tech%20%26%20Design",
            "Apple": f"https://jobs.apple.com/en-in/search?search={q}&location=india-INDC",
            "Adobe": f"https://careers.adobe.com/us/en/search-results?keywords={qp}&location=India",
            "Intel": f"https://jobs.intel.com/en/search-jobs/{q}/599/1",
            "IBM": f"https://www.ibm.com/careers/search?q={qp}&field_keyword_05[0]=India",
            "Salesforce": f"https://careers.salesforce.com/en/jobs/?search={qp}&country=India",
            "OpenAI": "https://openai.com/careers/search/",
            "Anthropic": "https://www.anthropic.com/jobs",
            "Databricks": "https://www.databricks.com/company/careers/open-positions",
            "Qualcomm": f"https://careers.qualcomm.com/careers?query={q}&location=India",
            "Samsung R&D": "https://research.samsung.com/careers",
            "Flipkart": "https://www.flipkartcareers.com/#!/joblist",
        },
    }


def extra_deep_links(kw):
    qp, slug = quote_plus(kw), kw.lower().replace(" ", "-")
    return {
        "Remote boards": {
            "Remotive": f"https://remotive.com/remote-jobs/search?query={qp}",
            "RemoteOK": f"https://remoteok.com/remote-{slug}-jobs",
            "Himalayas": f"https://himalayas.app/jobs?q={qp}",
            "We Work Remotely": f"https://weworkremotely.com/remote-jobs/search?term={qp}",
            "Arbeitnow": f"https://www.arbeitnow.com/?search={qp}",
            "HN Who is Hiring": "https://hn.algolia.com/?dateRange=pastMonth&query=%22who%20is%20hiring%22",
        },
        "More fresher platforms": {
            "Apna": f"https://apna.co/jobs?search={qp}",
            "Shine": f"https://www.shine.com/job-search/{slug}-jobs",
            "Foundit": f"https://www.foundit.in/srp/results?query={qp}",
            "Letsintern": "https://www.letsintern.com/internships",
            "Adzuna India": f"https://www.adzuna.in/search?q={qp}",
            "Jooble India": f"https://in.jooble.org/SearchResult?ukw={qp}",
            "YC Work at a Startup": "https://www.workatastartup.com/jobs",
        },
        "GitHub lists": {
            "SimplifyJobs New-Grad": "https://github.com/SimplifyJobs/New-Grad-Positions",
            "SimplifyJobs Internships 2027": "https://github.com/SimplifyJobs/Summer2027-Internships",
        },
    }


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------
def clean(df, fresher_only, role_only, location_filter, remote_only=False):
    if df.empty:
        return df
    df = df.dropna(subset=["Title", "Apply Link"]).copy()
    for col in ("Title", "Company", "Location", "Source", "Posted"):
        df[col] = df[col].fillna("").astype(str)
    df = df[df["Title"].str.strip() != ""]
    df = df.drop_duplicates(subset=["Apply Link"])
    df = df.drop_duplicates(subset=["Title", "Company"])
    if fresher_only:
        df = df[~df["Title"].str.contains(SENIOR_WORDS)]
    if role_only:
        df = df[df["Title"].str.contains(ROLE_WORDS)]
    if location_filter:
        pat = "|".join(re.escape(x.strip()) for x in location_filter.split(",") if x.strip())
        if pat:
            df = df[df["Location"].str.contains(pat, case=False, na=True)]
    if remote_only:
        df = df[df["Location"].str.contains("remote|anywhere|worldwide", case=False, na=False)]
    if df.empty:
        return df
    df["Fresher Match"] = df["Title"].apply(lambda t: "Yes" if FRESHER_WORDS.search(t) else "")
    df["Key"] = df["Apply Link"].apply(job_key)
    df = df.sort_values(["Fresher Match", "Posted"], ascending=[False, False])
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------
# Layer 3: PDF export (clickable links + applied status)
# --------------------------------------------------------------------------
def build_pdf(df, links_by_kw, meta, applied):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=10 * mm,
        rightMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title="AI/ML Jobs & Internships - 2027 Batch",
    )
    ss = getSampleStyleSheet()
    cell = ParagraphStyle("cell", parent=ss["BodyText"], fontSize=8, leading=10)
    link_style = ParagraphStyle("lnk", parent=cell, textColor=colors.HexColor("#1a56db"))
    esc = lambda s: (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    cut = lambda s, n: esc(_s(s)[:n] + ("..." if len(_s(s)) > n else ""))

    n_applied = int(df["Key"].isin(applied).sum()) if not df.empty else 0
    story = [
        Paragraph("AI / ML / Data Science / Python - Jobs &amp; Internships (2027 Batch)", ss["Title"]),
        Paragraph(
            f"Generated {datetime.now():%d %b %Y, %I:%M %p} | Keywords: {cut(meta['kw'], 200)} | "
            f"Location: {cut(meta['loc'], 60)} | Results: {len(df)} | Already applied: {n_applied}",
            cell,
        ),
        Spacer(1, 6),
    ]

    if not df.empty:
        story.append(Paragraph("Live results (click APPLY)", ss["Heading2"]))
        data = [["#", "Role", "Company", "Location", "Source", "Fresher?", "Status", "Apply"]]
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ]
        for i, r in df.reset_index(drop=True).iterrows():
            done = r["Key"] in applied
            url = esc(r["Apply Link"])
            status = (
                Paragraph(
                    f'<font color="#047857"><b>APPLIED</b></font><br/>{cut(applied[r["Key"]].get("applied_at", ""), 20)}',
                    cell,
                )
                if done
                else "Not yet"
            )
            data.append(
                [
                    i + 1,
                    Paragraph(cut(r["Title"], 110), cell),
                    Paragraph(cut(r["Company"], 45), cell),
                    Paragraph(cut(r["Location"], 60), cell),
                    Paragraph(cut(r["Source"], 30), cell),
                    r.get("Fresher Match", ""),
                    status,
                    Paragraph(f'<a href="{url}"><u>APPLY</u></a>', link_style),
                ]
            )
            style.append(
                (
                    "BACKGROUND",
                    (0, i + 1),
                    (-1, i + 1),
                    colors.HexColor("#d1fae5") if done else (colors.white if i % 2 == 0 else colors.HexColor("#f3f4f6")),
                )
            )
        t = Table(
            data,
            colWidths=[8 * mm, 78 * mm, 45 * mm, 40 * mm, 32 * mm, 16 * mm, 28 * mm, 18 * mm],
            repeatRows=1,
            splitByRow=1,
        )
        t.setStyle(TableStyle(style))
        story.append(t)

    story.append(Spacer(1, 10))
    story.append(Paragraph("Direct search links (portals &amp; company career pages)", ss["Heading2"]))
    for kw, groups in links_by_kw.items():
        story.append(Paragraph(f"<b>Keyword: {cut(kw, 80)}</b>", ss["Heading3"]))
        for group, items in groups.items():
            parts = [f'<a href="{esc(u)}" color="#1a56db"><u>{cut(n, 40)}</u></a>' for n, u in items.items()]
            story.append(Paragraph(f"<b>{esc(group)}:</b> " + " &nbsp;|&nbsp; ".join(parts), cell))
            story.append(Spacer(1, 3))
    doc.build(story)
    return buf.getvalue()


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
st.set_page_config(page_title="AI Job Finder - 2027 Batch", page_icon="🎯", layout="wide")
st.title("🎯 AI/ML Job & Internship Finder - CS Batch 2027")
st.caption(
    "Searches LinkedIn, Indeed, Naukri, Internshala, Amazon, AI companies and remote boards live, and gives "
    "search links for Unstop, Wellfound, Cutshort, Instahyre, Hirist, FreshersWorld, Apna, Shine, Foundit, "
    "Google, Microsoft, NVIDIA & more. Tick jobs you applied to and they stay marked ✅."
)

with st.sidebar:
    st.header("Search settings")
    kws = st.multiselect(
        "Keywords",
        DEFAULT_KEYWORDS + ["data analyst", "NLP", "computer vision", "MLOps", "deep learning", "LLM", "software engineer"],
        default=DEFAULT_KEYWORDS[:4],
    )
    extra = st.text_input("Add custom keywords (comma separated)")
    location = st.text_input("Location", "India")
    loc_filter = st.text_input("Only these cities (optional)", placeholder="Bangalore, Hyderabad, Remote")
    per_kw = st.slider("Results per keyword per site", 5, 40, 15)
    days = st.slider("Posted within (days)", 3, 90, 30)

    st.subheader("Live sources")
    use_li = st.checkbox("LinkedIn", True)
    use_in = st.checkbox("Indeed", True)
    use_nk = st.checkbox("Naukri", True)
    use_is = st.checkbox("Internshala", True)
    use_am = st.checkbox("Amazon Careers", True)
    use_gh = st.checkbox("AI company boards (Anthropic, Databricks, Scale...)", True)
    extra_on = st.multiselect(
        "Remote / extra sources",
        list(EXTRA_SOURCES),
        default=[n for n in EXTRA_SOURCES if "(key)" not in n],
        help="Sources marked (key) need API keys in secrets.toml.",
    )

    st.subheader("Filters")
    fresher_only = st.checkbox("Hide senior/lead/manager roles", True)
    role_only = st.checkbox("Only AI/ML/DS/Python-related titles", True)
    remote_only = st.checkbox("Remote roles only", False)
    hide_applied = st.checkbox("Hide applied jobs", False)

    st.subheader("Applied tracking")
    track_clicks = st.checkbox(
        "Auto-mark when I click Apply (local run only)",
        False,
        help="Uses a redirect server on 127.0.0.1, so it only works when you run the app on your own "
        "machine. Leave off on Streamlit Cloud and tick the Applied checkbox in the table instead.",
    )
    st.session_state["use_file"] = track_clicks
    applied_now = load_applied()
    st.metric("Total applied so far", len(applied_now))
    if applied_now and st.button("🗑️ Clear all applied history"):
        clear_applied()
        st.rerun()

keywords = kws + [k.strip() for k in extra.split(",") if k.strip()]

if track_clicks:
    start_tracker(TRACKER_PORT)

if st.button("🔍 Search jobs", type="primary", disabled=not keywords):
    load_keys()
    sites = [s for s, on in (("linkedin", use_li), ("indeed", use_in), ("naukri", use_nk)) if on]
    all_rows = []
    with st.status("Searching...", expanded=True) as status:
        if sites:
            st.write(f"LinkedIn / Indeed / Naukri: {', '.join(sites)}")
            all_rows += scrape_jobspy(keywords, location, per_kw, days * 24, sites)
        if use_is:
            st.write("Internshala")
            all_rows += scrape_internshala(keywords, per_kw)
        if use_am:
            st.write("Amazon Careers")
            all_rows += scrape_amazon(keywords, per_kw)
        if use_gh:
            st.write("AI company career boards")
            all_rows += scrape_greenhouse(keywords, GREENHOUSE_BOARDS)
        if extra_on:
            st.write(f"Remote / extra: {', '.join(extra_on)}")
            all_rows += run_extra_sources(extra_on, keywords, per_kw, days)
        status.update(label=f"Fetched {len(all_rows)} raw listings", state="complete")

    df = clean(pd.DataFrame(all_rows), fresher_only, role_only, loc_filter, remote_only)
    if not df.empty and track_clicks:
        register_jobs(df)
    st.session_state["df"] = df
    st.session_state["links"] = {k: {**deep_links(k), **extra_deep_links(k)} for k in keywords}
    st.session_state["meta"] = {"kw": ", ".join(keywords), "loc": loc_filter or location}
    st.session_state.pop("pdf_sig", None)
    st.session_state.pop("pdf_bytes", None)


def results_section(hide_applied: bool, track_clicks: bool):
    df = st.session_state["df"]
    links = st.session_state["links"]
    applied = load_applied()

    tab1, tab2 = st.tabs([f"📋 Live results ({len(df)})", "🔗 Portal & company search links"])

    with tab1:
        if df.empty:
            st.info("No live results - portals may have rate-limited you. Use the search links tab; they always work.")
        else:
            view = df.copy()
            view["Applied"] = view["Key"].isin(applied)
            n_done = int(view["Applied"].sum())

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Listings", len(view))
            c2.metric("✅ Applied", n_done)
            c3.metric("⏳ Not applied", len(view) - n_done)
            c4.metric("Fresher/intern titles", int((view["Fresher Match"] == "Yes").sum()))

            f1, f2 = st.columns([3, 1])
            src = f1.multiselect("Filter by source", sorted(view["Source"].unique()))
            only_f = f2.checkbox("Only fresher/intern-tagged")
            if src:
                view = view[view["Source"].isin(src)]
            if only_f:
                view = view[view["Fresher Match"] == "Yes"]
            if hide_applied:
                view = view[~view["Applied"]]

            view = view.reset_index(drop=True)
            view["Status"] = view["Applied"].map(lambda x: "✅ Applied" if x else "")
            view["Apply"] = (
                view["Key"].apply(lambda k: f"http://127.0.0.1:{TRACKER_PORT}/go?k={k}")
                if track_clicks
                else view["Apply Link"]
            )

            shown = view[["Applied", "Status", "Title", "Company", "Location", "Source",
                          "Fresher Match", "Posted", "Apply", "Key"]]
            editor_key = "editor_" + hashlib.md5("".join(shown["Key"]).encode()).hexdigest()[:8]
            edited = st.data_editor(
                shown,
                width="stretch",
                hide_index=True,
                disabled=[c for c in shown.columns if c != "Applied"],
                column_config={
                    "Applied": st.column_config.CheckboxColumn("Applied?", help="Tick to mark manually"),
                    "Apply": st.column_config.LinkColumn("Apply", display_text="Apply ↗"),
                    "Key": None,
                },
                key=editor_key,
            )
            changed = False
            for _, r in edited.iterrows():
                was = r["Key"] in applied
                if bool(r["Applied"]) != was:
                    src_row = df[df["Key"] == r["Key"]].iloc[0]
                    set_applied(
                        r["Key"],
                        {"link": src_row["Apply Link"], "title": src_row["Title"],
                         "company": src_row["Company"], "source": src_row["Source"]},
                        bool(r["Applied"]),
                    )
                    changed = True
            if changed:
                st.rerun(scope="fragment")

            if track_clicks:
                st.caption("Click **Apply ↗** once - the job turns ✅ Applied within a few seconds.")

    with tab2:
        for kw, groups in links.items():
            with st.expander(f"Keyword: {kw}", expanded=(kw == list(links)[0])):
                for g, items in groups.items():
                    st.markdown(f"**{g}**")
                    st.markdown(" · ".join(f"[{n}]({u})" for n, u in items.items()))

    st.divider()
    export_df = df[~df["Key"].isin(applied)] if hide_applied and not df.empty else df
    keys_sig = ",".join(export_df["Key"]) if not export_df.empty else ""
    sig = hashlib.md5(f"{keys_sig}|{','.join(sorted(applied))}|{len(links)}".encode()).hexdigest()
    if st.session_state.get("pdf_sig") != sig:
        try:
            st.session_state["pdf_bytes"] = build_pdf(export_df, links, st.session_state["meta"], applied)
        except Exception as e:
            st.session_state["pdf_bytes"] = None
            st.warning(f"PDF could not be built ({str(e)[:100]}). CSV download still works.")
        st.session_state["pdf_sig"] = sig

    d1, d2 = st.columns(2)
    if st.session_state.get("pdf_bytes"):
        d1.download_button(
            "⬇️ Download PDF (with Apply links + status)",
            st.session_state["pdf_bytes"],
            file_name=f"ai_jobs_2027_{datetime.now():%Y%m%d}.pdf",
            mime="application/pdf",
        )
    if not export_df.empty:
        csv_df = export_df.drop(columns=["Key"]).assign(
            Applied=export_df["Key"].isin(applied).map({True: "Yes", False: "No"})
        )
        d2.download_button(
            "⬇️ Download CSV",
            csv_df.to_csv(index=False).encode(),
            file_name="ai_jobs_2027.csv",
            mime="text/csv",
        )


if "df" in st.session_state:
    fragment = st.fragment(run_every=3 if track_clicks else None)(results_section)
    fragment(hide_applied, track_clicks)

done = load_applied()
with st.expander(f"✅ My applied jobs ({len(done)})"):
    if not done:
        st.write("Nothing yet.")
    else:
        st.dataframe(
            pd.DataFrame(done.values())[["title", "company", "source", "applied_at", "link"]].rename(columns=str.title),
            width="stretch",
            hide_index=True,
            column_config={"Link": st.column_config.LinkColumn("Link", display_text="Open ↗")},
        )

st.divider()
st.caption(
    "Job boards change layouts and rate-limit scrapers. If a source returns nothing, the direct "
    "search links still work. Always apply on the official page."
)