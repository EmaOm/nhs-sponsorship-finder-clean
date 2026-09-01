from __future__ import annotations

import argparse
import hashlib
import html
import random
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlencode

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://www.jobs.nhs.uk"
SEARCH_URL = f"{BASE}/candidate/search/results"
DB_PATH = Path(__file__).parent / "data" / "jobs.db"

# Explicit negative wording beats boilerplate positive wording.
NEGATIVE_PATTERNS = [
    r"\bno sponsorship\b",
    r"\bnot eligible for (?:visa |skilled worker )?sponsorship\b",
    r"\bnot eligible for a certificate of sponsorship\b",
    r"\bunable to (?:offer|provide) (?:visa |skilled worker )?sponsorship\b",
    r"\bcannot (?:offer|provide) (?:visa |skilled worker )?sponsorship\b",
    r"\bcan'?t (?:offer|provide) (?:visa |skilled worker )?sponsorship\b",
    r"\bsponsorship (?:is|will be) not available\b",
    r"\bsponsorship cannot be offered\b",
    r"\bdoes not meet (?:the )?(?:ukvi )?(?:salary )?requirements? for sponsorship\b",
    r"\bdoes not meet the requirements for a certificate of sponsorship\b",
    r"\bthis (?:role|post|vacancy) (?:is )?not (?:eligible|suitable) for (?:skilled worker )?sponsorship\b",
    r"\bwe do not sponsor (?:this|these) (?:role|post|vacanc(?:y|ies))\b",
    r"\bmust already have (?:the )?(?:existing )?right to work in the uk\b",
    r"\bapplications requiring sponsorship (?:cannot|will not) be considered\b",
    r"\bwe are not able to sponsor\b",
    r"\bnot able to offer sponsorship\b",
    r"\bthis post does not qualify for sponsorship\b",
]

POSITIVE_PATTERNS = [
    r"applications from job seekers who require current skilled worker sponsorship to work in the uk are welcome",
    r"applications from candidates who require (?:current )?skilled worker sponsorship .*? are welcome",
    r"\bwe (?:can|are able to) (?:offer|provide) (?:visa |skilled worker )?sponsorship\b",
    r"\bsponsorship (?:is|may be) available\b",
    r"\beligible for (?:visa |skilled worker )?sponsorship\b",
    r"\bcertificate of sponsorship (?:is|may be) available\b",
    r"\bwill be considered for sponsorship\b",
]

# Contextual restrictions that should not automatically reject the advert but make it ambiguous.
CAUTION_PATTERNS = [
    r"salary threshold",
    r"immigration salary list",
    r"applicants must check their eligibility",
    r"may no longer be eligible for sponsorship",
    r"subject to (?:ukvi|home office) requirements",
    r"sponsorship .*? subject to eligibility",
]

SALARY_RE = re.compile(r"£\s?([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{4,6})(?:\.\d{1,2})?")
BAND_RE = re.compile(r"\bBand\s+(2|3|4|5|6|7|8A|8B|8C|8D|9)\b", re.I)
POSTCODE_RE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.I)
CLOSING_RE = re.compile(r"(?:closing date|closes?)\s*[:\-]?\s*([^\n|]{5,45})", re.I)


@dataclass
class Listing:
    job_id: str
    url: str
    title: str = ""
    employer: str = ""
    location: str = ""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalise_text(text: str) -> str:
    text = html.unescape(text or "")
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def compact(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def build_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10))
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36 NHS-Sponsorship-Finder/1.0",
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Cache-Control": "no-cache",
    })
    return s


