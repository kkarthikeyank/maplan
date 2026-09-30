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
from html import escape as html_escape

from build_html_report import build_html_report

REPORTS_DIR = "reports"


def load_summary():
    path = os.path.join(REPORTS_DIR, "summary.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def build_message(summary, script_crashed, exit_code, run_url, attached_names):
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
    if attached_names:
        body += "\nAttached report file(s):\n"
        for name in attached_names:
            body += f"  - {name}\n"
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

    return subject, body


def build_html_body(plain_body, script_crashed):
    """Wraps the plain-text summary + the full per-contract report tables
    (same content as the .docx) into one HTML email body, so the reader sees
    the results without opening the attachment."""
    header_html = "<br>".join(html_escape(line) for line in plain_body.splitlines())
    report_html = "" if script_crashed else build_html_report()
    return f"""\
<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222">
<div style="white-space:normal">{header_html}</div>
{report_html}
</body></html>"""


def find_report_files():
    # Prefer the Excel workbook (per the task: Excel preferred when supported);
    # fall back to whatever CSV/docx report files exist if xlsx wasn't produced.
    # Filenames are dynamic (e.g. reference_integrity_contract_H5826_report.xlsx),
    # named after whichever contract(s) check_refs.py just ran.
    candidates = sorted(glob.glob(os.path.join(REPORTS_DIR, "*.xlsx")))
    candidates += sorted(glob.glob(os.path.join(REPORTS_DIR, "*.docx")))
    if not candidates:
        candidates = sorted(glob.glob(os.path.join(REPORTS_DIR, "*.csv")))
    return candidates


def attach_reports(msg, paths):
    for path in paths:
        with open(path, "rb") as f:
            data = f.read()
        msg.add_attachment(
            data,
            maintype="application",
            subtype="octet-stream",
            filename=os.path.basename(path),
        )


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
    report_paths = find_report_files()
    attached_names = [os.path.basename(p) for p in report_paths]
    subject, body = build_message(summary, script_crashed, exit_code, run_url, attached_names)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = username
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)  # plain-text fallback for clients that don't render HTML
    msg.add_alternative(build_html_body(body, script_crashed), subtype="html")
    attach_reports(msg, report_paths)

    context = ssl.create_default_context()
    # Port 465 = implicit TLS (Gmail's default). Everything else -- notably
    # Office365/Outlook's 587 -- speaks plaintext then upgrades via STARTTLS.
    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_server, smtp_port, context=context) as server:
            server.login(username, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls(context=context)
            server.login(username, password)
            server.send_message(msg)

    print(f"Email sent to {', '.join(recipients)} via {smtp_server}:{smtp_port}")


if __name__ == "__main__":
    main()
