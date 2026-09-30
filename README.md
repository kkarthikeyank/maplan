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

It writes per-contract CSVs plus a combined Excel/Word report named after the
contract(s) just run, e.g. `reference_integrity_contract_H5826_report.xlsx`
for one contract, or `reference_integrity_contract_H1619_H3124_report.xlsx`
for several.

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
- `reference_integrity_contract_<CONTRACT(S)>_report.xlsx` / `.docx` — combined report, named after whichever contract(s) were run

No credentials are required — the script only reads each contract's public
`index.json` and bundle files over HTTPS.

There are two GitHub Actions workflows on top of this script:

- **[`run-validation.yml`](#automated-validation)** — simple manual/scheduled
  run of one, several, or all contracts, every time it fires.
- **[`mpf-validation.yml`](#mpf-automatic-change-detection-validation)** —
  checks each contract's `index.json` for changes every 15 minutes and only
  validates + emails for contracts that actually changed, plus an on-demand
  manual mode. Use this one if you want "only tell me when something changed."

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
The primary report is `reports/reference_integrity_contract_<CONTRACT(S)>_report.xlsx`,
named dynamically after whichever contract(s) the run covered (e.g.
`reference_integrity_contract_H5826_report.xlsx`), with the supporting
per-contract CSVs and the matching `.docx` alongside it.

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

## MPF Automatic Change-Detection Validation

[`.github/workflows/mpf-validation.yml`](.github/workflows/mpf-validation.yml)
is the "only validate what actually changed" workflow. It reuses
`check_refs.py` unmodified — it never duplicates or rewrites the validation
logic, it only decides *when* to call it and sends a per-contract email
afterward.

```
Scheduled (every 15 min) ──┐
                            ├─→ plan job (checks index.json) ─→ validate job (matrix, one per
Manual (workflow_dispatch) ─┘                                   changed/selected contract) ─→ finalize job
                                                                  │
                                                                  ├─→ python check_refs.py <CONTRACT>  (unchanged)
                                                                  ├─→ per-contract email
                                                                  └─→ per-contract report artifact
```

### How change detection works

- `state/mpf_state.json` stores the last successfully-processed `last_updated`
  value per contract (committed to the repo — it's the durable record).
- Every scheduled run, the **plan** job fetches each contract's small
  `index.json` (not the full bundles) and compares `last_updated` against the
  stored value.
  - **Unchanged** → contract is skipped entirely: no download, no validation,
    no report, no email.
  - **Changed** (or never seen before) → contract is queued.
- The **validate** job runs as a matrix — one job per queued contract, each
  calling `python check_refs.py <CONTRACT>` independently. A change in H1619
  never triggers validation for H3124/H5826/H9207.
- `state/mpf_state.json` is only advanced for a contract **after its
  validation completes successfully** (exit code 0). If the script crashes,
  that contract's stored value is left untouched, so the same update is
  retried on the next scheduled run instead of being silently marked done.

### Manual mode

Actions → **MPF Provider Directory Validation** → Run workflow → choose
`ALL` or a specific contract (`H1619` / `H3124` / `H5826` / `H9207`) from the
dropdown → Run workflow. Manual mode always runs the selected contract(s),
regardless of whether the data actually changed — it's an explicit request,
not a change check. Runs entirely on GitHub's runners; your own computer
does not need to be on.

### Email content

Each processed contract gets its own email:

- **Automatic**, data changed, no issues: `[MPF AUTO VALIDATION] H1619 - Provider Directory Updated & Validation Completed`
- **Automatic/Manual, validation script crashed**: `[MPF AUTO VALIDATION] H1619 - Validation Script FAILED` — state is *not* advanced for this contract; body includes the log tail and says so explicitly.
- **Validation completed but found dangling references**: subject includes `(FAILED)`, body lists the failed checks (source → field → target, with counts).
- **Manual**: `[MPF MANUAL VALIDATION] H5826 - Validation Report`.

Note on honesty: `check_refs.py` validates end-to-end (download → parse →
reference-integrity checks → report generation) rather than instrumenting
"FHIR Validation" and "Business Validation" as separate, independently
measured phases. The email reports **Provider Directory Download**, **JSON
Parsing**, **Reference Integrity**, and **Report Generation** as the phases
that are actually distinguishable from the script's behavior, rather than
inventing pass/fail for checks it doesn't perform.

### Required GitHub Secrets

Same as [Automated Validation](#automated-validation) above:
`EMAIL_USERNAME`, `EMAIL_PASSWORD`, `EMAIL_TO`, optional `SMTP_SERVER` /
`SMTP_PORT`. Missing secrets → validation and state updates still happen,
email is skipped with a warning.

### Testing it

- **Test manual mode**: Actions → MPF Provider Directory Validation → Run
  workflow → pick one contract → confirm exactly one email/report is
  produced and `state/mpf_state.json` updates for that contract only.
- **Test that unchanged contracts are skipped**: run the workflow twice in a
  row (scheduled or via `workflow_dispatch` with the same contract data) —
  the second run's **plan** job step summary should show `NO CHANGE` for
  every contract and the **validate** job should not run at all
  (`has_work: false`).
- **Test that only the changed contract runs**: this can't be forced without
  the upstream data changing, but the plan job's step summary always shows
  the full 4-contract comparison table (`current` vs `previous` per
  contract), so you can confirm which one(s) triggered a run.
- **Test that your PC being off doesn't matter**: trigger a run from a
  different device (or just don't touch your PC) — the scheduled cron and
  manual dispatch both execute entirely on GitHub-hosted runners.
- **Example logs**: the plan job's Step Summary always renders the
  4-contract comparison table; the finalize job's Step Summary renders the
  final per-contract outcome table (`COMPLETED` / `FAILED (exit code N)` /
  `SKIPPED - NO CHANGE`) — both visible directly on the workflow run page
  without downloading anything.
