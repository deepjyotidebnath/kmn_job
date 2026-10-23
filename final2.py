# =====================================================================
# STEP 1: add to imports (top of file)
# =====================================================================
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor


# =====================================================================
# STEP 2: paste after GREENHOUSE_BOARDS / before "Layer 1" scrapers
# =====================================================================
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


def secret(name):
    try:
        return st.secrets.get(name, "")
    except Exception:
        return ""


def _date(v):
    try:
        if isinstance(v, (int, float)):
            v = v / 1000 if v > 1e11 else v
            return datetime.fromtimestamp(v).strftime("%Y-%m-%d")
    except Exception:
        pass
    return str(v or "")[:10]


def _row(title, company, loc, source, posted, link, kw):
    return {
        "Title": re.sub(r"<[^>]+>", "", str(title or "")),
        "Company": company or "",
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


# ---------------- Remote-first free APIs / RSS ----------------
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


# ---------------- Keyed APIs (put keys in .streamlit/secrets.toml) ----------------
def scrape_adzuna(keywords, per_kw, days=30):
    app_id, app_key = secret("ADZUNA_APP_ID"), secret("ADZUNA_APP_KEY")
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
    key = secret("JOOBLE_KEY")
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
    key = secret("RAPIDAPI_KEY")
    rows = []
    if not key:
        return rows
    for kw in keywords:
        try:
            r = requests.get(
                "https://jsearch.p.rapidapi.com/search",
                headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"},
                params={"query": f"{kw} fresher intern in India", "page": 1, "num_pages": 1, "date_posted": "month"},
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
    key = secret("SERPAPI_KEY")
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


# ---------------- ATS boards ----------------
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


def scrape_greenhouse_more(keywords, per_kw, days=30):
    return scrape_greenhouse(keywords, GREENHOUSE_BOARDS)


# ---------------- Community / GitHub lists ----------------
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


def extra_deep_links(kw):
    q, qp, slug = quote(kw), quote_plus(kw), kw.lower().replace(" ", "-")
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
            "Naukri Fresher": f"https://www.naukri.com/{slug}-fresher-jobs",
            "Letsintern": "https://www.letsintern.com/internships",
            "Adzuna India": f"https://www.adzuna.in/search?q={qp}",
            "Jooble India": f"https://in.jooble.org/SearchResult?ukw={qp}",
            "YC Work at a Startup": "https://www.workatastartup.com/jobs",
            "Wellfound": f"https://wellfound.com/role/l/{slug}/india",
        },
        "GitHub lists": {
            "SimplifyJobs New-Grad": "https://github.com/SimplifyJobs/New-Grad-Positions",
            "SimplifyJobs Internships 2027": "https://github.com/SimplifyJobs/Summer2027-Internships",
        },
    }


# =====================================================================
# STEP 3: sidebar (inside `with st.sidebar:`, after the "Live sources" checkboxes)
# =====================================================================
    extra_on = st.multiselect(
        "Remote / extra sources (API keys go in secrets.toml: ADZUNA_APP_ID, ADZUNA_APP_KEY, "
        "JOOBLE_KEY, RAPIDAPI_KEY, SERPAPI_KEY)",
        list(EXTRA_SOURCES),
        default=[n for n in EXTRA_SOURCES if "(key)" not in n],
    )
    remote_only = st.checkbox("Remote roles only", False)


# =====================================================================
# STEP 4: in the search block, after the `if use_gh:` block and before status.update(...)
# =====================================================================
        if extra_on:
            st.write(f"Remote / extra: {', '.join(extra_on)}")
            all_rows += run_extra_sources(extra_on, keywords, per_kw, days)


# =====================================================================
# STEP 5: replace the lines after `df = clean(...)` and the links assignment
# =====================================================================
    df = clean(pd.DataFrame(all_rows), fresher_only, role_only, loc_filter)
    if remote_only and not df.empty:
        df = df[df["Location"].str.contains("remote|anywhere|worldwide", case=False, na=False)].reset_index(drop=True)
    if not df.empty:
        register_jobs(df)
    st.session_state["df"] = df
    st.session_state["links"] = {k: {**deep_links(k), **extra_deep_links(k)} for k in keywords}
    st.session_state["meta"] = {"kw": ", ".join(keywords), "loc": loc_filter or location}