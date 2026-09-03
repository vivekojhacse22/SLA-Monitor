"""Thin Dataverse Web API client: token acquisition + service request query."""

import json
import os
from datetime import datetime, timezone

import requests

import config


class AuthError(RuntimeError):
    pass


class QueryError(RuntimeError):
    pass


def _scope():
    return [f"{config.DATAVERSE_URL}/.default"]


def _authority():
    return f"https://login.microsoftonline.com/{config.TENANT_ID}"


def _load_cache():
    import msal

    cache = msal.SerializableTokenCache()
    if os.path.exists(config.TOKEN_CACHE_FILE):
        with open(config.TOKEN_CACHE_FILE, "r", encoding="utf-8") as fh:
            cache.deserialize(fh.read())
    return cache


def _save_cache(cache):
    if cache.has_state_changed:
        with open(config.TOKEN_CACHE_FILE, "w", encoding="utf-8") as fh:
            fh.write(cache.serialize())


def get_token(device_code_callback=None):
    """Return an access token for the Dataverse environment."""
    import msal  # imported lazily so the query/classify layer works without it

    if config.AUTH_MODE == "app":
        if not (config.CLIENT_ID and config.CLIENT_SECRET and config.TENANT_ID):
            raise AuthError(
                "App-only sign-in needs TENANT_ID, CLIENT_ID and CLIENT_SECRET to be set."
            )
        app = msal.ConfidentialClientApplication(
            config.CLIENT_ID,
            authority=_authority(),
            client_credential=config.CLIENT_SECRET,
        )
        result = app.acquire_token_for_client(scopes=_scope())
    else:
        cache = _load_cache()
        broker = config.AUTH_MODE == "auto"
        try:
            app = msal.PublicClientApplication(
                config.CLIENT_ID,
                authority=_authority(),
                token_cache=cache,
                enable_broker_on_windows=broker,
            )
        except Exception:  # older msal, or msal[broker] not installed
            broker = False
            app = msal.PublicClientApplication(
                config.CLIENT_ID, authority=_authority(), token_cache=cache
            )

        result = None
        accounts = app.get_accounts(username=config.LOGIN_HINT or None) or app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(_scope(), account=accounts[0])

        if not result and config.AUTH_MODE == "auto":
            # Uses the account you are already signed into Windows with, so
            # conditional-access rules that block device-code sign-in are satisfied.
            kwargs = {"scopes": _scope(), "login_hint": config.LOGIN_HINT or None}
            if broker:
                kwargs["parent_window_handle"] = app.CONSOLE_WINDOW_HANDLE
            try:
                print("Opening a sign-in window in your browser...", flush=True)
                result = app.acquire_token_interactive(**kwargs)
                if result and "access_token" not in result:
                    print("  Sign-in window returned:", result.get("error_description", result), flush=True)
                    result = None
            except Exception as exc:
                print(f"  Sign-in window did not work: {type(exc).__name__}: {exc}", flush=True)
                result = None
            if not result and broker:
                # Retry once without the Windows broker, which is often what fails.
                try:
                    print("  Retrying sign-in without the Windows helper...", flush=True)
                    result = app.acquire_token_interactive(
                        scopes=_scope(), login_hint=config.LOGIN_HINT or None
                    )
                    if result and "access_token" not in result:
                        result = None
                except Exception as exc:
                    print(f"  That did not work either: {exc}", flush=True)
                    result = None
            if not result:
                print("  Falling back to a device code.", flush=True)

        if not result or "access_token" not in (result or {}):
            flow = app.initiate_device_flow(scopes=_scope())
            if "user_code" not in flow:
                raise AuthError("Could not start sign-in: " + json.dumps(flow))
            message = flow["message"]
            if device_code_callback:
                device_code_callback(message)
            else:
                print("\n" + message + "\n", flush=True)
            result = app.acquire_token_by_device_flow(flow)
        _save_cache(cache)

    if not result or "access_token" not in result:
        raise AuthError((result or {}).get("error_description", "Sign-in failed."))
    return result["access_token"]


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
        "Prefer": f'odata.include-annotations="*",odata.maxpagesize={config.PAGE_SIZE}',
    }


NOTES = []


