"""Incremental Sync & Orchestrator.

One Dataverse query pulls every configured business in a single go: each
business contributes an OR-branch to a dynamically built OData filter, so the
number of round trips stays at one no matter how many businesses are added.

The rows are then classified with the existing dataverse.classify logic,
labelled with the business they belong to, written into the local hot cache,
and any case that is no longer pending is deleted from it.

Incremental mode narrows the pull with a modifiedon watermark. Because a case
can breach its SLA without ever being modified, a periodic full refresh keeps
the cache honest - controlled by FULL_SYNC_EVERY_MINUTES.
"""

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import config
import dataverse


@dataclass(frozen=True)
class SyncResult:
    pulled: int
    cached: int
    purged: int
    full_refresh: bool
    watermark: str
    duration_ms: int
    notes: tuple = ()


def _odata_string(value):
    return str(value).replace("'", "''")


def _lookup_navigation(column):
    """_crmee_sr_country_value -> crmee_sr_country (the navigation property)."""
    name = column
    if name.startswith("_"):
        name = name[1:]
    if name.endswith("_value"):
        name = name[: -len("_value")]
    return name


_FIELD_TO_COLUMN = {
    "country": lambda: config.COL_COUNTRY,
    "product": lambda: config.COL_PRODUCT,
    "support_region": lambda: config.COL_SUPPORT_REGION,
    "pod": lambda: config.COL_POD,
}


def business_filter(business):
    """Turn one business's criteria into an OData clause, where possible.

    Only lookup fields that Dataverse can filter on server-side are pushed
    down. Anything else stays a local match, which the orchestrator applies
    after the pull - the result is identical, just less selective on the wire.
    """
    clauses = []
    for field_name, values in business.criteria.equals.items():
        column_getter = _FIELD_TO_COLUMN.get(field_name)
        if not column_getter or not values:
            continue
        navigation = _lookup_navigation(column_getter())
        branch = " or ".join(
            f"{navigation}/crmee_name eq '{_odata_string(value)}'" for value in values
        )
        clauses.append(f"({branch})")
    if not clauses:
        return ""
    return "(" + " and ".join(clauses) + ")"


def build_filter(ruleset_config, watermark="", base_filter=None):
    """Build the single filter that covers every business in one go."""
    base = config.EXTRA_FILTER if base_filter is None else base_filter
    parts = [base] if base else []

    if config.COUNTRY_EQUALS:
        navigation = _lookup_navigation(config.COL_COUNTRY)
        value = _odata_string(config.COUNTRY_EQUALS)
        parts.append(f"{navigation}/crmee_name eq '{value}'")

    if config.SUPPORT_REGION_EQUALS:
        navigation = _lookup_navigation(config.COL_SUPPORT_REGION)
        value = _odata_string(config.SUPPORT_REGION_EQUALS)
        parts.append(f"{navigation}/crmee_name eq '{value}'")

    if config.SUPPORT_POD_CONTAINS:
        navigation = _lookup_navigation(config.COL_POD)
        value = _odata_string(config.SUPPORT_POD_CONTAINS)
        parts.append(f"contains({navigation}/crmee_name,'{value}')")

    branches = [business_filter(b) for b in ruleset_config.enabled_businesses]
    branches = [branch for branch in branches if branch]
    if branches:
        # Deduplicate identical branches so two businesses in the same region
        # do not double the filter length.
        unique = list(dict.fromkeys(branches))
        parts.append("(" + " or ".join(unique) + ")" if len(unique) > 1 else unique[0])

    if watermark:
        parts.append(f"{config.SYNC_WATERMARK_COLUMN} gt {watermark}")

    return " and ".join(parts)


def fetch(token, ruleset_config, watermark=""):
    """Run the one-shot pull. Returns the raw rows and the notes collected."""
    select = dataverse.select_columns()
    url = f"{config.DATAVERSE_URL}/api/data/v9.2/{config.ENTITY_SET}"
    params = {"$select": select}
    odata_filter = build_filter(ruleset_config, watermark)
    if odata_filter:
        params["$filter"] = odata_filter

    dataverse.NOTES.clear()
    try:
        rows = dataverse._run(token, url, params)
    except dataverse.QueryError as exc:
        if watermark:
            # A rejected watermark must never silently empty the dashboard.
            dataverse.NOTES.append(
                "The incremental filter was rejected, so a full pull was used "
                f"instead. Original error: {exc}"
            )
            params.pop("$filter", None)
            base = build_filter(ruleset_config, "")
            if base:
                params["$filter"] = base
            rows = dataverse._run(token, url, params)
        else:
            raise
    return rows


def _due_for_full_refresh(store, now):
    last = store.get_watermark("last_full_sync")
    if not last:
        return True
    try:
        stamp = datetime.fromisoformat(last)
    except ValueError:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return now - stamp >= timedelta(minutes=config.FULL_SYNC_EVERY_MINUTES)


def run(token, store, ruleset_config, now=None, force_full=False):
    """Pull, classify, label by business, and refresh the local hot cache."""
    started = time.perf_counter()
    now = now or datetime.now(timezone.utc)

    full_refresh = force_full or _due_for_full_refresh(store, now)
    watermark = "" if full_refresh else store.get_watermark("dataverse")

    rows = fetch(token, ruleset_config, watermark)
    records = dataverse.classify(rows, now=now)

    # Cache every active case for the dashboard. Ruleset selectors decide
    # which records are actionable for each notification type.
    cached_records = records
    for record in cached_records:
        record["business"] = ruleset_config.business_for(record)

    cached = store.upsert_cases(cached_records)
    purged = 0
    if full_refresh:
        # Only a full pull knows the complete set of still-pending cases, so
        # only a full pull is allowed to delete.
        purged = store.purge_completed({str(r.get("id") or r.get("case_number") or "") for r in cached_records})
        store.set_watermark(now.isoformat(), "last_full_sync")

    # Overlap slightly so a record modified during the query is not skipped.
    next_watermark = (now - timedelta(seconds=config.SYNC_OVERLAP_SECONDS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    store.set_watermark(next_watermark, "dataverse")

    return SyncResult(
        pulled=len(rows),
        cached=cached,
        purged=purged,
        full_refresh=full_refresh,
        watermark=next_watermark,
        duration_ms=int((time.perf_counter() - started) * 1000),
        notes=tuple(dataverse.NOTES),
    )
