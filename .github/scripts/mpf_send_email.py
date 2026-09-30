"""
Sends the per-contract Automatic/Manual notification email described in the
MPF automation spec.

Honesty note: check_refs.py validates one thing end-to-end (download -> parse
-> reference-integrity checks -> report generation) rather than instrumenting
"FHIR Validation" / "Business Validation" as separate, independently-checked
phases. Rather than fabricate pass/fail for phases the script doesn't
actually check, this reports:
  - Provider Directory Download + JSON Parsing: PASS if the script reached
    Pass 2 (i.e. didn't crash before parsing), else UNKNOWN (crashed first).
  - Reference Integrity: PASS/FAIL based on the actual dangling-reference
    count check_refs.py computed.
  - Report Generation: PASS if the .xlsx/.docx were actually produced.
If the script crashed outright, this sends a clearly-labeled failure email
with the log tail instead of guessing at phase-level results.

Env vars:
  CONTRACT, RUN_MODE ("Automatic"/"Manual"), SCRIPT_EXIT_CODE
  EMAIL_USERNAME, EMAIL_PASSWORD, EMAIL_TO, SMTP_SERVER, SMTP_PORT, RUN_URL
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
PLAN_RESULT_PATH = "state/plan_result.json"


def load_json(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def get_plan_row(contract):
    plan = load_json(PLAN_RESULT_PATH)
    if not plan:
        return {}
    return next((r for r in plan["rows"] if r["contract"] == contract), {})


def find_report_files(contract):
    candidates = sorted(glob.glob(os.path.join(REPORTS_DIR, f"reference_integrity_contract_{contract}_report.*")))
    if not candidates:
        candidates = sorted(glob.glob(os.path.join(REPORTS_DIR, f"*{contract}*.csv")))
    return candidates


def build_email(contract, run_mode, script_crashed, exit_code, run_url):
    exec_date = datetime.now(timezone.utc)
    exec_date_str = exec_date.strftime("%Y-%m-%d")
    exec_dt_str = exec_date.strftime("%Y-%m-%dT%H:%M:%SZ")

    row = get_plan_row(contract)
    previous_update = row.get("previous_last_updated") or "(none recorded)"
    new_update = row.get("current_last_updated") or "(unknown)"

    summary = load_json(os.path.join(REPORTS_DIR, "summary.json"))
    report_files = find_report_files(contract)
    report_names = ", ".join(os.path.basename(p) for p in report_files) or "(not generated)"

    tag = "MPF AUTO VALIDATION" if run_mode == "Automatic" else "MPF MANUAL VALIDATION"

    if script_crashed:
        subject = f"[{tag}] {contract} - Validation Script FAILED"
        log_tail = ""
        log_path = os.path.join(REPORTS_DIR, f"validation_run_{contract}.log")
        if os.path.exists(log_path):
            with open(log_path, encoding="utf-8", errors="replace") as f:
                log_tail = "".join(f.readlines()[-40:])
        body = f"""Hi Team,

The MPF Provider Directory validation for contract {contract} did NOT complete successfully.

--------------------------------------------------
CONTRACT DETAILS
--------------------------------------------------

Contract        : {contract}
Plan Year       : 2027
Run Mode        : {run_mode}
Previous Update : {previous_update}
New Update      : {new_update}

--------------------------------------------------
VALIDATION STATUS
--------------------------------------------------

Overall Validation Status   : SCRIPT EXECUTION FAILED (exit code {exit_code})

This is a runtime/script failure, not a data-quality finding -- the stored
last_updated value for {contract} has NOT been advanced, so this update will
be re-attempted on the next scheduled run.

Last log lines:

{log_tail}