def resolve_user_id(token, email):
    """Look up a Dataverse systemuserid from a work email address."""
    url = (
        f"{config.DATAVERSE_URL}/api/data/v9.2/systemusers"
        f"?$select=systemuserid&$filter=internalemailaddress eq '{email}'&$top=1"
    )
    resp = requests.get(url, headers=_headers(token), timeout=60)
    if resp.status_code != 200:
        return None
    values = resp.json().get("value", [])
    return values[0]["systemuserid"] if values else None


def _run(token, url, params):
    rows = []
    resp = requests.get(url, headers=_headers(token), params=params, timeout=60)
    while True:
        if resp.status_code >= 400:
            raise QueryError(f"Dataverse returned {resp.status_code}: {resp.text[:600]}")
        payload = resp.json()
        rows.extend(payload.get("value", []))
        next_link = payload.get("@odata.nextLink")
        if not next_link or len(rows) >= config.PAGE_SIZE * 20:
            break
        resp = requests.get(next_link, headers=_headers(token), timeout=60)
    return rows


def _display(row, column):
    """Display name of a lookup, falling back to the raw value."""
    label = row.get(f"{column}@OData.Community.Display.V1.FormattedValue")
    return str(label if label is not None else (row.get(column) or ""))


def _lookup_navigation(column):
    """Convert a lookup value property such as _ownerid_value to its navigation name."""
    if column.startswith("_") and column.endswith("_value"):
        return column[1:-6]
    return column


def _odata_string(value):
    return value.replace("'", "''")


def apply_row_filters(rows):
    """Keep only rows matching the configured lookup display values."""
    if not rows:
        return rows

    if config.COUNTRY_EQUALS:
        before = len(rows)
        wanted = config.COUNTRY_EQUALS.strip().lower()
        rows = [r for r in rows if _display(r, config.COL_COUNTRY).strip().lower() == wanted]
        NOTES.append(
            f"Country filter '{config.COUNTRY_EQUALS}': {len(rows)} of {before} rows kept."
        )

    if config.SUPPORT_REGION_EQUALS:
        before = len(rows)
        wanted = config.SUPPORT_REGION_EQUALS.strip().lower()
        rows = [
            row
            for row in rows
            if _display(row, config.COL_SUPPORT_REGION).strip().lower() == wanted
        ]
        NOTES.append(
            f"Support Region filter '{config.SUPPORT_REGION_EQUALS}': "
            f"{len(rows)} of {before} rows kept."
        )

    if config.SUPPORT_REGION_CONTAINS:
        before = len(rows)
        wanted = config.SUPPORT_REGION_CONTAINS.strip().lower()
        rows = [
            row
            for row in rows
            if wanted in _display(row, config.COL_SUPPORT_REGION).strip().lower()
        ]
        NOTES.append(
            f"Support Region contains '{config.SUPPORT_REGION_CONTAINS}': "
            f"{len(rows)} of {before} rows kept."
        )

    if config.SUPPORT_POD_CONTAINS:
        before = len(rows)
        wanted = config.SUPPORT_POD_CONTAINS.strip().lower()
        rows = [
            row
            for row in rows
            if wanted in _display(row, config.COL_POD).strip().lower()
        ]
        NOTES.append(
            f"Support POD contains '{config.SUPPORT_POD_CONTAINS}': "
            f"{len(rows)} of {before} rows kept."
        )

    if config.PRODUCT_EQUALS:
        before = len(rows)
        wanted = config.PRODUCT_EQUALS.strip().lower()
        rows = [
            row
            for row in rows
            if _display(row, config.COL_PRODUCT).strip().lower() == wanted
        ]
        NOTES.append(
            f"Product filter '{config.PRODUCT_EQUALS}': "
            f"{len(rows)} of {before} rows kept."
        )

    return rows


def select_columns():
    """The $select list. Shared by the dashboard query and the orchestrator."""
    return ",".join(
        dict.fromkeys(
            [config.COL_ID, config.COL_CASE_NUMBER, config.COL_STATE,
             config.COL_STATUS_CODE, config.COL_SLA_START, config.COL_OWNER,
             config.COL_CASE_OWNER, config.COL_SLA_STATE, config.COL_SLA_MET,
             config.COL_COUNTRY, config.COL_SUPPORT_REGION, config.COL_PRODUCT,
             config.COL_POD,
             config.COL_ACTIVE_STATUS, config.COL_TRANSFER_REASON,
             config.COL_TRANSFER_REASON_DESCRIPTION,
             config.SYNC_WATERMARK_COLUMN]
            + config.SLA_COLUMNS
        )
    )


