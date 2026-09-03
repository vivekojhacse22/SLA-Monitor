# Service Request SLA Monitor

A small Python web app that reads your **Active Service Requests** (the `crmee_service_request`
table in `sbamanager.crm.dynamics.com`) and shows, in a self-refreshing dashboard:

- **Case number**
- **Owner name**
- **SLA due time**, plus time remaining / how long it is overdue
- **Status** — SLA missed (breached), about to expire, on track, or no SLA set

The page refreshes itself on a timer (60 seconds by default), so it stays live on a screen.
Production Teams notifications run separately as an Azure Functions timer and are delivered by
an authenticated Teams SDK v2 bot. The Flask dashboard no longer sends a startup notification.
The same timer can send deduplicated warning and missed-SLA emails through Microsoft Graph.

## Architecture

```
  SBAManager (Dataverse)
        |
        |  one query, dynamically filtered to cover every business in one go
        v
  Incremental Sync & Orchestrator          sync.py
        |                \
        |                 \  classify + label each case with its business
        |                  v
        |            Local DB (hot cache)  localdb.py
        |            - all active cases, deleted once they leave the active query
        |            - per-ruleset notification state
        |            - the business / ruleset configuration JSON
        v
  Concurrent runs, one per ruleset         runner.py
  up to 50 at a time, 20s budget each

   +-- ...
        |
        v
  Microsoft Teams notifications (and email) teams.py / mailer.py
```

`pipeline.py` runs the whole chain. Trigger it three ways:

- `python pipeline.py` — one cycle from the command line, printing a JSON summary
- `python app.py` — the dashboard, which runs a cycle in the background every `REFRESH_SECONDS`
- the `SlaAlertTimer` in `function_app.py` — unattended, on `TEAMS_ALERT_SCHEDULE`

### One pull for every business

`sync.build_filter` turns each business's `match` block into an OData clause and ORs them
together, so adding a business costs no extra round trip. Criteria on lookup fields
(`country`, `support_region`, `pod`) are pushed down to Dataverse; anything else is matched
locally after the pull, with the same end result.

### Incremental, with a safety net

Each cycle filters on `modifiedon gt <watermark>` and rewinds the watermark by
`SYNC_OVERLAP_SECONDS` (120s) so a row changed mid-query is never skipped. Because a case can
breach its SLA without being modified, the cache is fully rebuilt every
`FULL_SYNC_EVERY_MINUTES` (30) — and only a full pull is allowed to delete, so an incremental
run can never empty the dashboard.

### The local hot cache

`sla_cache.db` (SQLite, WAL mode) holds all cases returned by the active-service-request query.
Cases are deleted on the next full refresh after they leave that query. The dashboard reads this file instead of querying
Dynamics on every page refresh, which is what keeps the page fast as businesses are added.

### Concurrency and isolation

Every enabled ruleset is its own asynchronous run, bounded by a semaphore at
`concurrency.max_concurrent_runs` (50) and cut off at `concurrency.run_timeout_seconds` (20).
A ruleset that fails or overruns is logged to the `run_log` table and shown on the dashboard —
it can never delay or break the others. Each ruleset de-duplicates in its own namespace, so two
rulesets may both alert on the same case, but neither will alert twice.

## Configuring businesses and rulesets

`rulesets.json` is the only file you edit to add a business or a ruleset. No code changes.

```json
{
  "concurrency": { "max_concurrent_runs": 50, "run_timeout_seconds": 20 },
  "businesses": [
    {
      "name": "DTP-DP-Integration",
      "match": { "country": ["India"] },
      "rulesets": [
        {
          "id": "azure-integraion",
          "name": "integration / Ruleset EMEA",
          "warn_minutes": 60,
          "alert_on": ["pending", "missed"],
          "match": { "support_region_contains": ["India"] },
          "notify": {
            "teams_chat_id": "",
            "teams_webhook_url": "",
            "email_recipients": []
          }
        }
      ]
    }
  ]
}
```

