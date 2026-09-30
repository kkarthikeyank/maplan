"""
Sends the validation report by email using SMTP.

Reads all configuration from environment variables (set from GitHub Secrets by
the workflow) -- nothing is hard-coded. If EMAIL_USERNAME / EMAIL_PASSWORD /
EMAIL_TO aren't all set, this prints a warning and exits 0 without sending, so
missing email config never fails the workflow (the report is still uploaded as
an artifact regardless).

Required env vars:
  EMAIL_USERNAME   SMTP login (e.g. a Gmail address)
  EMAIL_PASSWORD   SMTP password -- for Gmail this MUST be an App Password,
                   not the normal account password (see README).
  EMAIL_TO         comma-separated recipient list

Optional env vars:
  SMTP_SERVER      default: smtp.gmail.com
  SMTP_PORT        default: 465 (SMTP_SSL)
  SCRIPT_EXIT_CODE set by the workflow to the validation script's exit code;
                   "0" (or unset) = ran to completion, anything else = crashed.
  RUN_URL          link back to the GitHub Actions run, included in the email.
"""
import glob
import json
import os
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage

REPORTS_DIR = "reports"


def load_summary():
    path = os.path.join(REPORTS_DIR, "summary.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def build_message(summary, script_crashed, exit_code, run_url):
    exec_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    environment = os.environ.get("VALIDATION_ENVIRONMENT", "Production")

    if script_crashed:
        subject = f"FHIR Provider Directory Validation Report - FAILED - {exec_date}"
        status_line = f"SCRIPT EXECUTION FAILED (exit code {exit_code})"
        checks = pass_n = fail_n = warn_n = "N/A"
    else:
        status_line = summary["overall_status"] if summary else "UNKNOWN"
        checks = summary["total_validation_checks"] if summary else "N/A"
        pass_n = summary["pass_count"] if summary else "N/A"
        fail_n = summary["fail_count"] if summary else "N/A"
        warn_n = summary["warning_count"] if summary else "N/A"
        subject = f"FHIR Provider Directory Validation Report - {exec_date}"

    body = f"""Hi Team,

The automated FHIR Provider Directory validation has completed.

Execution Date: {exec_date}
Environment: {environment}
Validation Status: {status_line}
Total Checks: {checks}
PASS: {pass_n}
FAIL: {fail_n}
WARNING: {warn_n}

Please find the detailed validation report attached.
"""
    if script_crashed:
        log_tail = ""
        log_path = os.path.join(REPORTS_DIR, "validation_run.log")
        if os.path.exists(log_path):
            with open(log_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            log_tail = "".join(lines[-40:])
        body += f"\nThe validation script did not complete successfully. Last log lines:\n\n{log_tail}\n"
    elif summary and summary.get("fail_details"):
        body += "\nFAIL details:\n"
        for d in summary["fail_details"][:20]:
            body += f"  - {d}\n"
        if len(summary["fail_details"]) > 20:
            body += f"  - ... {len(summary['fail_details']) - 20} more (see attached report)\n"

    if run_url:
        body += f"\nFull GitHub Actions run: {run_url}\n"

    body += "\nThanks,\nQA Team\n"
    return subject, body


def attach_reports(msg):
    # Prefer the Excel workbook (per the task: Excel preferred when supported);
    # fall back to whatever CSV/docx report files exist if xlsx wasn't produced.
    candidates = sorted(glob.glob(os.path.join(REPORTS_DIR, "*.xlsx")))
    candidates += sorted(glob.glob(os.path.join(REPORTS_DIR, "*.docx")))
    if not candidates:
        candidates = sorted(glob.glob(os.path.join(REPORTS_DIR, "*.csv")))

    attached_any = False
    for path in candidates:
        with open(path, "rb") as f:
            data = f.read()
        msg.add_attachment(
            data,
            maintype="application",
            subtype="octet-stream",
            filename=os.path.basename(path),
        )
        attached_any = True
    return attached_any


def main():
    username = os.environ.get("EMAIL_USERNAME", "").strip()
    password = os.environ.get("EMAIL_PASSWORD", "")
    to_raw = os.environ.get("EMAIL_TO", "").strip()

    if not (username and password and to_raw):
        print("::warning::EMAIL_USERNAME / EMAIL_PASSWORD / EMAIL_TO are not all "
              "set -- skipping email notification. The report is still available "
              "as a workflow artifact.")
        return

    recipients = [addr.strip() for addr in to_raw.split(",") if addr.strip()]
    # An unset GitHub secret still arrives as an empty string, not a missing
    # env var, so `.get(key, default)` alone won't fall back -- `or` catches it.
    smtp_server = os.environ.get("SMTP_SERVER") or "smtp.gmail.com"
    smtp_port = int(os.environ.get("SMTP_PORT") or "465")
    run_url = os.environ.get("RUN_URL", "")

    exit_code = os.environ.get("SCRIPT_EXIT_CODE", "0")
    script_crashed = exit_code not in ("0", "", None)

    summary = load_summary()
    subject, body = build_message(summary, script_crashed, exit_code, run_url)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = username
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    attach_reports(msg)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(smtp_server, smtp_port, context=context) as server:
        server.login(username, password)
        server.send_message(msg)

    print(f"Email sent to {', '.join(recipients)} via {smtp_server}:{smtp_port}")


if __name__ == "__main__":
    main()