def fetch_service_requests(token):
    """Pull active service requests with their SLA due date and owner.

    If a configured column or filter does not exist, fall back to a plain
    "give me everything" query rather than returning a blank page, and record
    a note the dashboard can show.
    """
    NOTES.clear()
    select = select_columns()
    url = f"{config.DATAVERSE_URL}/api/data/v9.2/{config.ENTITY_SET}"
    # No $orderby: sorting on the SLA column server-side is slow on this table,
    # and the dashboard sorts by urgency locally anyway.
    params = {"$select": select}

    filters = [config.EXTRA_FILTER] if config.EXTRA_FILTER else []
    row_filters_server_side = bool(
        config.COUNTRY_EQUALS
        or config.SUPPORT_REGION_EQUALS
        or config.SUPPORT_REGION_CONTAINS
        or config.SUPPORT_POD_CONTAINS
        or config.PRODUCT_EQUALS
    )
    if config.COUNTRY_EQUALS:
        country = _odata_string(config.COUNTRY_EQUALS)
        filters.append(
            f"{_lookup_navigation(config.COL_COUNTRY)}/crmee_name eq '{country}'"
        )
    if config.SUPPORT_REGION_EQUALS:
        support_region = _odata_string(config.SUPPORT_REGION_EQUALS)
        filters.append(
            f"{_lookup_navigation(config.COL_SUPPORT_REGION)}/crmee_name "
            f"eq '{support_region}'"
        )
    if config.SUPPORT_REGION_CONTAINS:
        support_region = _odata_string(config.SUPPORT_REGION_CONTAINS)
        filters.append(
            f"contains({_lookup_navigation(config.COL_SUPPORT_REGION)}/crmee_name,"
            f"'{support_region}')"
        )
    if config.SUPPORT_POD_CONTAINS:
        support_pod = _odata_string(config.SUPPORT_POD_CONTAINS)
        filters.append(
            f"contains({_lookup_navigation(config.COL_POD)}/crmee_name,"
            f"'{support_pod}')"
        )
    if config.PRODUCT_EQUALS:
        product = _odata_string(config.PRODUCT_EQUALS)
        filters.append(
            f"{_lookup_navigation(config.COL_PRODUCT)}/crmee_name eq '{product}'"
        )
    if config.MINE_ONLY:
        owner_id = resolve_user_id(token, config.OWNER_EMAIL)
        if owner_id:
            filters.append(f"_ownerid_value eq {owner_id}")
        else:
            NOTES.append(f"Could not find a user with the email {config.OWNER_EMAIL}.")
    if filters:
        params["$filter"] = " and ".join(filters)

    try:
        rows = _run(token, url, params)
    except QueryError as exc:
        row_filters_server_side = False
        if "404" in str(exc):
            raise QueryError(
                f"The table '{config.ENTITY_SET}' was not found in {config.DATAVERSE_URL}. "
                "Run  python discover.py  to get the correct entity set name."
            ) from exc
        NOTES.append(
            "Configured columns/filter were rejected, so all columns are being "
            "returned instead. Run discover.py and fix the names in .env. "
            f"Original error: {exc}"
        )
        rows = _run(token, url, {})

    if rows and config.COL_CASE_NUMBER not in rows[0]:
        NOTES.append(
            f"Column '{config.COL_CASE_NUMBER}' is not on these records. "
            "Available columns: " + ", ".join(sorted(rows[0].keys())[:60])
        )
    if row_filters_server_side:
        NOTES.append(
            f"Dataverse lookup filters matched {len(rows)} rows "
            "before paging."
        )
    else:
        rows = apply_row_filters(rows)

    if rows and not any(any(r.get(c) for c in config.SLA_COLUMNS) for r in rows):
        NOTES.append(
            "Rows came back, but none of them have a value in "
            + " or ".join(config.SLA_COLUMNS)
            + ". Set COL_SLA_DUE in .env to whichever date column your team tracks."
        )
    if not rows:
        NOTES.append(
            f"The query succeeded but returned 0 rows. Filter in use: "
            f"'{config.EXTRA_FILTER or '(none)'}'. Clear EXTRA_FILTER in .env to see "
            "every row, or check that your account has read access to this table."
        )
    return rows


