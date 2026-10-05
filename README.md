# Freight Dispatch Email Extractor

Turns the unstructured emails a freight dispatching company receives (load offers, rate confirmations, check calls, breakdown reports, detention requests, invoices, carrier setup) into structured records for routing and Power BI reporting.

## Setup
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export OPENAI_API_KEY=...        # Windows PowerShell: $env:OPENAI_API_KEY="..."
export OPENAI_MODEL=gpt-4o-mini  # optional; use a stronger model if accuracy needs it
```

## Run
```bash
python dispatch_extractor.py --input sample_emails.csv --dry-run   # check cleaning/redaction, no API calls
python dispatch_extractor.py --input sample_emails.csv             # full run
python dispatch_extractor.py --input ./eml_folder --limit 50       # folder of .eml files
```
Outputs go to `outputs/`:
- `dispatch_records.csv` : every email, one row each (load this into Power BI)
- `review_queue.csv` : rows a person should check (see review rules below)

Results are cached in `.cache/` by email content, so reruns do not repeat API charges.

## What gets extracted
| Group | Fields |
|---|---|
| Classification | `request_type`, `priority` (critical / urgent / normal / low), `action_needed`, `summary` |
| Who | `counterparty_type` (broker, shipper, carrier or driver, receiver, factoring), `counterparty_name`, `contact_name`, `contact_phone`, `carrier_name`, `mc_or_dot_number` |
| Load | `load_number`, `other_references` (PO, BOL, PRO), `service_type`, `equipment_type`, `commodity`, `weight_lbs`, `pallet_count`, `temperature_requirement`, `miles` |
| Lane and timing | origin and destination (facility, city, state), `stop_count`, `pickup_date` and window, `delivery_date` and window |
| Money | `rate_usd`, `rate_basis` (flat or per mile), `amount_mentioned_usd` (detention, TONU, lumper, claim, invoice) |
| Safety and quality | `special_requirements`, `risk_flags`, `contains_sensitive_info`, `missing_fields`, `confidence` |

Request types: `load_offer`, `rate_negotiation`, `load_tender`, `rate_confirmation`, `status_check`, `eta_update`, `delay_or_breakdown`, `detention_or_layover`, `accessorial_request`, `paperwork_request`, `paperwork_submitted`, `cancellation`, `appointment_change`, `invoice_or_payment`, `carrier_onboarding`, `claim_or_damage`, `complaint`, `other`.

Pickup and delivery times are kept as written, in the stop's local time, with no time zone conversion.

## Review queue rules
`review_reasons()` in `dispatch_extractor.py` decides what goes to a person. Edit it to match your process.

| Reason | Meaning |
|---|---|
| `critical_attention` | Breakdown, accident, stranded driver, or a load that needs a truck now |
| `possible_fraud` | Any risk flag: payment or bank change request, pressure to skip verification, sender identity mismatch, request for sensitive data |
| `missing_<field>` | A load offer, tender, or rate confirmation lacks origin, destination, pickup date, load number, or rate |
| `money_or_liability` | Detention, accessorial, or claim requests that need approval |
| `complaint` | General complaints |
| `sensitive_info_present` | Bank, tax, or card numbers appeared in the email |
| `low_confidence`, `missing_fields` | The model was unsure or the email left out key details |

## How the pieces work
1. **Clean**: removes reply chains, signatures, disclaimers, and HTML.
2. **Redact**: masks SSNs, EINs, bank routing and account numbers, and card numbers before anything is sent to the model. This catches common patterns only.
3. **Extract**: the model gets the sender, subject, received time, and body, and returns JSON that must match the `FreightRecord` schema (fixed allowed values for request type, priority, equipment, and so on).
4. **Validate**: dates must be real `YYYY-MM-DD` dates, numbers cannot be negative, and failed outputs are retried.
5. **Review rules**: flag anything risky or incomplete for a person.

## Before using real emails
- Rates, customers, and lanes are confidential. Send real emails only to an AI provider whose data-handling terms your company has reviewed.
- Keep a person in the loop for signing rate confirmations, approving detention or other charges, and any payment or bank change. This tool flags and suggests; it does not verify or act.
- Payment-change fraud is common in freight. If an email asks to change bank or remit details, verify by calling a phone number you already have on file, never one from the email.
- Rate confirmations often arrive as PDF attachments. This version reads the email body only, so the next big improvement is extracting text from PDF attachments.

## Validate before trusting it
1. Export 100 to 200 past dispatch emails and hand-label the key fields (request type, priority, lanes, dates, rate, equipment).
2. Run the extractor on them and compare field by field.
3. Track accuracy per field, especially priority, dates, and rates. Adjust the rules and examples in `SYSTEM_PROMPT` (bump `PROMPT_VERSION` so the cache refreshes) and repeat.

## Power BI ideas
Loads offered vs booked by week, lanes by volume (origin state to destination state), rate per mile by equipment type and lane, response volume by broker, critical events (breakdowns, missed appointments) over time, detention and accessorial dollars by customer, carrier onboarding backlog, review-queue rate, and extraction confidence over time.

## Automatic email ingestion (Gmail)
`gmail_ingest.py` reads new mail from a Gmail or Google Workspace mailbox with the Gmail API and feeds it to the extractor. It uses read-only access.

### One-time setup (about 10 minutes)
1. Go to console.cloud.google.com and create a project (for example "dispatch-extractor").
2. APIs & Services > Library > search **Gmail API** > Enable.
3. Open the **OAuth consent screen** (called "Google Auth Platform" in the newer console). Choose **External** (or Internal if you are on Google Workspace and only your organization will use it), enter an app name and your email, and add the Gmail address you will read as a **test user**.
4. Credentials > Create credentials > **OAuth client ID** > Application type **Desktop app** > Create > **Download JSON**. Rename the file to `credentials.json` and put it in the project folder.
5. Run `python gmail_ingest.py --since-hours 24`. A browser window opens: sign in, click Advanced > "Go to ... (unsafe)" (Google shows this warning for your own unverified app), and allow read-only access. This creates `token.json`.

While the app is in "Testing" status, Google expires the sign-in after 7 days. If you get an auth error later, delete `token.json` and run again.

### Try it with test emails
1. Send yourself a few emails (copy the bodies from `sample_emails.csv`).
2. Run `python gmail_ingest.py --process`. This fetches them, runs the extractor, and writes `outputs/dispatch_records.csv` and `outputs/review_queue.csv`.
3. Run it again: nothing new is fetched, and no rows are duplicated.

For real use, create a Gmail label (for example `dispatch`) with a filter that labels dispatch emails, then run `python gmail_ingest.py --query "label:dispatch" --process`. The `--query` option accepts any Gmail search.

### Notes for real use
- Limit the tool to a dedicated dispatch mailbox or label so it only reads what it needs.
- Never commit `credentials.json` or `token.json` (the included `.gitignore` excludes them, along with `outputs/` and `incoming/`).
- For a company mailbox in production, the usual setup is a service account with domain-wide delegation limited to the read-only Gmail scope and to the dispatch mailbox only, configured by a Workspace admin.
- Scheduling: cron or Task Scheduler running `python gmail_ingest.py --process` every 5 minutes. The first sign-in needs a browser, so sign in on your computer first and copy `token.json` to the server.

## Automatic email ingestion (Microsoft 365)
`graph_ingest.py` pulls new mail from the dispatch mailbox through the Microsoft Graph API and feeds it to the extractor.

### One-time setup (needs a Microsoft 365 / Entra admin)
1. Entra admin center > App registrations > New registration (single tenant). Note the Tenant ID and Client ID.
2. Certificates & secrets > New client secret. Copy the secret value.
3. API permissions > Add > Microsoft Graph > **Application permissions** > `Mail.Read` > Grant admin consent.
4. **Restrict the app to the dispatch mailbox only.** An application `Mail.Read` permission can read every mailbox in the tenant by default. Have your admin limit it to the dispatch mailbox with an Exchange Online Application Access Policy (or the newer RBAC for Applications).
5. Copy `.env.example` to `.env` and fill in the values. Never commit `.env`.

### Run
```bash
python graph_ingest.py --since-hours 24   # first run: pull the last 24 hours into incoming/batch_*.csv
python graph_ingest.py --process          # pull mail since the last run, then run the extractor on it
```
How both ingestion scripts work:
- The newest received time is saved in a state file, and each run fetches only newer mail.
- Each run writes `incoming/batch_<time>.csv`. With `--process` it then runs `dispatch_extractor.py --append`, which merges into `outputs/dispatch_records.csv` and keeps one row per email, so reruns never duplicate rows.
- The saved time only advances after the extractor succeeds, so a crash means the next run retries the same emails (the cache avoids paying twice).
- Your own outgoing mail and drafts are skipped. Attachments are not read yet; only the email body is processed.
- `review_queue.csv` is rebuilt from all flagged rows. Track which ones are resolved in your own tool (a SharePoint list, Power Apps screen, or shared sheet works well).

Scheduling (Linux/macOS cron, every 5 minutes): `*/5 * * * * cd /path/to/project && .venv/bin/python graph_ingest.py --process >> ingest.log 2>&1`. On Windows, use Task Scheduler with action `python.exe graph_ingest.py --process`, starting in the project folder. For production, run it as an Azure Function or container job and keep secrets in Azure Key Vault.

## Next steps
- Read PDF attachments (rate confirmations, BOLs, PODs) and merge their text into the email body before extraction.
- Normalize lanes (city and state) and verify miles with a geocoding or routing service.
- Push records into your TMS or a shared sheet, and draft replies (rate quotes, check-call updates) for a dispatcher to approve before sending.
- Cluster emails labeled `other` to discover request types you have not defined yet.
