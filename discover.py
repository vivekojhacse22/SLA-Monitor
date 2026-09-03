"""Prints the real entity-set name and column names for the service request table.

Run this once if the dashboard reports an unknown column or entity set:
    python discover.py
Then copy the matching logical names into your .env file.
"""

import requests

import config
import dataverse


def main():
    token = dataverse.get_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    base = f"{config.DATAVERSE_URL}/api/data/v9.2"

    meta = requests.get(
        f"{base}/EntityDefinitions(LogicalName='crmee_service_request')"
        "?$select=LogicalName,EntitySetName,DisplayName",
        headers=headers,
        timeout=60,
    )
    meta.raise_for_status()
    entity = meta.json()
    print("Entity set name :", entity.get("EntitySetName"))
    print()

    attrs = requests.get(
        f"{base}/EntityDefinitions(LogicalName='crmee_service_request')/Attributes"
        "?$select=LogicalName,AttributeType",
        headers=headers,
        timeout=60,
    )
    attrs.raise_for_status()
    values = sorted(attrs.json().get("value", []), key=lambda a: a["LogicalName"])

    print("Likely case-number columns:")
    for a in values:
        n = a["LogicalName"]
        if any(k in n for k in ("name", "number", "ticket", "case")):
            print(f"  {n:45} {a['AttributeType']}")

    print("\nLikely SLA / date columns:")
    for a in values:
        n = a["LogicalName"]
        if "sla" in n or a["AttributeType"] == "DateTime":
            print(f"  {n:45} {a['AttributeType']}")

    print("\nOwner / lookup columns:")
    for a in values:
        n = a["LogicalName"]
        if "owner" in n or "assign" in n or "engineer" in n:
            print(f"  {n:45} {a['AttributeType']}")

    print("\nProduct columns:")
    for a in values:
        n = a["LogicalName"]
        if "product" in n:
            print(f"  {n:45} {a['AttributeType']}")

    print(f"\nTotal columns on this table: {len(values)}")


if __name__ == "__main__":
    main()
