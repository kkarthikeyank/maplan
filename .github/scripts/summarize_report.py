"""
Reads the CSVs that check_refs.py already writes (dangling_summary_<C>.csv,
orphan_refs_<C>.csv, resource_counts_<C>.csv) from reports/, rolls them into one
summary, writes reports/summary.json for send_report_email.py to consume, and
prints a Markdown table to $GITHUB_STEP_SUMMARY so the run is readable from the
Actions UI without downloading anything.

This does not touch validation logic -- it only aggregates the numbers the
existing script already produced.
"""
import csv
import glob
import json
import os

REPORTS_DIR = "reports"


def read_csv(path):
    if not os.path.exists(path):
        return [], []
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    return (rows[0], rows[1:]) if rows else ([], [])


def main():
    contracts = sorted({
        os.path.basename(p).replace("dangling_summary_", "").replace(".csv", "")
        for p in glob.glob(os.path.join(REPORTS_DIR, "dangling_summary_*.csv"))
    })

    total_checks = 0
    total_pass = 0
    total_fail = 0
    total_warning = 0
    total_resources = 0
    fail_details = []
    sample_ids = []

    for c in contracts:
        _, srows = read_csv(os.path.join(REPORTS_DIR, f"dangling_summary_{c}.csv"))
        for row in srows:
            src_type, field, tgt_type, tgt_pub, total_refs, bad, affected = row[:7]
            total_checks += 1
            if int(bad) > 0:
                total_fail += 1
                fail_details.append(
                    f"{c}: {src_type}.{field} -> {tgt_type} "
                    f"({bad} dangling of {total_refs}, {affected} resources affected)"
                )
            else:
                total_pass += 1

        _, crows = read_csv(os.path.join(REPORTS_DIR, f"resource_counts_{c}.csv"))
        total_resources += sum(int(r[1]) for r in crows)

        _, orows = read_csv(os.path.join(REPORTS_DIR, f"orphan_refs_{c}.csv"))
        for row in orows:
            flag = row[5] if len(row) > 5 else ""
            if flag == "Review":
                total_warning += 1
                if len(sample_ids) < 10:
                    sample_ids.append(f"{c}: {row[0]}/{row[1]} ({row[4] or 'no type'})")

    dangling_refs_path = glob.glob(os.path.join(REPORTS_DIR, "dangling_refs_*.csv"))
    total_dangling_rows = 0
    for p in dangling_refs_path:
        _, rows = read_csv(p)
        total_dangling_rows += len(rows)

    if total_fail > 0:
        overall_status = "FAIL"
    elif total_warning > 0:
        overall_status = "PASS_WITH_WARNINGS"
    else:
        overall_status = "PASS"

    summary = {
        "contracts": contracts,
        "total_resources_checked": total_resources,
        "total_validation_checks": total_checks,
        "pass_count": total_pass,
        "fail_count": total_fail,
        "warning_count": total_warning,
        "total_dangling_reference_rows": total_dangling_rows,
        "sample_warning_identifiers": sample_ids,
        "fail_details": fail_details,
        "overall_status": overall_status,
    }

    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(os.path.join(REPORTS_DIR, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    step_summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    lines = [
        "## FHIR Provider Directory Validation Summary",
        "",
        f"**Contracts:** {', '.join(contracts) or '(none found)'}",
        f"**Overall status:** {overall_status}",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Total resources checked | {total_resources:,} |",
        f"| Total validation checks | {total_checks:,} |",
        f"| PASS | {total_pass:,} |",
        f"| FAIL | {total_fail:,} |",
        f"| WARNING (orphan, needs review) | {total_warning:,} |",
        f"| Dangling reference rows | {total_dangling_rows:,} |",
        "",
    ]
    if fail_details:
        lines.append("### FAIL details")
        lines.extend(f"- {d}" for d in fail_details[:25])
        if len(fail_details) > 25:
            lines.append(f"- ... {len(fail_details) - 25} more (see attached report)")
        lines.append("")
    if sample_ids:
        lines.append("### Sample WARNING identifiers")
        lines.extend(f"- {s}" for s in sample_ids)
        lines.append("")

    text = "\n".join(lines)
    print(text)
    if step_summary_path:
        with open(step_summary_path, "a", encoding="utf-8") as f:
            f.write(text)


if __name__ == "__main__":
    main()
