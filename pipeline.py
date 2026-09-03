"""The whole cycle, in the order the architecture diagram reads.

    SBAManager (Dataverse)
        -> Incremental Sync & Orchestrator   (sync.py, one pull for all businesses)
        -> Local DB                          (localdb.py, hot cache + state + config)
        -> Concurrent runs, one per ruleset  (runner.py, <=50 at a time, 20s each)
        -> Microsoft Teams / email notifications

Run it once from the command line:      python pipeline.py
Run it on a timer in Azure Functions:   function_app.py calls run_cycle().
"""

import asyncio
import json
from datetime import datetime, timezone

import config
import dataverse
import localdb
import mailer
import rulesets
import runner
import teams

_store = None


def get_store():
    global _store
    if _store is None:
        _store = localdb.LocalStore(config.LOCAL_DB_PATH)
    return _store


def build_senders(teams_sender=None, email_sender=None):
    """Notification callables, one per alert kind, honouring each ruleset.

    Each sender receives (ruleset, records) so a ruleset can target its own
    Teams chat, its own webhook, and its own email recipients. Destinations
    left blank on a ruleset fall back to the global settings in .env.
    """

    def send_teams(ruleset, records):
        chat_id = ruleset.teams_chat_id or config.TEAMS_CHAT_ID
        webhook = ruleset.teams_webhook_url or config.TEAMS_WEBHOOK_URL
        if chat_id and config.GRAPH_CLIENT_ID:
            token = teams.get_graph_token(
                config.TENANT_ID,
                config.GRAPH_CLIENT_ID,
                config.LOGIN_HINT,
                config.TOKEN_CACHE_FILE,
            )
            return teams.post_pending_cases_to_chat(
                token, chat_id, records, max_minutes=ruleset.warn_minutes
            )
        if webhook:
            return teams.post_pending_cases(
                webhook, records, max_minutes=ruleset.warn_minutes
            )
        raise teams.TeamsNotificationError(
            f"Ruleset '{ruleset.id}' has no Teams chat or webhook to post to."
        )

    def send_email(ruleset, records):
        recipients = list(ruleset.email_recipients) or config.EMAIL_RECIPIENTS
        return mailer.send_alert_email(
            records,
            config.EMAIL_TENANT_ID,
            config.EMAIL_CLIENT_ID,
            config.EMAIL_CLIENT_SECRET,
            config.EMAIL_SENDER,
            recipients,
        )

    async def dispatch(ruleset, records):
        count = 0
        if config.TEAMS_NOTIFICATIONS_ENABLED:
            count += await asyncio.to_thread(
                teams_sender or send_teams, ruleset, records
            )
        if config.EMAIL_NOTIFICATIONS_ENABLED:
            count += await asyncio.to_thread(
                email_sender or send_email, ruleset, records
            )
        return count

    return {"pending": dispatch, "missed": dispatch, "met": dispatch}


async def run_cycle(token=None, store=None, ruleset_config=None, senders=None,
                    force_full=False):
    """One full pass: sync -> cache -> concurrent ruleset fan-out."""
    import sync  # imported here so tests can stub the network layer

    store = store or get_store()
    ruleset_config = ruleset_config or rulesets.load(store=store)
    token = token or dataverse.get_token()

    sync_result = await asyncio.to_thread(
        sync.run, token, store, ruleset_config, None, force_full
    )
    records = dataverse.refresh_timing(store.pending_cases())
    cycle = await runner.run_cycle(
        ruleset_config, records, store, senders or build_senders()
    )
    store.prune_notifications(config.NOTIFICATION_RETENTION_DAYS)

    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "sync": {
            "pulled": sync_result.pulled,
            "cached": sync_result.cached,
            "purged": sync_result.purged,
            "full_refresh": sync_result.full_refresh,
            "duration_ms": sync_result.duration_ms,
            "notes": list(sync_result.notes),
        },
        "cache_size": store.count_cases(),
        "fanout": cycle.as_dict(),
    }


def main():
    summary = asyncio.run(run_cycle(force_full=True))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
