from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

DB_PATH = Path(__file__).parent / "data" / "jobs.db"

st.set_page_config(page_title="NHS Sponsorship Job Finder", page_icon="🔎", layout="wide")
st.title("NHS Sponsorship Job Finder")
st.caption("All NHS Jobs vacancies are scanned first. Filters are applied only after sponsorship checking.")

if not DB_PATH.exists():
    st.info("The database is empty. Run `python scanner.py` once, then refresh this page.")
    st.stop()

con = sqlite3.connect(DB_PATH)
df = pd.read_sql_query(
    """
    SELECT job_id, title, employer, location, salary_text, salary_min, salary_max, band,
           closing_date_text, working_pattern, contract_type, staff_group, status, confidence,
           positive_evidence, negative_evidence, caution_evidence, url, first_seen, last_seen, last_checked
    FROM jobs
    WHERE active=1
    ORDER BY first_seen DESC
    """,
    con,
)
con.close()

if df.empty:
    st.warning("No scanned jobs are currently stored.")
    st.stop()

# Main user view is conservative: only explicit positive sponsorship wording with no negative/caution contradiction.
confirmed = df[df["status"] == "CONFIRMED"].copy()

with st.sidebar:
    st.header("Filters")
    keyword = st.text_input("Keyword / job title")
    location = st.text_input("Location")
    employer = st.text_input("Employer")
    bands = sorted(x for x in confirmed["band"].dropna().unique() if x)
    selected_bands = st.multiselect("Band", bands)
    min_salary = st.number_input("Minimum annual salary (£)", min_value=0, value=0, step=1000)
    patterns = sorted(x for x in confirmed["working_pattern"].dropna().unique() if x)
    selected_patterns = st.multiselect("Working pattern", patterns)
    contracts = sorted(x for x in confirmed["contract_type"].dropna().unique() if x)
    selected_contracts = st.multiselect("Contract type", contracts)
    groups = sorted(x for x in confirmed["staff_group"].dropna().unique() if x)
    selected_groups = st.multiselect("Staff group", groups)
    show_review = st.checkbox("Show ambiguous/manual-review jobs", value=False)

view = df[df["status"].isin(["CONFIRMED", "AMBIGUOUS"] if show_review else ["CONFIRMED"])].copy()

if keyword:
    q = keyword.lower()
    view = view[view.apply(lambda r: q in " ".join(str(r.get(c, "")) for c in ["title", "employer", "location", "staff_group"]).lower(), axis=1)]
if location:
    view = view[view["location"].fillna("").str.contains(location, case=False, regex=False)]
if employer:
    view = view[view["employer"].fillna("").str.contains(employer, case=False, regex=False)]
if selected_bands:
    view = view[view["band"].isin(selected_bands)]
if min_salary:
    view = view[(view["salary_max"].fillna(0) >= min_salary) | (view["salary_min"].fillna(0) >= min_salary)]
if selected_patterns:
    view = view[view["working_pattern"].isin(selected_patterns)]
if selected_contracts:
    view = view[view["contract_type"].isin(selected_contracts)]
if selected_groups:
    view = view[view["staff_group"].isin(selected_groups)]

c1, c2, c3 = st.columns(3)
c1.metric("Confirmed sponsorship", int((df["status"] == "CONFIRMED").sum()))
c2.metric("Manual review", int((df["status"] == "AMBIGUOUS").sum()))
c3.metric("Matching filters", len(view))

st.caption("Confirmed means the advert contains explicit positive sponsorship wording and the scanner found no explicit refusal or eligibility caveat. Always verify the live advert before applying because adverts and immigration rules can change.")

for _, row in view.iterrows():
    badge = "✅ Confirmed" if row["status"] == "CONFIRMED" else "⚠️ Manual review"
    with st.container(border=True):
        st.subheader(row["title"] or "Untitled vacancy")
        cols = st.columns([2, 2, 1, 1])
        cols[0].write(f"**Employer:** {row['employer'] or 'Not extracted'}")
        cols[1].write(f"**Location:** {row['location'] or 'Not extracted'}")
        cols[2].write(f"**Band:** {row['band'] or '—'}")
        cols[3].write(f"**Status:** {badge}")
        st.write(f"**Salary:** {row['salary_text'] or 'Not extracted'}")
        if row["closing_date_text"]:
            st.write(f"**Closing:** {row['closing_date_text']}")
        if row["working_pattern"]:
            st.write(f"**Working pattern:** {row['working_pattern']}")
        st.link_button("Open NHS advert", row["url"])
        with st.expander("Why the scanner classified this advert"):
            if row["positive_evidence"]:
                st.write("**Positive sponsorship evidence**")
                st.code(row["positive_evidence"], language=None)
            if row["negative_evidence"]:
                st.write("**Negative evidence**")
                st.code(row["negative_evidence"], language=None)
            if row["caution_evidence"]:
                st.write("**Eligibility/caution wording**")
                st.code(row["caution_evidence"], language=None)

st.divider()
with st.expander("Audit view: excluded jobs"):
    excluded = df[df["status"].isin(["NO_SPONSORSHIP", "NO_EVIDENCE"])][
        ["title", "employer", "status", "negative_evidence", "url"]
    ]
    st.dataframe(excluded, use_container_width=True, hide_index=True)
