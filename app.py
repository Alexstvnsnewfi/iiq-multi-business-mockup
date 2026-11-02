#!/usr/bin/env python3
"""
IIQ Test (NewfiBLU - Broker Loan Utility / IncomeIQ bank statement analysis)

A Flask + SQLite + Jinja port of the Mocha export (React + Hono + Cloudflare D1).
Pages are server-rendered, so there is no npm, no build step and no API layer.

    pip install -r requirements.txt
    python app.py                 # http://localhost:5050
    python app.py --reset         # wipe iiq.db, reload the exported production data, then exit
"""
import math
import os
import sqlite3
import sys
import time
from pathlib import Path

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   url_for)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("IIQ_DB", BASE_DIR / "iiq.db"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-mockup-key")
# Set USE_TAILWIND_CDN=1 to load Tailwind from its CDN instead of static/tailwind.css.
# Handy if you start editing templates and adding new utility classes.
app.config["USE_TAILWIND_CDN"] = os.environ.get("USE_TAILWIND_CDN") == "1"

DECIDED = ("ELIGIBLE", "INELIGIBLE")
PER_PAGE_CHOICES = (10, 25, 50)


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def q_all(sql, args=()):
    return get_db().execute(sql, args).fetchall()


def q_one(sql, args=()):
    return get_db().execute(sql, args).fetchone()


def run(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur


def init_db(reset=False):
    """Create iiq.db from schema.sql + the exported production rows (first run only)."""
    if reset and DB_PATH.exists():
        DB_PATH.unlink()
    if DB_PATH.exists():
        return
    con = sqlite3.connect(DB_PATH)
    con.executescript((BASE_DIR / "schema.sql").read_text())
    con.executescript((BASE_DIR / "seed" / "exported_data.sql").read_text())
    con.commit()
    con.close()


# --------------------------------------------------------------------------- #
# Template helpers
# --------------------------------------------------------------------------- #
@app.template_filter("num")
def num_filter(value, commas=False):
    """71250.0 -> '71,250', 5.0 -> '5', 2.5 -> '2.5'. None -> ''."""
    if value is None:
        return ""
    text = f"{value:,.2f}" if commas else f"{value:.2f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def lead_is_reviewable(lead):
    """Where a lead's number link goes: analysis page (True) or intake (False)."""
    s, e = lead["income_analysis_status"], lead["eligibility_status"]
    return bool(
        lead["has_any_analysis"]
        or s in ("Review Analysis", "In Review")
        or (s == "COMPLETE" and e in DECIDED)
    )


def income_status_is_link(lead):
    s, e = lead["income_analysis_status"], lead["eligibility_status"]
    return s in ("Review Analysis", "In Review") or (s == "COMPLETE" and e in DECIDED)


def page_numbers(current, total):
    """Pagination window, same rules as the React version."""
    if total <= 7:
        return list(range(1, total + 1))
    if current <= 3:
        return [1, 2, 3, "...", total]
    if current >= total - 2:
        return [1, "...", total - 2, total - 1, total]
    return [1, "...", current - 1, current, current + 1, "...", total]


app.jinja_env.globals.update(
    lead_is_reviewable=lead_is_reviewable,
    income_status_is_link=income_status_is_link,
)


# --------------------------------------------------------------------------- #
# Data helpers
# --------------------------------------------------------------------------- #
def get_lead_or_404(lead_number):
    lead = q_one("SELECT * FROM leads WHERE lead_number = ?", (lead_number,))
    if lead is None:
        abort(404, description=f"Lead {lead_number} not found")
    return lead


def create_batches(lead, count):
    """BATCH-<lead suffix>-<NNN>, numbered after any batches the lead already has."""
    existing = q_one("SELECT COUNT(*) AS n FROM batches WHERE lead_id = ?", (lead["id"],))["n"]
    suffix = lead["lead_number"].replace("WTEST", "")
    ids = []
    for i in range(count):
        batch_id = f"BATCH-{suffix}-{existing + i + 1:03d}"
        run("INSERT INTO batches (batch_id, lead_id) VALUES (?, ?)", (batch_id, lead["id"]))
        ids.append(batch_id)
    return ids


def populate_dummy_data(batch_id, eligibility_status):
    """Fills in canned analysis numbers (mirrors /populate-dummy-data in the worker)."""
    eligible = eligibility_status == "ELIGIBLE"
    deposit_qual = 8500 if eligible else 4200
    large_dep = 5000 if eligible else 3000
    changes = 5 if eligible else 15
    run(
        """UPDATE batches SET
             income_period = '12 months', expense_ratio = '50%', ownership_percentage = '100%',
             start_period = '01/2023', end_period = '12/2023',
             analysis_findings = 'Analysis completed based on 12 months of bank statements. Business shows consistent deposit patterns with moderate expenses. NSF activity is within acceptable limits.',
             nsf_qualification = 'OK',
             large_deposit_threshold = ?, changes_recent_6_month = ?,
             year_over_year_comparison = 'Stable',
             deposit_qualification = ?, gross_deposit = ?, total_net_deposit = ?,
             eligibility_status = ?, has_analysis_complete = 1,
             updated_at = CURRENT_TIMESTAMP
           WHERE batch_id = ?""",
        (large_dep, changes, deposit_qual, deposit_qual * 2, deposit_qual,
         eligibility_status, batch_id),
    )


# --------------------------------------------------------------------------- #
# Pipeline (home)
# --------------------------------------------------------------------------- #
@app.route("/")
def home():
    by = request.args.get("by", "lead_number")
    term = request.args.get("q", "").strip()
    per_page = request.args.get("per_page", 10, type=int)
    if per_page not in PER_PAGE_CHOICES:
        per_page = 10

    where, args = "", []
    if term:
        col = "l.borrower_name" if by == "borrower_name" else "l.lead_number"  # whitelisted
        where, args = f"WHERE {col} LIKE ?", [f"%{term}%"]

    leads = q_all(
        f"""SELECT l.*,
                   GROUP_CONCAT(b.batch_id) AS batch_ids,
                   MAX(b.has_analysis_complete) AS has_any_analysis
            FROM leads l
            LEFT JOIN batches b ON b.lead_id = l.id
            {where}
            GROUP BY l.id
            ORDER BY l.created_at DESC, l.id ASC""",
        args,
    )

    total = len(leads)
    total_pages = max(1, math.ceil(total / per_page))
    page = min(max(1, request.args.get("page", 1, type=int)), total_pages)
    start = (page - 1) * per_page
    end = min(start + per_page, total)

    def page_url(p):
        params = {"page": p, "per_page": per_page}
        if term:
            params.update(q=term, by=by)
        return url_for("home", **params)

    return render_template(
        "home.html",
        leads=leads[start:end], total=total, page=page, total_pages=total_pages,
        first=start + 1 if total else 0, last=end,
        pages=page_numbers(page, total_pages), page_url=page_url,
        by=by, term=term, per_page=per_page, per_page_choices=PER_PAGE_CHOICES,
    )


@app.post("/leads/new")
def create_lead():
    lead_number = f"WTEST{int(time.time() * 1000)}"
    run("INSERT INTO leads (lead_number, loan_officer) VALUES (?, ?)",
        (lead_number, "LendingQB Test"))
    return redirect(url_for("intake_count", lead_number=lead_number))


# --------------------------------------------------------------------------- #
# Intake flow:  count -> (form -> upload) x N -> analysis
# --------------------------------------------------------------------------- #
@app.route("/intake/<lead_number>", methods=["GET", "POST"])
def intake_count(lead_number):
    lead = get_lead_or_404(lead_number)
    error, count = "", 1
    if request.method == "POST":
        count = request.form.get("count", 1, type=int)
        if count > 4:
            error = "Maximum of 4 businesses allowed"
        elif count < 1:
            error = "Please select at least 1 business"
        else:
            ids = create_batches(lead, count)
            return redirect(url_for("intake_form", lead_number=lead_number,
                                    ids=",".join(ids), i=0))
    return render_template("intake_count.html", lead_number=lead_number, count=count, error=error)


def intake_context(lead_number):
    """Validate ?ids=...&i=... and return (lead, ids, index, batch_row)."""
    lead = get_lead_or_404(lead_number)
    ids = [x for x in request.args.get("ids", "").split(",") if x]
    index = request.args.get("i", 0, type=int)
    if not ids or not 0 <= index < len(ids):
        abort(404)
    marks = ",".join("?" * len(ids))
    found = q_all(f"SELECT batch_id FROM batches WHERE lead_id = ? AND batch_id IN ({marks})",
                  (lead["id"], *ids))
    if len(found) != len(ids):
        abort(404)
    batch = q_one("SELECT * FROM batches WHERE batch_id = ?", (ids[index],))
    return lead, ids, index, batch


FORM_FIELDS = [
    "business_name", "bank_name", "account_last_four", "nsf_count",
    "borrower_ownership_percentage", "number_of_owners", "self_employment_duration",
    "exception_needed", "business_type", "anticipated_expense_factor", "business_url",
    "business_location", "bank_statement_months", "bank_statement_type", "business_description",
]
INT_FIELDS = {"nsf_count", "number_of_owners"}


@app.route("/intake/<lead_number>/form", methods=["GET", "POST"])
def intake_form(lead_number):
    lead, ids, index, batch = intake_context(lead_number)
    ids_csv = ",".join(ids)

    if request.method == "POST":
        data = {}
        for f in FORM_FIELDS:
            val = request.form.get(f, "").strip()
            if f in INT_FIELDS:
                data[f] = int(val) if val.lstrip("-").isdigit() else None
            else:
                data[f] = val or None
        data["space_type"] = ",".join(request.form.getlist("space_type")) or None

        # Borrower name is entered once (business 1) and reused for the rest.
        if index == 0:
            data["primary_borrower_first_name"] = request.form.get("primary_borrower_first_name", "").strip() or None
            data["primary_borrower_last_name"] = request.form.get("primary_borrower_last_name", "").strip() or None
            data["additional_borrowers"] = request.form.get("additional_borrowers", "").strip() or None
        else:
            first = q_one("SELECT primary_borrower_first_name, primary_borrower_last_name, "
                          "additional_borrowers FROM batches WHERE batch_id = ?", (ids[0],))
            data["primary_borrower_first_name"] = first["primary_borrower_first_name"]
            data["primary_borrower_last_name"] = first["primary_borrower_last_name"]
            data["additional_borrowers"] = first["additional_borrowers"]

        cols = ", ".join(f"{c} = ?" for c in data)  # column names come from our own whitelist
        run(f"UPDATE batches SET {cols}, updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?",
            (*data.values(), ids[index]))
        # Show the borrower in the pipeline: the lead's Borrower Name is "First Last".
        full_name = " ".join(n for n in (data["primary_borrower_first_name"],
                                         data["primary_borrower_last_name"]) if n)
        if index == 0 and full_name:
            run("UPDATE leads SET borrower_name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (full_name, lead["id"]))
        return redirect(url_for("intake_upload", lead_number=lead_number, ids=ids_csv, i=index))

    return render_template(
        "intake_form.html", lead_number=lead_number, v=dict(batch),
        index=index, total=len(ids), is_last=index == len(ids) - 1,
        spaces=(batch["space_type"] or "").split(","),
    )


@app.route("/intake/<lead_number>/upload")
def intake_upload(lead_number):
    _lead, ids, index, batch = intake_context(lead_number)
    is_last = index == len(ids) - 1
    next_url = (url_for("analysis", lead_number=lead_number) if is_last else
                url_for("intake_form", lead_number=lead_number, ids=",".join(ids), i=index + 1))
    return render_template("intake_upload.html", lead_number=lead_number,
                           business_name=batch["business_name"], is_last=is_last, next_url=next_url)


# --------------------------------------------------------------------------- #
# Analysis summary
# --------------------------------------------------------------------------- #
@app.route("/analysis/<lead_number>")
def analysis(lead_number):
    lead = get_lead_or_404(lead_number)
    batches = q_all("SELECT * FROM batches WHERE lead_id = ? ORDER BY created_at DESC, id ASC",
                    (lead["id"],))
    selected = None
    if batches:
        wanted = request.args.get("batch")
        selected = next((b for b in batches if b["batch_id"] == wanted), batches[0])
    return render_template(
        "analysis.html", lead=lead, batches=batches, batch=selected,
        edit=request.args.get("edit") == "1",
    )


@app.post("/analysis/<lead_number>/status")
def update_status(lead_number):
    lead = get_lead_or_404(lead_number)
    field = request.form.get("field")
    value = request.form.get("value", "")
    batch_id = request.form.get("batch", "")
    batch = q_one("SELECT * FROM batches WHERE batch_id = ? AND lead_id = ?", (batch_id, lead["id"]))
    if batch is None or field not in ("eligibility_status", "income_analysis_status"):
        abort(400)

    if field == "eligibility_status":
        run("UPDATE batches SET eligibility_status = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE batch_id = ?", (value, batch_id))
        if (value in DECIDED and lead["income_analysis_status"] == "COMPLETE"
                and not batch["has_analysis_complete"]):
            populate_dummy_data(batch_id, value)
    else:
        run("UPDATE leads SET income_analysis_status = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE lead_number = ?", (value, lead_number))
        if (value == "COMPLETE" and batch["eligibility_status"] in DECIDED
                and not batch["has_analysis_complete"]):
            populate_dummy_data(batch_id, batch["eligibility_status"])

    return redirect(url_for("analysis", lead_number=lead_number, batch=batch_id, edit=1))


# --------------------------------------------------------------------------- #
# "Seed Data" button: wipe everything and load the demo set from the original app
# --------------------------------------------------------------------------- #
DEMO_LEADS = [
    ("WTEST2026010148", "Alex UpdateTest", "Purchase"),
    ("WTEST2026010147", "Alex UpdateTest", "Cash Out"),
    ("WTEST2026010146", "alex processtest", "Refinance"),
    ("WTEST2026010145", "Alice Test Name", "Cash Out"),
    ("WTEST2026010144", None, None),
    ("WTEST2026010129", "sf asfas", "Purchase"),
    ("WTEST2025080070", None, None),
    ("WTEST2025080068", None, None),
]

# (lead index, batch_id, business, status, gross, net, large-deposit threshold, 6-mo change %, findings)
DEMO_BATCHES = [
    (0, "WTEST2026010148-1", "Tech Solutions LLC", "PENDING", None, None, None, None, None),
    (0, "WTEST2026010148-2", "Creative Design Co", "PENDING", None, None, None, None, None),
    (1, "WTEST2026010147-1", "Marketing Pro Services", "PENDING", None, None, None, None, None),
    (2, "WTEST2026010146-1", "Restaurant Ventures", "INELIGIBLE", 45000, 22500, 5000, -8.5,
     "Business shows declining revenue patterns with high expense ratios. Multiple NSF occurrences indicate cash flow challenges."),
    (2, "WTEST2026010146-2", "Retail Store Inc", "INELIGIBLE", 38000, 19000, 4500, 2.1,
     "Irregular deposit patterns and high NSF count raise concerns about business stability."),
    (2, "WTEST2026010146-3", "Consulting Group LLC", "ELIGIBLE", 95000, 71250, 10000, 6.4,
     "Strong consistent income with healthy cash reserves. Business demonstrates excellent financial management."),
    (3, "WTEST2026010145-1", "Construction Services", "ELIGIBLE", 125000, 87500, 12000, 14.2,
     "Excellent cash flow with consistent project-based deposits. Well-managed expenses and minimal NSF activity."),
    (3, "WTEST2026010145-2", "Property Management Co", "ELIGIBLE", 98000, 68600, 9500, 3.8,
     "Recurring rental income provides stable cash flow. Expense management is within industry standards."),
    (3, "WTEST2026010145-3", "Legal Advisory LLC", "ELIGIBLE", 145000, 108750, 14000, 5.1,
     "Professional services firm with excellent payment history. Strong client retention and minimal receivables issues."),
    (3, "WTEST2026010145-4", "Healthcare Consulting", "ELIGIBLE", 165000, 123750, 16000, 9.7,
     "Healthcare sector showing strong demand. Excellent cash management and qualified income streams."),
    (4, "WTEST2026010144-1", "E-commerce Ventures", "ELIGIBLE", 185000, 129500, 18000, 22.4,
     "Online business showing strong sales momentum. Clean bank statements with no NSF activity."),
]


@app.post("/seed")
def seed_demo_data():
    db = get_db()
    db.execute("DELETE FROM batches")
    db.execute("DELETE FROM leads")
    lead_ids, lead_names = [], []
    for number, name, purpose in DEMO_LEADS:
        cur = db.execute(
            "INSERT INTO leads (lead_number, borrower_name, loan_officer, lead_purpose) VALUES (?,?,?,?)",
            (number, name, "LendingQB Test", purpose))
        lead_ids.append(cur.lastrowid)
        lead_names.append(name)

    for li, batch_id, biz, status, gross, net, large, change, findings in DEMO_BATCHES:
        done = 1 if gross else 0
        bad = status == "INELIGIBLE"
        parts = (lead_names[li] or "").split()
        first, last = (parts[0], parts[-1]) if len(parts) > 1 else (None, None)
        db.execute(
            """INSERT INTO batches (
                 lead_id, batch_id, business_name, primary_borrower_first_name, primary_borrower_last_name,
                 bank_name, account_last_four, nsf_count, borrower_ownership_percentage, number_of_owners,
                 self_employment_duration, exception_needed, business_type, anticipated_expense_factor,
                 business_url, business_location, space_type, bank_statement_months, bank_statement_type,
                 business_description, gross_deposit, total_net_deposit, deposit_qualification,
                 large_deposit_threshold, changes_recent_6_month, eligibility_status, has_analysis_complete,
                 income_period, expense_ratio, ownership_percentage, start_period, end_period,
                 analysis_findings, nsf_qualification, year_over_year_comparison
               ) VALUES (?,?,?,?,?, ?,?,?,?,?, ?,?,?,?, ?,?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?,?, ?,?,?)""",
            (lead_ids[li], batch_id, biz, first, last,
             "Chase Bank" if done else None, "4567" if done else None,
             (3 if bad else 0) if done else None, "75%" if done else None, 2 if done else None,
             "5 years" if done else None, "High NSF activity" if bad else None,
             "LLC" if done else None, "50%" if done else None,
             "https://example.com" if done else None, "San Francisco, CA" if done else None,
             "Commercial" if done else None, "12" if done else None, "Business" if done else None,
             "Professional services firm" if done else None, gross, net, net,
             large, change, status, done,
             "12 months" if done else None, ("65%" if bad else "50%") if done else None,
             "100%" if done else None, "01/2023" if done else None, "12/2023" if done else None,
             findings, ("FAIL" if bad else "OK") if done else None, "Stable" if done else None),
        )
    db.commit()
    flash("Sample data seeded successfully.")
    return redirect(url_for("home"))


# --------------------------------------------------------------------------- #
init_db()

if __name__ == "__main__":
    if "--reset" in sys.argv:
        # Rebuild and exit. (Don't fall through to app.run: the debug reloader re-executes
        # this script, which would wipe the database again on every code change.)
        init_db(reset=True)
        print(f"Rebuilt {DB_PATH} from the exported production data.")
        sys.exit(0)
    app.run(debug=True, port=int(os.environ.get("PORT", 5050)))
