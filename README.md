# IIQ Test – Flask mockup

A Flask + SQLite mockup of potential multi business intake flow.

## Run it

```bash
python -m venv .venv                 # optional but tidy
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open http://localhost:5050  (change with `PORT=8000 python app.py`).

The first run creates `iiq.db` and loads the 20 leads from your production export.

| Command | What it does |
|---|---|
| `python app.py --reset` | Delete `iiq.db`, reload the exported production data, exit |
| **Seed Data** button | Wipe everything and load the 8-lead demo set from the original app |
| `USE_TAILWIND_CDN=1 python app.py` | Use Tailwind's CDN (needs internet) – use this if you add new CSS classes to templates |

## Layout

```
app.py              routes + all the logic (was worker/index.ts + the React event handlers)
schema.sql          final D1 schema
seed/exported_data.sql   leads + batches rows from d1_dump.sql
templates/          Jinja pages (Home / IntakePage / AnalysisPage became home / intake_* / analysis)
static/tailwind.css precompiled, so it works offline
```

## Flow

Pipeline → click a lead → **intake** (how many businesses, 1–4 → a form + simulated upload per business)
→ **analysis**. On the analysis page, "Edit Status" lets you set Income Analysis = Complete and a
business to Eligible/Ineligible, which fills in canned numbers (same as the original).

## Differences from the original

- Pages are server-rendered; the `/api/*` JSON endpoints are gone (nothing else consumed them).
- The pipeline search box now works (it was a no-op before).
- "Seed Data" was fixed: the original inserted into columns that don't exist in the real schema.
- Status dropdowns show a lead's actual value even when it isn't in the option list (e.g. `In Review`).
- Still intentionally dead, as in the original: Register Loan, the sidebar links, the Bank Statements /
  Business Narrative tabs, and the file-upload dropzone.
- Not carried over: `.env` (was empty), `users.json` (empty), `public_asset_links.json` (nothing
  in the code references those images), and Mocha's auth/email/analytics (the code never used them).
