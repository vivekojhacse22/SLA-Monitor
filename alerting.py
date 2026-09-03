"""Orchestrate deduplicated SLA notifications independently of the host."""

import hashlib
import json
from dataclasses import dataclass

import teams


@dataclass(frozen=True)
class AlertRunResult:
    eligible: int
    sent: int
    skipped_duplicate: int
    messages: int


def alert_fingerprint(record, namespace="default"):
    identity = {
        "id": record.get("id") or record.get("case_number"),
        "sla_due_utc": record.get("sla_due_utc"),
    }
    if namespace != "default":
        identity["namespace"] = namespace
    serialized = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


async def run_alert_cycle(
    records,
    state,
    sender,
    max_minutes=60,
    selector=None,
    namespace="default",
):
    selector = selector or teams.select_due_soon_pending
    eligible = selector(records, max_minutes=max_minutes)
    pending = [
        record
        for record in eligible
        if not state.was_sent(alert_fingerprint(record, namespace))
    ]
    if not pending:
        return AlertRunResult(len(eligible), 0, len(eligible), 0)

    message_count = await sender(pending)
    for record in pending:
        state.mark_sent(
            alert_fingerprint(record, namespace),
            str(record.get("case_number") or ""),
        )
    return AlertRunResult(
        eligible=len(eligible),
        sent=len(pending),
        skipped_duplicate=len(eligible) - len(pending),
        messages=message_count,
    )