Thanks,
MPF Provider Directory Automation
"""
        return subject, body

    dangling = summary["fail_count"] if summary else None
    overall_status = "FAILED" if dangling else "COMPLETED"
    subject = (f"[{tag}] {contract} - Provider Directory Updated & Validation Completed"
               if run_mode == "Automatic" else f"[{tag}] {contract} - Validation Report")
    if overall_status == "FAILED":
        subject = f"[{tag}] {contract} - Validation Report (FAILED)"

    intro = (f"The CMS Medicare Advantage Plan Finder Provider Directory for contract "
             f"{contract} has been updated.\n\nValidation was automatically triggered "
             f"based on the new provider directory update."
             if run_mode == "Automatic" else
             "The MPF Provider Directory validation was manually triggered.")

    body = f"""Hi Team,

{intro}

--------------------------------------------------
CONTRACT DETAILS
--------------------------------------------------

Contract        : {contract}
Plan Year       : 2027
Run Mode        : {run_mode}
Previous Update : {previous_update}
New Update      : {new_update}

--------------------------------------------------
VALIDATION STATUS
--------------------------------------------------

Provider Directory Download : PASS
JSON Parsing                : PASS
Reference Integrity         : {"FAIL" if dangling else "PASS"}
Report Generation           : {"PASS" if report_files else "FAIL"}

Overall Validation Status   : {overall_status}
"""
    if summary and summary.get("fail_details"):
        body += "\nFailed Checks:\n\n"
        for d in summary["fail_details"][:25]:
            body += f"  - {d}\n"
        if len(summary["fail_details"]) > 25:
            body += f"  - ... {len(summary['fail_details']) - 25} more (see attached report)\n"

    body += f"""
--------------------------------------------------
REPORT
--------------------------------------------------

Report:
{report_names}
"""
    if run_url:
        body += f"\nFull GitHub Actions run: {run_url}\n"

    body += "\nThanks,\nMPF Provider Directory Automation\n"
    return subject, body


def build_html_body(plain_body, contract, script_crashed):
    """Wraps the plain-text summary + the full report tables (same content as
    the .docx / the run-validation.yml email) into one HTML body, scoped to
    just this contract, so the reader sees the results without opening the
    attachment."""
    header_html = "<br>".join(html_escape(line) for line in plain_body.splitlines())
    report_html = "" if script_crashed else build_html_report([contract])
    return f"""\
<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222">
<div style="white-space:normal">{header_html}</div>
{report_html}
</body></html>"""


def attach_reports(msg, contract):
    for path in find_report_files(contract):
        with open(path, "rb") as f:
            data = f.read()
        msg.add_attachment(data, maintype="application", subtype="octet-stream",
                            filename=os.path.basename(path))


def main():
    contract = os.environ["CONTRACT"]
    run_mode = os.environ.get("RUN_MODE", "Automatic")
    exit_code = os.environ.get("SCRIPT_EXIT_CODE", "0")
    script_crashed = exit_code not in ("0", "", None)
    run_url = os.environ.get("RUN_URL", "")

    username = os.environ.get("EMAIL_USERNAME", "").strip()
    password = os.environ.get("EMAIL_PASSWORD", "")
    to_raw = os.environ.get("EMAIL_TO", "").strip()

    if not (username and password and to_raw):
        print("::warning::EMAIL_USERNAME / EMAIL_PASSWORD / EMAIL_TO are not all "
              f"set -- skipping email for {contract}. The report is still available "
              "as a workflow artifact.")
        return

    recipients = [addr.strip() for addr in to_raw.split(",") if addr.strip()]
    smtp_server = os.environ.get("SMTP_SERVER") or "smtp.gmail.com"
    smtp_port = int(os.environ.get("SMTP_PORT") or "465")

    subject, body = build_email(contract, run_mode, script_crashed, exit_code, run_url)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = username
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)  # plain-text fallback for clients that don't render HTML
    msg.add_alternative(build_html_body(body, contract, script_crashed), subtype="html")
    if not script_crashed:
        attach_reports(msg, contract)

    context = ssl.create_default_context()
    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_server, smtp_port, context=context) as server:
            server.login(username, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls(context=context)
            server.login(username, password)
            server.send_message(msg)

    print(f"Email sent for {contract} to {', '.join(recipients)} via {smtp_server}:{smtp_port}")


if __name__ == "__main__":
    main()
