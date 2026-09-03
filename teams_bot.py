"""Microsoft Teams SDK v2 proactive notification bot."""

import asyncio
import logging

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from microsoft_teams.api import InstalledActivity, MessageActivity
from microsoft_teams.apps import ActivityContext, App, FastAPIAdapter
from microsoft_teams.cards import AdaptiveCard

import config
import teams
from notification_state import create_notification_state


logger = logging.getLogger(__name__)
http_api = FastAPI(title="SLA Teams notification bot")
http_adapter = FastAPIAdapter(app=http_api)
notification_state = create_notification_state(
    config.TEAMS_STATE_TABLE, config.TABLE_STORAGE_ENDPOINT
)
teams_app = App(
    client_id=config.TEAMS_BOT_CLIENT_ID,
    client_secret=config.TEAMS_BOT_CLIENT_SECRET,
    tenant_id=config.TENANT_ID,
    http_server_adapter=http_adapter,
)


def _activity_location(activity):
    team = activity.team
    channel = activity.channel
    return (
        getattr(team, "id", "") if team else "",
        getattr(channel, "id", "") if channel else "",
    )


async def _save_destination(activity):
    team_id, channel_id = _activity_location(activity)
    await asyncio.to_thread(
        notification_state.save_conversation,
        activity.conversation.id,
        activity.service_url or "",
        team_id,
        channel_id,
    )


@teams_app.on_install_add
async def handle_install(ctx: ActivityContext[InstalledActivity]) -> None:
    await ctx.send(
        "SLA notifications are installed. In the channel thread that should receive "
        "alerts, mention me and send `activate alerts`."
    )


@teams_app.on_message
async def handle_message(ctx: ActivityContext[MessageActivity]) -> None:
    text = (ctx.activity.text or "").lower()
    if "activate alerts" not in text:
        await ctx.send("Mention me with `activate alerts` to select this alert thread.")
        return

    if ctx.activity.conversation.conversation_type != "channel":
        await ctx.send("SLA alerts must be activated from a Teams channel.")
        return

    await _save_destination(ctx.activity)
    await ctx.send(
        "SLA alerts are active in this thread. New pending cases entering the warning "
        f"window ({config.WARN_MINUTES} minutes) will be posted once."
    )


@http_api.get("/health")
async def health():
    return {"status": "healthy", "service": "sla-teams-notification-bot"}


@http_api.get("/privacy", response_class=PlainTextResponse)
async def privacy():
    return "This app stores Teams conversation identifiers only for SLA alert delivery."


@http_api.get("/terms", response_class=PlainTextResponse)
async def terms():
    return "Use of this internal app is governed by your organization's policies."


async def initialize():
    await teams_app.initialize()


async def send_pending_cases(records):
    destination = await asyncio.to_thread(
        notification_state.get_destination, config.TEAMS_TARGET_CONVERSATION_ID
    )
    if not destination:
        raise teams.TeamsNotificationError(
            "No unique Teams destination is active. Mention the bot with "
            "'activate alerts' in the target channel, or set "
            "TEAMS_TARGET_CONVERSATION_ID."
        )

    payloads = teams.build_payloads(records)
    for payload in payloads:
        card_data = payload["attachments"][0]["content"]
        card = AdaptiveCard.model_validate(card_data)
        await _send_with_retry(
            destination["conversation_id"], card, destination.get("service_url", "")
        )
    return len(payloads)


async def _send_with_retry(conversation_id, card, service_url, attempts=3):
    for attempt in range(1, attempts + 1):
        try:
            return await teams_app.send(
                conversation_id, card, service_url=service_url or None
            )
        except Exception:
            if attempt == attempts:
                raise
            delay = 2 ** (attempt - 1)
            logger.warning(
                "Teams send attempt %s failed; retrying in %s seconds",
                attempt,
                delay,
                exc_info=True,
            )
            await asyncio.sleep(delay)