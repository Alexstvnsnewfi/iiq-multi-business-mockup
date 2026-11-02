CREATE TABLE leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_number TEXT NOT NULL UNIQUE,
  borrower_name TEXT,
  loan_officer TEXT,
  lead_purpose TEXT,
  eligibility_status TEXT,
  income_analysis_status TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id TEXT NOT NULL UNIQUE,
  lead_id INTEGER NOT NULL,
  business_name TEXT,
  primary_borrower_first_name TEXT,
  primary_borrower_last_name TEXT,
  additional_borrowers TEXT,
  income_period TEXT,
  expense_ratio TEXT,
  ownership_percentage TEXT,
  start_period TEXT,
  end_period TEXT,
  analysis_findings TEXT,
  nsf_qualification TEXT,
  large_deposit_threshold REAL,
  changes_recent_6_month REAL,
  year_over_year_comparison TEXT,
  deposit_qualification REAL,
  gross_deposit REAL,
  total_net_deposit REAL,
  has_analysis_complete BOOLEAN DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  eligibility_status TEXT,
  bank_name TEXT,
  account_last_four TEXT,
  nsf_count INTEGER,
  borrower_ownership_percentage TEXT,
  number_of_owners INTEGER,
  self_employment_duration TEXT,
  exception_needed TEXT,
  business_type TEXT,
  anticipated_expense_factor TEXT,
  business_url TEXT,
  business_location TEXT,
  space_type TEXT,
  bank_statement_months TEXT,
  bank_statement_type TEXT,
  business_description TEXT
);

CREATE INDEX idx_leads_lead_number ON leads(lead_number);
CREATE INDEX idx_batches_lead_id ON batches(lead_id);
CREATE INDEX idx_batches_batch_id ON batches(batch_id);
