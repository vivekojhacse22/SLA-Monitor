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
    legacy_namespaces=(),
):
    selector = selector or teams.select_due_soon_pending
    eligible = selector(records, max_minutes=max_minutes)
    pending = []
    claims = []
    skipped_duplicate = 0
    for record in eligible:
        fingerprint = alert_fingerprint(record, namespace)
        case_number = str(record.get("case_number") or "")
        if any(
            state.was_sent(alert_fingerprint(record, legacy_namespace))
            for legacy_namespace in legacy_namespaces
        ):
            state.mark_sent(fingerprint, case_number)
            skipped_duplicate += 1
            continue
        if state.claim_notification(fingerprint, case_number):
            pending.append(record)
            claims.append(fingerprint)
        else:
            skipped_duplicate += 1

    if not pending:
        return AlertRunResult(len(eligible), 0, skipped_duplicate, 0)

    try:
        message_count = await sender(pending)
    except Exception:
        for fingerprint in claims:
            state.release_notification_claim(fingerprint)
        raise

    for record in pending:
        state.mark_sent(
            alert_fingerprint(record, namespace),
            str(record.get("case_number") or ""),
        )
    return AlertRunResult(
        eligible=len(eligible),
        sent=len(pending),
        skipped_duplicate=skipped_duplicate,
        messages=message_count,
    )