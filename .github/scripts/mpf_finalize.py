"""
Merges the per-contract state_<CONTRACT>.json files (downloaded as artifacts
from each matrix job) into state/mpf_state.json, advancing last_updated only
for contracts whose validation actually succeeded this run. Contracts that
failed, crashed, or weren't processed this run keep their previous stored
value untouched, so they're retried (or re-checked) next time.

Also prints the final GitHub Step Summary combining the change-detection
table from mpf_plan.py with each processed contract's outcome.
"""
import glob
import json
import os

STATE_PATH = "state/mpf_state.json"
PLAN_RESULT_PATH = "state/plan_result.json"
STATE_ARTIFACTS_DIR = "state_artifacts"


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def main():
    state = load_state()

    plan = {}
    if os.path.exists(PLAN_RESULT_PATH):
        with open(PLAN_RESULT_PATH, encoding="utf-8") as f:
            plan = json.load(f)

    results = {}
    for path in sorted(glob.glob(os.path.join(STATE_ARTIFACTS_DIR, "state_*.json"))) + \
                sorted(glob.glob("state/state_*.json")):
        with open(path, encoding="utf-8") as f:
            r = json.load(f)
        results[r["contract"]] = r
        if r["succeeded"] and r["new_last_updated"]:
            state[r["contract"]] = r["new_last_updated"]

    os.makedirs("state", exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)

    lines = [
        "## MPF Provider Directory Validation - Final Summary",
        "",
        f"**Run mode:** {plan.get('run_mode', 'Unknown')}",
        "",
        "| Contract | Change Status | Validation Result |",
        "|---|---|---|",
    ]
    for row in plan.get("rows", []):
        c = row["contract"]
        r = results.get(c)
        if r is None:
            outcome = "SKIPPED - NO CHANGE" if row["status"] == "NO CHANGE" else row["status"]
        elif r["succeeded"]:
            outcome = "COMPLETED"
        else:
            outcome = f"FAILED (exit code {r['exit_code']})"
        lines.append(f"| {c} | {row['status']} | {outcome} |")

    lines.append("")
    lines.append(f"Updated state/mpf_state.json: {json.dumps(state)}")

    text = "\n".join(lines)
    print(text)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
