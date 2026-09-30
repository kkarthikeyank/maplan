# maplan

FHIR Provider Directory reference-integrity validator.

`check_refs.py` downloads each Medicare Advantage plan's published FHIR
provider-directory bundles, then:

1. **Pass 1** — indexes every `(resourceType, id)` that actually exists.
2. **Pass 2** — walks every `reference` field in every resource and flags any
   target that doesn't resolve against that index ("dangling reference"), and
   also checks the reverse direction: `Organization` resources never pointed
   to by an `OrganizationAffiliation`, and `Practitioner` resources never
   pointed to by a `PractitionerRole` ("orphan" checks).

It writes per-contract CSVs plus a combined `FHIR_Reference_Integrity_Report.xlsx`
and `FHIR_Reference_Integrity_Report.docx`.

## Running locally

```bash
pip install -r requirements.txt

# Validate every configured contract
python check_refs.py

# Validate one contract
python check_refs.py H1619

# Validate several contracts, each as its own separate pass
python check_refs.py H1619 H3124

# Force re-download instead of using the local cache/ folder
python check_refs.py H5826 --fresh
```

Outputs land in the current directory:

- `dangling_refs_<CONTRACT>.csv` — one row per broken reference
- `dangling_summary_<CONTRACT>.csv` — pass/fail counts per relationship
- `orphan_refs_<CONTRACT>.csv` — resources never referenced back
- `placeholder_refs_<CONTRACT>.csv` — dummy/test-looking target ids
- `resource_counts_<CONTRACT>.csv` — total published resources per type
- `FHIR_Reference_Integrity_Report.xlsx` / `.docx` — combined report

No credentials are required — the script only reads each contract's public
`index.json` and bundle files over HTTPS.

## Automated Validation

The validation runs automatically via GitHub Actions:
[`.github/workflows/run-validation.yml`](.github/workflows/run-validation.yml).

### Running the GitHub Action manually

1. Go to the repository's **Actions** tab.
2. Select **FHIR Provider Directory Validation**.
3. Click **Run workflow**.
4. Optionally enter a single contract code (e.g. `H1619`) in the **contract**
   input; leave it blank to validate every configured contract.

### Scheduled execution

The workflow also runs on a cron schedule (currently every Monday at
06:00 UTC), defined by the `schedule:` trigger at the top of
`run-validation.yml`. Edit the cron expression there to change the cadence —
no other changes are needed.

### Configuring GitHub Secrets

Set these under **Settings → Secrets and variables → Actions → New repository
secret**:

| Secret | Required | Description |
|---|---|---|
| `EMAIL_USERNAME` | For email | SMTP login, e.g. your Gmail address |
| `EMAIL_PASSWORD` | For email | SMTP password (see Gmail note below) |
| `EMAIL_TO` | For email | Recipient(s). Comma-separated for multiple, e.g. `a@x.com,b@x.com` |
| `SMTP_SERVER` | Optional | Defaults to `smtp.gmail.com`. Set to `smtp.office365.com` for Outlook/Microsoft 365 |
| `SMTP_PORT` | Optional | Defaults to `465` (implicit TLS, Gmail). Use `587` for Office365 (STARTTLS) |

If any of `EMAIL_USERNAME` / `EMAIL_PASSWORD` / `EMAIL_TO` is missing, the
workflow still runs the validation and uploads the report artifact — it just
skips sending the email (with a warning in the run log), so a missing email
secret never fails the workflow.

**Using Gmail:** use an **App Password**, not your normal Gmail password.
Generate one at <https://myaccount.google.com/apppasswords> (requires 2-Step
Verification to be enabled on the account) and store that in the
`EMAIL_PASSWORD` secret.

**Using Outlook / Microsoft 365:** set `SMTP_SERVER` to `smtp.office365.com`
and `SMTP_PORT` to `587`. If the account has MFA/Security Defaults enabled
(the Microsoft 365 default), SMTP AUTH with the regular password will be
rejected — either use an **App Password**
(<https://mysignins.microsoft.com/security-info>, if the tenant allows them)
or have your admin enable SMTP AUTH for the mailbox.

### Where the report is generated

Each run copies every generated report file into `reports/` (this directory
is git-ignored — it's only populated at run time, both locally and in CI).
The primary report is `reports/FHIR_Reference_Integrity_Report.xlsx`, with
the supporting per-contract CSVs and `.docx` alongside it.

### Downloading the report from GitHub Actions

1. Open the workflow run under the **Actions** tab.
2. Scroll to **Artifacts**.
3. Download **fhir-validation-report** — a zip of everything under
   `reports/`, kept for 30 days regardless of whether the email step
   succeeded.

### How the email notification works

After the validation step (whether it passes, fails validation checks, or
crashes), the workflow:

1. Collects the generated report files into `reports/`.
2. Summarizes PASS/FAIL/WARNING counts (`.github/scripts/summarize_report.py`).
3. Uploads `reports/` as a workflow artifact.
4. Sends an email (`.github/scripts/send_report_email.py`) with the summary
   in the body and the Excel/Word report attached.

The email is always attempted, even when the validation script crashes or
finds FAIL records — a crash sends a failure-style email with the tail of the
run log instead of the pass/fail counts.

### If the workflow fails

- **Red X on "Run validation script"**: the script itself crashed (network
  issue, unexpected data shape, etc.). Check `validation_run.log` in the
  `fhir-validation-report` artifact, or the step's own log output, for the
  traceback. An email is still sent (if secrets are configured) describing
  the failure.
- **Red X on "Send validation email"**: usually an SMTP auth problem — for
  Gmail, confirm `EMAIL_PASSWORD` is an App Password and that 2-Step
  Verification is on. The report artifact is unaffected and still downloadable.
- **Workflow shows green but FAIL counts are non-zero**: this is expected —
  the workflow succeeds as long as the script itself ran to completion. FAIL
  findings are data-quality results, not a workflow error; check the emailed
  report / artifact for details.
