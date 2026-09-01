# NHS Sponsorship Job Finder

A conservative NHS Jobs scanner that:

- discovers **all** live NHS Jobs adverts before filtering by role;
- opens individual adverts and scans the full visible advert text;
- gives explicit negative sponsorship wording priority over generic positive wording;
- puts conditional/eligibility caveats into **Manual review** instead of pretending sponsorship is confirmed;
- stores an audit trail in SQLite;
- provides a Streamlit dashboard with keyword, location, employer, band, salary, working-pattern, contract and staff-group filters;
- includes a GitHub Actions workflow for a daily scan.

## Run locally

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
# source .venv/bin/activate

pip install -r requirements.txt
python scanner.py --max-pages 2   # small test first
python scanner.py                 # full scan
streamlit run app.py
```

Then open the local Streamlit address shown in the terminal (normally `http://localhost:8501`).

## Put it online

1. Create a private GitHub repository and upload this folder.
2. Run the `Daily NHS sponsorship scan` workflow once manually from GitHub Actions. This creates/populates `data/jobs.db`.
3. In Streamlit Community Cloud, create an app from that repository and choose `app.py`.
4. Bookmark the Streamlit URL on your phone/computer.
5. Keep GitHub Actions enabled. The included workflow runs every day and commits the refreshed database back to the repository.

## Classification rule

`CONFIRMED` is deliberately strict:

- there must be explicit positive sponsorship wording;
- there must be **no explicit negative sponsorship restriction** anywhere in the advert;
- there must be **no conditional eligibility caveat** detected by the current rules.

A line such as "applications requiring Skilled Worker sponsorship are welcome" does not win if another part of the same advert says this vacancy cannot be sponsored.

`AMBIGUOUS` is shown only when the dashboard's manual-review option is enabled.

## Important limitations

This is an advert-text classifier, not an immigration-law eligibility decision. A vacancy can say sponsorship is available while an individual applicant still fails a salary, occupation-code, qualification, registration or other Home Office requirement. Always open the live advert before applying.

The NHS Jobs HTML can change. The scanner intentionally uses broad selectors (`/candidate/jobadvert/`) and text-based metadata extraction to reduce fragility, but selectors/patterns may occasionally need updating.

The scanner is polite/incremental: every daily run discovers the live job list, downloads new adverts, and rechecks existing adverts periodically rather than downloading every full advert every day.