| Key | Meaning |
|-----|---------|
| `match` | Fields to match on: `country`, `support_region`, `pod`, `case_owner`, `active_status`, `sr_status`, `sla_state`, `transfer_reason`, `status`. Add `_contains` for a substring match. An empty `match` matches everything in the business. |
| `warn_minutes` | How close to the deadline this ruleset alerts on. Each ruleset has its own threshold. |
| `alert_on` | `pending` (due within `warn_minutes`), `missed`, or both. |
| `notify` | Per-ruleset destinations. Anything left blank falls back to the global `.env` settings. |
| `enabled` | Set `false` on a business or ruleset to park it without deleting it. |

Bad configuration fails loudly at load: unknown match fields, duplicate ruleset ids, and invalid
`alert_on` values are all rejected with a message naming the offending entry. The last valid
configuration is cached in the local DB, so a cycle can still start if the file is briefly
unavailable.

## What the dashboard shows

Alongside the case table, the page now lists every business and ruleset with the number of
cases it currently matches, its warn threshold, the outcome of its last run (`ok`, `skipped`,
`timeout`, `error`), how long that run took, and how many alerts it sent. `POST /api/cycle`
forces a full cycle immediately instead of waiting for the timer.

## Tests

```bash
python -m unittest discover -s tests -t .
```

The suite runs fully offline — Dataverse and the notification senders are stubbed — and covers
the one-query filter builder, watermark advance and overlap, cache purge rules, per-ruleset
routing, de-duplication across cycles, concurrency bounds, and the timeout and failure
isolation guarantees.

## Files

| File | What it does |
|------|--------------|
| `pipeline.py` | Runs the whole cycle: sync -> local cache -> ruleset fan-out |
| `sync.py` | Incremental Sync & Orchestrator - one Dataverse pull for all businesses |
| `localdb.py` | Local DB: hot cache of pending cases, notification state, cached config |
| `rulesets.py` | Loads and validates `rulesets.json` |
| `rulesets.json` | Business and ruleset configuration |
| `runner.py` | Concurrent per-ruleset execution with a per-run deadline |
| `app.py` | Flask web server + the `/api/service-requests` JSON feed |
| `dataverse.py` | Sign-in and the Dataverse Web API query, plus the SLA classification |
| `teams.py` | Selects pending SLA cases and builds Adaptive Card payloads |
| `teams_bot.py` | Authenticated Teams endpoint and proactive channel sender |
| `function_app.py` | Azure Functions HTTP and five-minute timer triggers |
| `notification_state.py` | Azure Table Storage destination and deduplication state |
| `alerting.py` | Host-independent scheduled alert orchestration |
| `infra/main.bicep` | Function, storage, monitoring, and Azure Bot infrastructure |
| `teams-manifest/manifest.json` | Teams app manifest template for channel installation |
| `config.py` | All settings, each overridable by an environment variable |
| `discover.py` | Prints the real table / column names from your environment |
| `templates/index.html` | The auto-refreshing dashboard page |
| `.env.example` | Template for your local settings |
| `requirements.txt` | Python dependencies |

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # then edit .env
python app.py
```

Open <http://127.0.0.1:8081>. On first run with `AUTH_MODE=device` the console prints a code and
a URL — sign in with your work account once, and the token is cached for later runs.

## Production Teams notifications

The production path does not use an incoming webhook, Teams Workflow, or delegated Microsoft
Graph token. A single-tenant Teams bot receives authenticated activities at `/api/messages` and
the timer trigger checks Dataverse every five minutes. Azure Table Storage retains the selected
conversation and a fingerprint of each successfully delivered case/SLA deadline pair.

Only records with SLA State `Pending` and `0 <= minutes_remaining <= WARN_MINUTES` are sent.
Failed sends remain unmarked and are retried on the next timer run. A changed SLA deadline gets a
new fingerprint and can alert again. If more than one channel is activated, set
`TEAMS_TARGET_CONVERSATION_ID` on the Function App to make the destination explicit.

### Prerequisites

1. Create a single-tenant Entra app registration for the bot and create a client secret. No
   Microsoft Graph chat permissions are required.
2. Create a separate Entra application user in Dataverse with read access to the service-request
   table. Its client ID and secret populate `CLIENT_ID` and `CLIENT_SECRET`.
3. Install Azure CLI, Azure Functions Core Tools v4, Python 3.11, and Azurite for local testing.
4. Ensure your Teams administrator allows custom app upload or publishes the package centrally.

The bot and Dataverse credentials must remain separate: `TEAMS_BOT_CLIENT_ID` and
`TEAMS_BOT_CLIENT_SECRET` authenticate Teams, while `CLIENT_ID` and `CLIENT_SECRET` authenticate
Dataverse.

### Provision Azure

Copy `infra/main.bicepparam.example` to `infra/main.bicepparam`, fill in the four application
values, and keep that file out of source control. Then run:

```powershell
$resourceGroupName = "rg-sla-monitor-prod"
$location = "eastus2"
az group create --name $resourceGroupName --location $location
az deployment group validate --resource-group $resourceGroupName --template-file infra/main.bicep --parameters infra/main.bicepparam
az deployment group create --name sla-monitor --resource-group $resourceGroupName --template-file infra/main.bicep --parameters infra/main.bicepparam
$functionAppName = az deployment group show --name sla-monitor --resource-group $resourceGroupName --query properties.outputs.functionAppName.value -o tsv
```

The template creates a Python 3.11 Flex Consumption Function App, Storage account, managed-identity
Blob/Table role assignments, Log Analytics, Application Insights, Azure Bot resource, and Teams
channel. The Function HTTP route is anonymous at the Azure Functions layer because Bot Framework
must reach it; Teams SDK bearer-token validation protects `/api/messages`.

### Publish the Functions code

Build a deployment archive without local settings, caches, or the virtual environment:

```powershell
$files = Get-ChildItem -Force | Where-Object { $_.Name -notin @('.git', '.venv', '.env', 'infra', 'teams-manifest', 'local.settings.json', 'function.zip') }
Compress-Archive -Path $files.FullName -DestinationPath function.zip -Force
az functionapp deployment source config-zip --resource-group $resourceGroupName --name $functionAppName --src function.zip
```

Confirm `https://<function-host>/health` returns a healthy response and review the `SlaAlertTimer`
traces in Application Insights.