def init_db(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            title TEXT,
            employer TEXT,
            location TEXT,
            salary_text TEXT,
            salary_min REAL,
            salary_max REAL,
            band TEXT,
            closing_date_text TEXT,
            working_pattern TEXT,
            contract_type TEXT,
            staff_group TEXT,
            status TEXT NOT NULL,
            confidence TEXT NOT NULL,
            positive_evidence TEXT,
            negative_evidence TEXT,
            caution_evidence TEXT,
            advert_hash TEXT,
            full_text TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            last_checked TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_active ON jobs(active)")
    con.commit()
    return con


def fetch(session: requests.Session, url: str, params: dict | None = None) -> str:
    response = session.get(url, params=params, timeout=35)
    if response.status_code == 403:
        raise RuntimeError(
            "NHS Jobs returned HTTP 403. The site may be rate-limiting this host. "
            "Try again later or run from a normal home/GitHub Actions IP."
        )
    response.raise_for_status()
    return response.text


def listing_links_from_page(page_html: str) -> list[Listing]:
    soup = BeautifulSoup(page_html, "html.parser")
    found: dict[str, Listing] = {}
    for a in soup.select('a[href*="/candidate/jobadvert/"]'):
        href = a.get("href", "")
        m = re.search(r"/candidate/jobadvert/([^/?#]+)", href)
        if not m:
            continue
        job_id = m.group(1)
        title = compact(a.get_text(" ", strip=True))
        # Walk up to a likely result card to capture employer/location when available.
        card = a.find_parent(["li", "article", "div"])
        card_text = compact(card.get_text(" | ", strip=True)) if card else ""
        found[job_id] = Listing(job_id, urljoin(BASE, href), title=title, location=card_text)
    return list(found.values())


def discover_all_listings(session: requests.Session, max_pages: int | None = None) -> list[Listing]:
    all_jobs: dict[str, Listing] = {}
    page = 1
    while True:
        params = {"language": "en", "page": page}
        raw = fetch(session, SEARCH_URL, params=params)
        page_jobs = listing_links_from_page(raw)
        new_count = 0
        for item in page_jobs:
            if item.job_id not in all_jobs:
                all_jobs[item.job_id] = item
                new_count += 1
        print(f"Search page {page}: {len(page_jobs)} links, {new_count} new")

        # NHS Jobs returns no new advert links after the final results page.
        if not page_jobs or new_count == 0:
            break
        if max_pages and page >= max_pages:
            break
        page += 1
        time.sleep(random.uniform(0.4, 0.9))

    return list(all_jobs.values())


def evidence_windows(text: str, patterns: Iterable[str], radius: int = 190) -> list[str]:
    low = text.lower()
    snippets: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, low, re.I | re.S):
            start = max(0, match.start() - radius)
            end = min(len(text), match.end() + radius)
            snippet = compact(text[start:end])
            if snippet and snippet not in snippets:
                snippets.append(snippet)
    return snippets[:8]


def classify(text: str) -> tuple[str, str, list[str], list[str], list[str]]:
    negatives = evidence_windows(text, NEGATIVE_PATTERNS)
    positives = evidence_windows(text, POSITIVE_PATTERNS)
    cautions = evidence_windows(text, CAUTION_PATTERNS)

    # Explicit role-specific exclusion always wins.
    if negatives:
        return "NO_SPONSORSHIP", "HIGH", positives, negatives, cautions
    if positives and cautions:
        # Common NHS boilerplate can say both "welcome" and "check eligibility".
        # Keep this out of Confirmed unless no eligibility caveat exists.
        return "AMBIGUOUS", "MEDIUM", positives, negatives, cautions
    if positives:
        return "CONFIRMED", "HIGH", positives, negatives, cautions
    if cautions:
        return "AMBIGUOUS", "LOW", positives, negatives, cautions
    return "NO_EVIDENCE", "LOW", positives, negatives, cautions


def text_after_label(text: str, label: str) -> str:
    m = re.search(rf"{re.escape(label)}\s*[:\-]?\s*([^\n]+)", text, re.I)
    return compact(m.group(1)) if m else ""


def parse_advert(raw_html: str, url: str, fallback: Listing) -> dict:
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = normalise_text(soup.get_text("\n", strip=True))

    h1 = soup.find("h1")
    title = compact(h1.get_text(" ", strip=True)) if h1 else fallback.title
    if not title or title.lower() == "job advert":
        # NHS pages often expose the actual title in <title> or an early h2.
        page_title = compact(soup.title.get_text(" ", strip=True)) if soup.title else ""
        title = re.sub(r"\s*-\s*NHS Jobs.*$", "", page_title, flags=re.I) or fallback.title

    employer = text_after_label(text, "Employer")
    location = text_after_label(text, "Location")
    salary_text = text_after_label(text, "Salary")
    working_pattern = text_after_label(text, "Working pattern")
    contract_type = text_after_label(text, "Contract") or text_after_label(text, "Contract type")
    staff_group = text_after_label(text, "Staff group")

    if not location:
        pc = POSTCODE_RE.search(text[:5000])
        location = pc.group(0).upper() if pc else ""

    salaries = [float(x.replace(",", "")) for x in SALARY_RE.findall(salary_text or text[:6000])]
    salary_min = min(salaries) if salaries else None
    salary_max = max(salaries) if salaries else None
    band_match = BAND_RE.search(text[:6000])
    band = f"Band {band_match.group(1).upper()}" if band_match else ""
    closing = CLOSING_RE.search(text[:8000])
    closing_date_text = compact(closing.group(1)) if closing else text_after_label(text, "Closing date")

    status, confidence, positives, negatives, cautions = classify(text)
    return {
        "job_id": fallback.job_id,
        "url": url,
        "title": title,
        "employer": employer,
        "location": location,
        "salary_text": salary_text,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "band": band,
        "closing_date_text": closing_date_text,
        "working_pattern": working_pattern,
        "contract_type": contract_type,
        "staff_group": staff_group,
        "status": status,
        "confidence": confidence,
        "positive_evidence": "\n---\n".join(positives),
        "negative_evidence": "\n---\n".join(negatives),
        "caution_evidence": "\n---\n".join(cautions),
        "advert_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "full_text": text,
    }


