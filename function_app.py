"""Azure Functions host for Teams activities and scheduled SLA notifications."""

import asyncio
import logging

import azure.functions as func

import alerting
import config
import dataverse
import mailer
import pipeline
import teams_bot


logger = logging.getLogger(__name__)

# Teams registers /api/messages on the FastAPI application during initialization.
asyncio.run(teams_bot.initialize())
asgi_middleware = func.AsgiMiddleware(teams_bot.http_api)
function_app = func.FunctionApp()


@function_app.function_name(name="TeamsBotHttp")
@function_app.route(
    route="{*route}",
    methods=[func.HttpMethod.GET, func.HttpMethod.POST],
    auth_level=func.AuthLevel.ANONYMOUS,
)
async def teams_http(req: func.HttpRequest, context: func.Context):
    # The Teams SDK validates Bot Framework bearer tokens on /api/messages.
    return await asgi_middleware.handle_async(req, context)


async def _run_pipeline(force_full=False):
    """One architecture cycle: sync -> local hot cache -> ruleset fan-out."""
    return await pipeline.run_cycle(force_full=force_full)


@function_app.function_name(name="SlaAlertTimer")
@function_app.timer_trigger(
    schedule=config.TEAMS_ALERT_SCHEDULE,
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
async def sla_alert_timer(timer: func.TimerRequest):
    if not (
        config.TEAMS_NOTIFICATIONS_ENABLED or config.EMAIL_NOTIFICATIONS_ENABLED
    ):
        logger.info("SLA notifications are disabled")
        return

    summary = await _run_pipeline()
    logger.info(
        "SLA cycle complete: pulled=%s cached=%s purged=%s cache=%s runs=%s sent=%s failures=%s",
        summary["sync"]["pulled"],
        summary["sync"]["cached"],
        summary["sync"]["purged"],
        summary["cache_size"],
        summary["fanout"]["runs"],
        summary["fanout"]["sent"],
        summary["fanout"]["failures"],
    )
    for run in summary["fanout"]["results"]:
        if run["outcome"] not in ("ok", "skipped"):
            logger.error(
                "Ruleset %s finished as %s after %sms: %s",
                run["ruleset_id"],
                run["outcome"],
                run["duration_ms"],
                run["detail"],
            )

    logger.info("SLA timer complete: past_due=%s", timer.past_due)