### Package and install the Teams app

1. Replace `${TEAMS_BOT_CLIENT_ID}` and `${FUNCTION_HOST_NAME}` in
   `teams-manifest/manifest.json` with the deployment outputs.
2. Zip `manifest.json`, `outline.png`, and `color.png` at the root of the archive, upload the
   package to Teams, and add it to the
   target team.
3. In the channel thread that should receive notifications, mention the bot and send
   `activate alerts`. The bot confirms activation and the next timer cycle uses that destination.

For local Functions development, copy `local.settings.example.json` to `local.settings.json`, use
Azurite for `AzureWebJobsStorage`, install dependencies into a Python 3.11 virtual environment,
and run `func start`. Bot activities still require a public HTTPS tunnel whose URL is configured
as the Azure Bot messaging endpoint.

## Automatic email notifications

Create a dedicated single-tenant Entra app registration for email delivery, add the Microsoft
Graph **Mail.Send application** permission, and grant tenant admin consent. For least privilege,
use Exchange Online application access policy/RBAC for Applications to restrict that app to the
sender mailbox. The sender must be an Exchange Online mailbox.

Set `EMAIL_NOTIFICATIONS_ENABLED=true`, `EMAIL_TENANT_ID`, `EMAIL_CLIENT_ID`,
`EMAIL_CLIENT_SECRET`, `EMAIL_SENDER`, and comma-separated `EMAIL_RECIPIENTS`. The five-minute
Function timer sends one email when pending cases enter the warning window and another if those
cases later become missed. Delivery fingerprints are stored separately from Teams notifications;
failed sends are retried on the next timer run.

## Verified configuration

These were read directly from `sbamanager.crm.dynamics.com`, so they are no longer guesses:

| Setting | Value |
|---------|-------|
| Table (entity set) | `crmee_service_requests` |
| Case number | `crmee_sr_number` |
| SLA deadline | `crmee_sla_expired_date`, falling back to `crmee_sr_initialresponse` |
| SLA start | `crmee_sla_start_date` |
| Owner | `ownerid` (read as `_ownerid_value` with its display name) |
| Active filter | `statecode eq 0 and crmee_sr_status_code ne 'Closed'` — the same filter the "Active Service Requests" view uses |

Note: on the sample records checked, `crmee_sla_expired_date` was empty. If your whole list shows
"No SLA set", the SLA clock is likely tracked in a different column for your queue — the banner on
the page will say so, and `diagnose.py` prints every available column so you can point
`COL_SLA_DUE` at the right one.

The app no longer sorts server-side on the SLA column (that query was very slow on this table);
it sorts by urgency in the browser instead.

## Sign-in

`AUTH_MODE=auto` (the default) tries, in order: a cached token → your existing Windows account /
a browser sign-in window → a device code as a last resort. The device-code route is what gets
re-challenged when your sign-in location looks unusual, so `auto` avoids it where possible.

Sign-in now happens **before** the web server starts, so the terminal will not leave you looking
at a blank page while it quietly waits for a code.

## Row filters

Set in `.env`, applied to the displayed names so they work regardless of how the lookups are stored:

- `COUNTRY_EQUALS=India` — exact match on the service request country
- `SUPPORT_REGION_EQUALS=DTP DP Integration - India` — exact match on Support Region
- `SUPPORT_REGION_CONTAINS=Integration` — Support Region contains this text
- `SUPPORT_POD_CONTAINS=- Integration` — Support POD contains this text

Clear either value to turn that filter off. The dashboard banner reports how many rows each
filter kept, so you can see immediately if a filter is what emptied the list.

## If the dashboard is blank

Run the diagnostics first — it checks each stage in order and tells you which one failed:

```bash
python diagnose.py
```

It verifies, in this order: sign-in → reaching the environment → the table exists → reading
3 rows with no filter and no column list (and printing every column name it finds) → whether
your configured columns exist → whether your filter matches anything.

The three usual causes:

1. **Wrong column names** — `crmee_name` / `crmee_sladuedate` are educated guesses. Step 4 of
   the diagnostics prints the real ones; put them in `.env`.
2. **The filter excludes everything** — `EXTRA_FILTER=statecode eq 0` assumes active rows use
   state 0. Clear `EXTRA_FILTER` in `.env` to see all rows.
3. **Permissions** — the Power Apps view may show rows your security role cannot read through
   the API. Step 4 returning 0 rows with no filter points at this.

The app now also self-corrects where it can: if your columns are rejected it retries asking for
all columns, matches case-number/SLA fields by name pattern, and shows a warning banner on the
page explaining what it did.

## Confirming the column names

I could not read your Dynamics environment from here, so the column names in `config.py`
(`crmee_name`, `crmee_sladuedate`, `_ownerid_value`) are the standard naming pattern for that
table — they are **not verified against your environment**. If the app reports an unknown
property, run:

```bash
python discover.py
```

It lists the table's real entity-set name and its candidate case-number, SLA/date, and owner
columns. Put the right ones in `.env` (`COL_CASE_NUMBER`, `COL_SLA_DUE`, `COL_OWNER`) and
restart — no code changes needed.

If your SLA lives in the standard Dataverse SLA KPI instances rather than a column on the
record, set `COL_SLA_DUE` to whichever date column your team tracks (for example a
"response by" or "resolve by" field) — the dashboard logic is the same.

## Tuning

Set these in `.env`:

- `WARN_MINUTES` — how close to the deadline counts as "about to expire" (default 60)
- `REFRESH_SECONDS` — dashboard refresh cadence (default 60)
- `EXTRA_FILTER` — any extra OData filter, e.g. `statecode eq 0 and _ownerid_value eq <guid>`
  to scope it to one engineer or queue

## Unattended / always-on

The Azure Functions deployment always uses `AUTH_MODE=app`. The local Flask dashboard can continue
using interactive authentication, but it is no longer responsible for Teams delivery.
