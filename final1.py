"""
AI/ML Job & Internship Finder for CS Graduates (2027 batch)  -  v2
-------------------------------------------------------------------
Run:  streamlit run app.py

NEW in v2: APPLIED TRACKING
 * Click "Apply" once  -> the job is automatically marked  ✅ Applied
   (links go through a tiny local redirect server on 127.0.0.1:8765 that records the
    click and forwards you to the real job page).
 * You can also tick / untick the "Applied" checkbox manually.
 * Applied jobs are saved in applied_jobs.json, so they stay marked on every future
   search and after restarting the app. Use "Hide applied jobs" to remove them.
 * PDF / CSV exports include the Applied status.

Note: the click-tracking redirect works when you run the app on YOUR computer.
On Streamlit Cloud / a server, turn it off in the sidebar and use the checkbox.
"""
import hashlib
import io
import json
import re
import threading
import time
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
    r"\b(?:senior|sr\.?|lead|principal|staff|manager|head|director|architect|vp|"
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
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def load_applied() -> dict:
    with LOCK:
        return _load(APPLIED_FILE)


def set_applied(key: str, info: dict, applied: bool):
    with LOCK:
        data = _load(APPLIED_FILE)
        if applied:
            data.setdefault(key, {**info, "applied_at": datetime.now().strftime("%d %b %Y %H:%M")})
        else:
            data.pop(key, None)
        _save(APPLIED_FILE, data)


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
        set_applied(key, job, True)
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
        return True  # already running (another session / earlier run)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return True