def upsert(con: sqlite3.Connection, job: dict, seen_at: str) -> None:
    existing = con.execute("SELECT first_seen FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
    first_seen = existing[0] if existing else seen_at
    fields = [
        "job_id", "url", "title", "employer", "location", "salary_text", "salary_min", "salary_max",
        "band", "closing_date_text", "working_pattern", "contract_type", "staff_group", "status", "confidence",
        "positive_evidence", "negative_evidence", "caution_evidence", "advert_hash", "full_text"
    ]
    values = [job.get(f) for f in fields]
    con.execute(
        f"""
        INSERT INTO jobs ({','.join(fields)}, first_seen, last_seen, last_checked, active)
        VALUES ({','.join(['?'] * len(fields))}, ?, ?, ?, 1)
        ON CONFLICT(job_id) DO UPDATE SET
        {','.join(f'{f}=excluded.{f}' for f in fields if f != 'job_id')},
        last_seen=excluded.last_seen,
        last_checked=excluded.last_checked,
        active=1
        """,
        values + [first_seen, seen_at, seen_at],
    )


def scan(max_pages: int | None = None, force_all: bool = False, delay: float = 0.7) -> None:
    session = build_session()
    con = init_db()
    seen_at = now_iso()
    listings = discover_all_listings(session, max_pages=max_pages)
    ids = {x.job_id for x in listings}
    print(f"Discovered {len(listings)} unique live job adverts")

    # Mark missing jobs inactive only on a full unbounded scan.
    if max_pages is None:
        con.execute("UPDATE jobs SET active=0")
        con.commit()

    for i, listing in enumerate(listings, 1):
        old = con.execute(
            "SELECT last_checked, status FROM jobs WHERE job_id=?", (listing.job_id,)
        ).fetchone()
        should_fetch = force_all or old is None

        # Existing adverts are rechecked every 7 days, while every daily run discovers new jobs.
        if old and not force_all:
            try:
                last = datetime.fromisoformat(old[0].replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - last).total_seconds() / 86400
                should_fetch = age_days >= 7
            except Exception:
                should_fetch = True

        if not should_fetch:
            con.execute("UPDATE jobs SET active=1, last_seen=? WHERE job_id=?", (seen_at, listing.job_id))
            continue

        try:
            raw = fetch(session, listing.url)
            parsed = parse_advert(raw, listing.url, listing)
            upsert(con, parsed, seen_at)
            print(f"[{i}/{len(listings)}] {parsed['status']}: {parsed['title'][:80]}")
        except Exception as exc:
            print(f"[{i}/{len(listings)}] ERROR {listing.job_id}: {exc}")
        con.commit()
        time.sleep(max(delay, 0) + random.uniform(0.1, 0.45))

    if max_pages is None:
        # Reactivate all IDs positively observed in this scan. (Parameterized chunks avoid SQLite limits.)
        for start in range(0, len(ids), 500):
            chunk = list(ids)[start:start+500]
            placeholders = ",".join("?" for _ in chunk)
            if chunk:
                con.execute(f"UPDATE jobs SET active=1, last_seen=? WHERE job_id IN ({placeholders})", [seen_at] + chunk)
        con.commit()

    counts = dict(con.execute("SELECT status, COUNT(*) FROM jobs WHERE active=1 GROUP BY status").fetchall())
    print("Active result counts:", counts)
    con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan NHS Jobs and classify sponsorship wording.")
    parser.add_argument("--max-pages", type=int, default=None, help="For testing only: limit result pages")
    parser.add_argument("--force-all", action="store_true", help="Re-download every advert")
    parser.add_argument("--delay", type=float, default=0.7, help="Delay between advert requests")
    args = parser.parse_args()
    scan(max_pages=args.max_pages, force_all=args.force_all, delay=args.delay)


if __name__ == "__main__":
    main()
