# SLA Monitor

A local Flask dashboard for filtered Dataverse service requests. It refreshes the local SQLite cache on a schedule, shows SLA status in the browser, and sends alert cards to Microsoft Teams through a Power Automate webhook.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Fill in `.env` with your Dataverse sign-in details and Teams webhook. Keep `.env`, `.token_cache.json`, and `sla_cache.db` local; they are excluded from Git.

Start the dashboard:

```powershell
python app.py
```

Open http://127.0.0.1:8082.

## Filters

The default example is limited to:

```dotenv
COUNTRY_EQUALS=India
SUPPORT_REGION_EQUALS=DTP DP Integration - India
```

Clear either value to disable that filter. `SUPPORT_POD_CONTAINS` is optional.

## Notifications

Set `TEAMS_NOTIFICATIONS_ENABLED=true` and provide `TEAMS_WEBHOOK_URL`. The rules in `rulesets.json` control alert thresholds and whether pending or met cases are sent. The webhook flow must be enabled and configured to post to the intended Teams channel.

## Runtime files

- `app.py` starts the browser dashboard and refresh worker.
- `config.py` loads environment settings.
- `dataverse.py` signs in, queries, classifies, and times service requests.
- `sync.py` applies filters and refreshes the cache.
- `localdb.py` stores cases and notification state in SQLite.
- `rulesets.py`, `runner.py`, `alert_selectors.py`, and `alerting.py` select and deduplicate alerts.
- `teams.py` builds and sends Teams cards.
- `templates/index.html` is the dashboard page.
- `rulesets.json` contains alert thresholds.
