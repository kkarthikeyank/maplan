"""
Writes state/state_<CONTRACT>.json recording whether this contract's
validation completed successfully, and the last_updated value it should be
stamped with going forward.

Per the spec: state is only advanced when the validation process itself
completed successfully -- a script crash must NOT mark the new update as
processed, so it gets retried on the next scheduled run instead of silently
being skipped forever.

Runs once per matrix job (one per contract), after check_refs.py, and
independently of whether the email send later succeeds or fails.

Env vars:
  CONTRACT           the contract code this matrix job is validating
  SCRIPT_EXIT_CODE   check_refs.py's exit code for this contract
"""
import json
import os

PLAN_RESULT_PATH = "state/plan_result.json"


def main():
    contract = os.environ["CONTRACT"]
    exit_code = os.environ.get("SCRIPT_EXIT_CODE", "1")
    succeeded = exit_code == "0"

    with open(PLAN_RESULT_PATH, encoding="utf-8") as f:
        plan = json.load(f)
    row = next((r for r in plan["rows"] if r["contract"] == contract), None)
    current_last_updated = row["current_last_updated"] if row else None

    result = {
        "contract": contract,
        "succeeded": succeeded,
        "exit_code": exit_code,
        # Only advance the stored last_updated on success -- on failure this
        # stays None and mpf_finalize.py leaves the previous value untouched,
        # so the same update gets retried next run instead of being skipped.
        "new_last_updated": current_last_updated if succeeded else None,
    }

    os.makedirs("state", exist_ok=True)
    out_path = f"state/state_{contract}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(f"Wrote {out_path}: {result}")


if __name__ == "__main__":
    main()
