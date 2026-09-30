"""
Decides which contracts need validation this run, without touching the
existing check_refs.py validation logic.

- Scheduled (cron) runs: fetches each contract's index.json (a small file --
  this does NOT download the full provider-directory bundles) and compares
  its `last_updated` against state/mpf_state.json. Only contracts whose
  last_updated changed are queued for validation.
- Manual (workflow_dispatch) runs: the user's `contract` choice (ALL or one
  code) is queued regardless of whether the data actually changed, since a
  manual run is an explicit request.

Writes:
  state/plan_result.json  -- full per-contract detail, used by mpf_finalize.py
                              and the GitHub Step Summary.
  $GITHUB_OUTPUT: contracts_json (JSON array), has_work (true/false)
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from check_refs import CONTRACTS, fetch  # noqa: E402  (reuse, don't duplicate)

STATE_PATH = "state/mpf_state.json"
PLAN_RESULT_PATH = "state/plan_result.json"


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def get_last_updated(url):
    idx = json.loads(fetch(url))
    return idx.get("last_updated")


def main():
    event_name = os.environ.get("GITHUB_EVENT_NAME", "schedule")
    manual_contract = os.environ.get("MANUAL_CONTRACT", "").strip().upper()
    is_manual = event_name == "workflow_dispatch"

    state = load_state()
    rows = []
    to_process = []

    for code, url in CONTRACTS.items():
        try:
            current = get_last_updated(url)
            check_error = None
        except Exception as e:  # network hiccup checking the index -- don't crash the whole plan
            current = None
            check_error = f"{type(e).__name__}: {e}"

        previous = state.get(code)

        if check_error:
            status = f"CHECK_FAILED ({check_error})"
        elif is_manual:
            if manual_contract == "ALL" or manual_contract == code:
                status = "MANUAL_RUN"
                to_process.append(code)
            else:
                status = "SKIPPED (not selected)"
        else:
            if previous is not None and previous == current:
                status = "NO CHANGE"
            else:
                status = "UPDATED"
                to_process.append(code)

        rows.append({
            "contract": code,
            "current_last_updated": current,
            "previous_last_updated": previous,
            "status": status,
        })

    os.makedirs("state", exist_ok=True)
    with open(PLAN_RESULT_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "event_name": event_name,
            "run_mode": "Manual" if is_manual else "Automatic",
            "rows": rows,
            "to_process": to_process,
        }, f, indent=2)

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"contracts_json={json.dumps(to_process)}\n")
            f.write(f"has_work={'true' if to_process else 'false'}\n")

    lines = [
        "## MPF Provider Directory - Change Detection",
        "",
        f"**Run mode:** {'MANUAL' if is_manual else 'AUTOMATIC'}",
        "",
        "| Contract | Current Update | Previous Update | Status |",
        "|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['contract']} | {r['current_last_updated'] or 'N/A'} | "
                      f"{r['previous_last_updated'] or '(none yet)'} | {r['status']} |")
    if to_process:
        lines.append("")
        lines.append(f"**Will validate:** {', '.join(to_process)}")
    else:
        lines.append("")
        lines.append("**No contracts need validation this run.**")

    text = "\n".join(lines)
    print(text)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