def _parse(value):
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def refresh_timing(records, now=None):
    """Recalculate SLA countdowns for records read from the local cache."""
    now = now or datetime.now(timezone.utc)
    for record in records:
        due = _parse(record.get("sla_due_utc"))
        minutes = None if due is None else (due - now).total_seconds() / 60.0
        state = str(record.get("sla_state") or "").strip().lower()
        if state in ("achieved", "met", "complete", "completed"):
            status = "Met"
        elif minutes is None:
            status = "No SLA set"
        elif minutes < 0:
            status = "Breached"
        elif minutes <= config.WARN_MINUTES:
            status = "Expiring soon"
        else:
            status = "On track"
        record["minutes_remaining"] = None if minutes is None else round(minutes, 1)
        record["status"] = status
    return records


def classify(rows, now=None):
    """Turn raw rows into dashboard records with an SLA status."""
    now = now or datetime.now(timezone.utc)
    case_owner_key = config.COL_CASE_OWNER
    case_owner_label = f"{case_owner_key}@OData.Community.Display.V1.FormattedValue"

    def pick(row, configured, keywords):
        """Use the configured column if present, else the best-matching one."""
        if configured in row and row.get(configured) is not None:
            return row.get(configured)
        for key in row:
            if key.startswith("@") or "@OData" in key:
                continue
            low = key.lower()
            if any(k in low for k in keywords):
                return row.get(key)
        return None

    def sla_due(row):
        """First SLA column that actually has a value on this record."""
        for column in config.SLA_COLUMNS:
            value = row.get(column)
            if value:
                return _parse(value), column
        return None, None

    records = []
    for row in rows:
        due, due_source = sla_due(row)
        minutes = None if due is None else (due - now).total_seconds() / 60.0
        case_number = pick(
            row,
            config.COL_CASE_NUMBER,
            ["ticketnumber", "casenumber", "_name", "number"],
        ) or "(no case number)"
        sla_met = row.get(config.COL_SLA_MET) is True or str(
            row.get(config.COL_SLA_MET) or ""
        ).strip().lower() in ("1", "true", "yes")
        sla_state = (
            "Met"
            if sla_met or str(case_number) in config.SLA_MET_CASE_NUMBERS
            else row.get(config.COL_SLA_STATE) or "No SLA state"
        )
        if minutes is None:
            status = "No SLA set"
        elif minutes < 0:
            status = "Breached"
        elif minutes <= config.WARN_MINUTES:
            status = "Expiring soon"
        else:
            status = "On track"
        if str(sla_state).strip().lower() in (
            "achieved",
            "met",
            "complete",
            "completed",
        ):
            status = "Met"

        records.append(
            {
                "id": row.get(config.COL_ID),
                "case_number": case_number,
                "case_owner": row.get(case_owner_label) or row.get(case_owner_key) or "(unassigned)",
                "sla_due_utc": due.isoformat() if due else None,
                "sla_source": due_source,
                "sla_state": sla_state,
                "country": _display(row, config.COL_COUNTRY) or "-",
                "product": _display(row, config.COL_PRODUCT) or "-",
                "pod": _display(row, config.COL_POD) or "-",
                "support_region": _display(row, config.COL_SUPPORT_REGION) or "-",
                "transfer_reason": _display(row, config.COL_TRANSFER_REASON) or "-",
                "transfer_reason_description": row.get(
                    config.COL_TRANSFER_REASON_DESCRIPTION
                ) or "-",
                "active_status": row.get(config.COL_ACTIVE_STATUS) or "-",
                "sr_status": row.get(config.COL_STATUS_CODE),
                "modified_on": row.get(config.SYNC_WATERMARK_COLUMN),
                "minutes_remaining": None if minutes is None else round(minutes, 1),
                "status": status,
            }
        )

    order = {
        "Breached": 0,
        "Expiring soon": 1,
        "No SLA set": 2,
        "On track": 3,
        "Met": 4,
    }
    records.sort(
        key=lambda r: (
            order[r["status"]],
            r["minutes_remaining"] if r["minutes_remaining"] is not None else 10**9,
        )
    )
    return records
