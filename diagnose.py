"""Step-by-step check of why the dashboard is blank.

Run:  python diagnose.py
It tests each stage in order and stops at the first thing that is wrong,
printing exactly what to change in your .env file.
"""

import json

import requests

import config
import dataverse


def head(step, text):
    print(f"\n[{step}] {text}")


def main():
    print("SLA Monitor diagnostics")
    print("Environment:", config.DATAVERSE_URL)
    print("Table      :", config.ENTITY_SET)
    print("Filter     :", config.EXTRA_FILTER or "(none)")

    head(1, "Signing in...")
    try:
        token = dataverse.get_token()
    except Exception as exc:
        print("  FAILED:", exc)
        print("  -> Check TENANT_ID / AUTH_MODE in .env, and that you completed the browser sign-in.")
        return
    print("  OK - token acquired")

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    base = f"{config.DATAVERSE_URL}/api/data/v9.2"

    head(2, "Reaching the Dataverse Web API...")
    who = requests.get(f"{base}/WhoAmI", headers=headers, timeout=60)
    if who.status_code != 200:
        print(f"  FAILED: HTTP {who.status_code} - {who.text[:400]}")
        print("  -> Your account may not have access to this environment.")
        return
    print("  OK - signed in as user id", who.json().get("UserId"))

    head(3, "Finding the service request table...")
    meta = requests.get(
        f"{base}/EntityDefinitions(LogicalName='crmee_service_request')?$select=EntitySetName",
        headers=headers,
        timeout=60,
    )
    if meta.status_code == 200:
        entity_set = meta.json().get("EntitySetName")
        print("  OK - entity set is:", entity_set)
        if entity_set != config.ENTITY_SET:
            print(f"  -> Set ENTITY_SET={entity_set} in your .env file.")
    else:
        entity_set = config.ENTITY_SET
        print(f"  Could not read metadata (HTTP {meta.status_code}); continuing with {entity_set}")

    head(4, "Reading 3 rows with NO filter and NO column list...")
    raw = requests.get(
        f"{base}/{entity_set}?$top=3",
        headers={**headers, "Prefer": 'odata.include-annotations="*"'},
        timeout=60,
    )
    if raw.status_code != 200:
        print(f"  FAILED: HTTP {raw.status_code} - {raw.text[:500]}")
        print("  -> Usually means the table name is wrong, or you lack read privilege on it.")
        return
    rows = raw.json().get("value", [])
    print(f"  OK - {len(rows)} row(s) returned")
    if not rows:
        print("  -> The table is readable but empty for your security role.")
        print("     The Power Apps view may be showing rows owned by others that you cannot query.")
        return

    print("\n  Columns available on the first row:")
    for key in sorted(rows[0].keys()):
        if key.startswith("@"):
            continue
        value = rows[0][key]
        print(f"    {key:55} = {json.dumps(value)[:60]}")

    head(5, "Checking your configured columns...")
    for label, name in [
        ("COL_CASE_NUMBER", config.COL_CASE_NUMBER),
        ("COL_SLA_DUE", config.COL_SLA_DUE),
        ("COL_OWNER", config.COL_OWNER),
        ("COL_STATE", config.COL_STATE),
    ]:
        mark = "OK  " if name in rows[0] else "MISSING"
        print(f"  {mark} {label} = {name}")
    print("  -> Replace any MISSING entries with a real column name from the list above.")

    head(6, f"Applying your filter: {config.EXTRA_FILTER or '(none)'}")
    if config.EXTRA_FILTER:
        filtered = requests.get(
            f"{base}/{entity_set}?$top=3&$filter={config.EXTRA_FILTER}",
            headers=headers,
            timeout=60,
        )
        if filtered.status_code != 200:
            print(f"  FAILED: HTTP {filtered.status_code} - {filtered.text[:400]}")
            print("  -> Clear EXTRA_FILTER in .env.")
        else:
            count = len(filtered.json().get("value", []))
            print(f"  OK - {count} row(s) match")
            if count == 0:
                print("  -> This filter is why the dashboard is blank. Clear EXTRA_FILTER in .env.")

    print("\nDiagnostics complete.")


if __name__ == "__main__":
    main()
