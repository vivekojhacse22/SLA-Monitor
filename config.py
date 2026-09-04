"""Configuration for the SLA Monitor.

Every value can be overridden with an environment variable, so nothing
sensitive has to live in this file. Copy .env.example to .env and fill it in.
"""

import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # dotenv is optional
    pass


def _env(name, default=""):
    return os.environ.get(name, default).strip()


# --- Dataverse environment ------------------------------------------------
# From your link: https://sbamanager.crm.dynamics.com
DATAVERSE_URL = _env("DATAVERSE_URL", "https://sbamanager.crm.dynamics.com").rstrip("/")

# --- Authentication -------------------------------------------------------
# AUTH_MODE = "device"  -> you sign in once in the browser with your own account
# AUTH_MODE = "app"     -> unattended, uses an app registration's client secret
# "auto"   -> tries your Windows account, then a browser pop-up, then a device code
# "device" -> device code only
# "app"    -> unattended, uses an app registration's client secret
AUTH_MODE = _env("AUTH_MODE", "auto").lower()

TENANT_ID = _env("TENANT_ID", "72f988bf-86f1-41af-91ab-2d7cd011db47")  # Microsoft tenant

# Which account to sign in with (used to pick the right cached account).
LOGIN_HINT = _env("LOGIN_HINT", "viojha@microsoft.com")
CLIENT_ID = _env("CLIENT_ID", "04b07795-8ddb-461a-bbee-02f9e1bf7b46")  # Azure CLI public client (has a localhost redirect, so the browser pop-up works)
CLIENT_SECRET = _env("CLIENT_SECRET")

TOKEN_CACHE_FILE = _env("TOKEN_CACHE_FILE", ".token_cache.json")

# --- Table + column mapping ----------------------------------------------
# The entity from your view is crmee_service_request; its Web API entity set
# name is normally the plural form below. Run  python discover.py  if the app
# reports an unknown column or entity set - it prints the real names.
ENTITY_SET = _env("ENTITY_SET", "crmee_service_requests")

COL_ID = _env("COL_ID", "crmee_service_requestid")
COL_CASE_URL = _env("COL_CASE_URL", "crmee_active_system_uri")
COL_CASE_NUMBER = _env("COL_CASE_NUMBER", "crmee_sr_number")
COL_OWNER = _env("COL_OWNER", "_ownerid_value")
COL_CASE_OWNER = _env("COL_CASE_OWNER", "_crmee_case_owner_value")
# Comma-separated, in priority order: the first one with a value on a record wins.
COL_SLA_DUE = _env("COL_SLA_DUE", "crmee_sla_expired_date,crmee_sr_initialresponse")
SLA_COLUMNS = [c.strip() for c in COL_SLA_DUE.split(",") if c.strip()]
COL_SLA_START = _env("COL_SLA_START", "crmee_sla_start_date")
COL_SLA_STATE = _env("COL_SLA_STATE", "crmee_sla_state")
COL_SLA_MET = _env("COL_SLA_MET", "crmee_sla_met")
SLA_MET_CASE_NUMBERS = {
    value.strip()
    for value in _env("SLA_MET_CASE_NUMBERS").split(",")
    if value.strip()
}
COL_STATUS_CODE = _env("COL_STATUS_CODE", "crmee_sr_status_code")
COL_STATE = _env("COL_STATE", "statecode")
COL_ACTIVE_STATUS = _env("COL_ACTIVE_STATUS", "crmee_sr_active_system_status")
COL_TRANSFER_REASON = _env("COL_TRANSFER_REASON", "_crmee_casetransferreason_value")
COL_TRANSFER_REASON_DESCRIPTION = _env(
    "COL_TRANSFER_REASON_DESCRIPTION", "crmee_casetransferreasondescription"
)

# OData filter applied on top of everything else (active rows only by default)
EXTRA_FILTER = _env(
    "EXTRA_FILTER", "statecode eq 0 and crmee_sr_status_code ne 'Closed'"
)

# Row filters matched against the displayed names of lookup fields.
# Leave any value blank to disable that filter.
COUNTRY_EQUALS = _env("COUNTRY_EQUALS")
SUPPORT_REGION_EQUALS = _env("SUPPORT_REGION_EQUALS")
SUPPORT_REGION_CONTAINS = _env("SUPPORT_REGION_CONTAINS")
SUPPORT_POD_CONTAINS = _env("SUPPORT_POD_CONTAINS")
PRODUCT_EQUALS = _env("PRODUCT_EQUALS")

COL_COUNTRY = _env("COL_COUNTRY", "_crmee_sr_country_value")
COL_SUPPORT_REGION = _env("COL_SUPPORT_REGION", "_crmee_support_region_value")
COL_PRODUCT = _env("COL_PRODUCT", "_crmee_product_name_value")
COL_POD = _env("COL_POD", "_crmee_supportpod_value")

# Set to "true" to show only service requests owned by OWNER_EMAIL.
MINE_ONLY = _env("MINE_ONLY", "false").lower() in ("1", "true", "yes")
OWNER_EMAIL = _env("OWNER_EMAIL", "viojha@microsoft.com")

# --- SLA thresholds -------------------------------------------------------
# Anything due within this many minutes is "about to expire".
WARN_MINUTES = int(_env("WARN_MINUTES", "60") or 60)
# Refresh cadence of the live dashboard, in seconds.
REFRESH_SECONDS = int(_env("REFRESH_SECONDS", "60") or 60)
# Max rows pulled per refresh.
PAGE_SIZE = int(_env("PAGE_SIZE", "500") or 500)

# --- Microsoft Teams ------------------------------------------------------
TEAMS_NOTIFICATIONS_ENABLED = _env(
    "TEAMS_NOTIFICATIONS_ENABLED", "true"
).lower() in ("1", "true", "yes")
TEAMS_WEBHOOK_URL = _env("TEAMS_WEBHOOK_URL")

# --- Local DB (hot cache) -------------------------------------------------
# Cases pending SLA live here between runs, along with per-ruleset notification
# state and a cached copy of the business/ruleset configuration.
LOCAL_DB_PATH = _env("LOCAL_DB_PATH", "sla_cache.db")
# How long a delivered-alert fingerprint is kept before it is pruned.
NOTIFICATION_RETENTION_DAYS = int(_env("NOTIFICATION_RETENTION_DAYS", "7") or 7)

# --- Incremental sync -----------------------------------------------------
# Column used as the incremental watermark.
SYNC_WATERMARK_COLUMN = _env("SYNC_WATERMARK_COLUMN", "modifiedon")
# Rewind the watermark slightly so a row modified mid-query is never skipped.
SYNC_OVERLAP_SECONDS = int(_env("SYNC_OVERLAP_SECONDS", "120") or 120)
# A case can breach its SLA without being modified, so the cache is fully
# rebuilt on this cadence regardless of the watermark.
FULL_SYNC_EVERY_MINUTES = int(_env("FULL_SYNC_EVERY_MINUTES", "30") or 30)

# --- Business / ruleset fan-out -------------------------------------------
RULESETS_FILE = _env("RULESETS_FILE", "rulesets.json")

# --- Web server -----------------------------------------------------------
HOST = _env("HOST", "127.0.0.1")
PORT = int(_env("PORT", "8081") or 8081)