# --------------------------------------------------------------------------
# Layer 1: live scrapers
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
                        {
                            "Title": r.get("title"),
                            "Company": r.get("company"),
                            "Location": r.get("location"),
                            "Source": str(r.get("site", site)).capitalize(),
                            "Posted": str(r.get("date_posted") or ""),
                            "Apply Link": r.get("job_url_direct") or r.get("job_url"),
                            "Keyword": kw,
                        }
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
                        {
                            "Title": a.get_text(strip=True) if a else kw.title(),
                            "Company": comp.get_text(strip=True) if comp else "",
                            "Location": loc.get_text(" ", strip=True) if loc else "",
                            "Source": f"Internshala ({kind})",
                            "Posted": "",
                            "Apply Link": "https://internshala.com" + href
                            if href.startswith("/")
                            else href,
                            "Keyword": kw,
                        }
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
                    {
                        "Title": j.get("title"),
                        "Company": "Amazon",
                        "Location": j.get("normalized_location") or j.get("location"),
                        "Source": "Amazon Careers",
                        "Posted": j.get("posted_date", ""),
                        "Apply Link": "https://www.amazon.jobs" + j.get("job_path", ""),
                        "Keyword": kw,
                    }
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
                        {
                            "Title": title,
                            "Company": name,
                            "Location": loc,
                            "Source": "Company Careers",
                            "Posted": (j.get("updated_at") or "")[:10],
                            "Apply Link": j.get("absolute_url"),
                            "Keyword": "AI company",
                        }
                    )
        except Exception:
            continue
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


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------
def clean(df, fresher_only, role_only, location_filter):
    if df.empty:
        return df
    df = df.dropna(subset=["Title", "Apply Link"]).copy()
    for col in ("Title", "Company", "Location", "Source", "Posted"):
        df[col] = df[col].fillna("").astype(str)
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

    n_applied = int(df["Key"].isin(applied).sum()) if not df.empty else 0
    story = [
        Paragraph("AI / ML / Data Science / Python - Jobs &amp; Internships (2027 Batch)", ss["Title"]),
        Paragraph(
            f"Generated {datetime.now():%d %b %Y, %I:%M %p} | Keywords: {esc(meta['kw'])} | "
            f"Location: {esc(meta['loc'])} | Results: {len(df)} | Already applied: {n_applied}",
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
        for i, r in df.iterrows():
            done = r["Key"] in applied
            url = esc(r["Apply Link"])
            status = (
                Paragraph(f'<font color="#047857"><b>APPLIED</b></font><br/>{esc(applied[r["Key"]].get("applied_at", ""))}', cell)
                if done
                else "Not yet"
            )
            data.append(
                [
                    i + 1,
                    Paragraph(esc(r["Title"]), cell),
                    Paragraph(esc(r["Company"]), cell),
                    Paragraph(esc(r["Location"]), cell),
                    Paragraph(esc(r["Source"]), cell),
                    r.get("Fresher Match", ""),
                    status,
                    Paragraph(f'<a href="{url}"><u>APPLY</u></a>', link_style),
                ]
            )
            style.append(
                ("BACKGROUND", (0, i + 1), (-1, i + 1),
                 colors.HexColor("#d1fae5") if done else (colors.white if i % 2 == 0 else colors.HexColor("#f3f4f6")))
            )
        t = Table(
            data,
            colWidths=[8 * mm, 78 * mm, 45 * mm, 40 * mm, 32 * mm, 16 * mm, 28 * mm, 18 * mm],
            repeatRows=1,
        )
        t.setStyle(TableStyle(style))
        story.append(t)

    story.append(Spacer(1, 10))
    story.append(Paragraph("Direct search links (portals &amp; company career pages)", ss["Heading2"]))
    for kw, groups in links_by_kw.items():
        story.append(Paragraph(f"<b>Keyword: {esc(kw)}</b>", ss["Heading3"]))
        for group, items in groups.items():
            parts = [f'<a href="{esc(u)}" color="#1a56db"><u>{esc(n)}</u></a>' for n, u in items.items()]
            story.append(Paragraph(f"<b>{group}:</b> " + " &nbsp;|&nbsp; ".join(parts), cell))
            story.append(Spacer(1, 3))
    doc.build(story)
    return buf.getvalue()


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
st.set_page_config(page_title="AI Job Finder - 2027 Batch", page_icon="🎯", layout="wide")
st.title("🎯 AI/ML Job & Internship Finder - CS Batch 2027")
st.caption(
    "Searches LinkedIn, Indeed, Naukri, Internshala, Amazon + AI companies live, and gives search "
    "links for Unstop, Wellfound, Cutshort, Instahyre, Hirist, FreshersWorld, Google, Microsoft, "
    "NVIDIA & more. Click Apply once and the job is remembered as ✅ Applied."
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

    st.subheader("Filters")
    fresher_only = st.checkbox("Hide senior/lead/manager roles", True)
    role_only = st.checkbox("Only AI/ML/DS/Python-related titles", True)
    hide_applied = st.checkbox("Hide applied jobs", False)

    st.subheader("Applied tracking")
    track_clicks = st.checkbox(
        "Auto-mark when I click Apply (local only)",
        True,
        help="Uses a tiny redirect server on 127.0.0.1. Turn off on Streamlit Cloud and use the checkbox instead.",
    )
    applied_now = load_applied()
    st.metric("Total applied so far", len(applied_now))
    if applied_now and st.button("🗑️ Clear all applied history"):
        _save(APPLIED_FILE, {})
        st.rerun()

keywords = kws + [k.strip() for k in extra.split(",") if k.strip()]

if track_clicks:
    start_tracker(TRACKER_PORT)

if st.button("🔍 Search jobs", type="primary", disabled=not keywords):
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
        status.update(label=f"Fetched {len(all_rows)} raw listings", state="complete")

    df = clean(pd.DataFrame(all_rows), fresher_only, role_only, loc_filter)
    if not df.empty:
        register_jobs(df)
    st.session_state["df"] = df
    st.session_state["links"] = {k: deep_links(k) for k in keywords}
    st.session_state["meta"] = {"kw": ", ".join(keywords), "loc": loc_filter or location}


@st.fragment(run_every=3)  # re-reads applied_jobs.json every 3s so clicks show up automatically
def results_section(hide_applied: bool, track_clicks: bool):
    df = st.session_state["df"]
    links = st.session_state["links"]
    applied = load_applied()

    tab1, tab2 = st.tabs([f"📋 Live results ({len(df)})", "🔗 Portal & company search links"])

    with tab1:
        if df.empty:
            st.info("No live results - portals may have rate-limited you. Use the search links tab; they always work.")
            view = df
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
            edited = st.data_editor(
                shown,
                use_container_width=True,
                hide_index=True,
                disabled=[c for c in shown.columns if c != "Applied"],
                column_config={
                    "Applied": st.column_config.CheckboxColumn("Applied?", help="Tick to mark manually"),
                    "Apply": st.column_config.LinkColumn("Apply", display_text="Apply ↗"),
                    "Key": None,
                },
                key="editor",
            )
            # manual tick / untick -> persist
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
                st.caption("Click **Apply ↗** once - the job turns ✅ Applied within a few seconds "
                           "(and stays that way next time you search).")

    with tab2:
        for kw, groups in links.items():
            with st.expander(f"Keyword: {kw}", expanded=(kw == list(links)[0])):
                for g, items in groups.items():
                    st.markdown(f"**{g}**")
                    st.markdown(" · ".join(f"[{n}]({u})" for n, u in items.items()))

    st.divider()
    export_df = df[~df["Key"].isin(applied)] if hide_applied and not df.empty else df
    pdf_bytes = build_pdf(export_df, links, st.session_state["meta"], applied)
    d1, d2 = st.columns(2)
    d1.download_button(
        "⬇️ Download PDF (with Apply links + status)",
        pdf_bytes,
        file_name=f"ai_jobs_2027_{datetime.now():%Y%m%d}.pdf",
        mime="application/pdf",
    )
    if not export_df.empty:
        csv_df = export_df.drop(columns=["Key"]).assign(
            Applied=export_df["Key"].isin(applied).map({True: "Yes", False: "No"})
        )
        d2.download_button("⬇️ Download CSV", csv_df.to_csv(index=False).encode(),
                           file_name="ai_jobs_2027.csv", mime="text/csv")


if "df" in st.session_state:
    results_section(hide_applied, track_clicks)

with st.expander(f"✅ My applied jobs ({len(load_applied())})"):
    done = load_applied()
    if not done:
        st.write("Nothing yet.")
    else:
        st.dataframe(
            pd.DataFrame(done.values())[["title", "company", "source", "applied_at", "link"]]
            .rename(columns=str.title),
            use_container_width=True,
            hide_index=True,
            column_config={"Link": st.column_config.LinkColumn("Link", display_text="Open ↗")},
        )

st.divider()
st.caption("Job boards change layouts and rate-limit scrapers. If a source returns nothing, the direct "
           "search links still work. Always apply on the official page.")