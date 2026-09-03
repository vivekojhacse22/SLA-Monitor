"""Post pending SLA case summaries to Microsoft Teams."""

import json

import requests


MAX_PAYLOAD_BYTES = 25_000


class TeamsNotificationError(RuntimeError):
    pass


def select_due_soon_pending(records, max_minutes=60):
    """Return Pending cases whose unexpired SLA is due within max_minutes."""
    return [
        record
        for record in records
        if record.get("sla_state") == "Pending"
        and record.get("minutes_remaining") is not None
        and 0 <= record["minutes_remaining"] <= max_minutes
    ]


def _remaining(record):
    minutes = record.get("minutes_remaining")
    if minutes is None:
        return "No SLA expiry time set"
    if minutes < 0:
        return f"Breached by {abs(minutes):.1f} minutes"
    if minutes < 60:
        return f"{minutes:.1f} minutes remaining"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{int(hours)}h {remaining_minutes:.0f}m remaining"


def _case_section(record):
    return {
        "type": "Container",
        "separator": True,
        "items": [
            {
                "type": "TextBlock",
                "text": str(record.get("case_number") or "(no case number)"),
                "weight": "Bolder",
                "wrap": True,
            },
            {
                "type": "FactSet",
                "facts": [
                    {
                        "title": "Owner",
                        "value": str(record.get("case_owner") or "(unassigned)"),
                    },
                    {"title": "SLA", "value": _remaining(record)},
                ],
            },
        ],
    }


def _is_met_batch(records):
    met_states = {"achieved", "met", "complete", "completed"}
    return bool(records) and all(
        str(record.get("sla_state") or "").strip().lower() in met_states
        for record in records
    )


def _alert_title(met, max_minutes=60):
    if met:
        return "SLA met with 1 minute or less remaining"
    if max_minutes == 60:
        return "Pending SLA cases due within 1 hour"
    return f"Pending SLA cases due within {max_minutes} minutes"


def build_payloads(records, max_minutes=60):
    """Build Teams MessageCard payloads, splitting before the webhook size limit."""
    if not records:
        return [_payload([], 0, 1)]

    batches = []
    sections = []
    batch_records = []
    total = len(records)
    for record in records:
        candidate = sections + [_case_section(record)]
        candidate_records = batch_records + [record]
        payload = _payload(
            candidate,
            total,
            len(batches) + 1,
            _is_met_batch(candidate_records),
            max_minutes,
        )
        if sections and len(json.dumps(payload).encode("utf-8")) > MAX_PAYLOAD_BYTES:
            batches.append(
                _payload(
                    sections,
                    total,
                    len(batches) + 1,
                    _is_met_batch(batch_records),
                    max_minutes,
                )
            )
            sections = [_case_section(record)]
            batch_records = [record]
        else:
            sections = candidate
            batch_records = candidate_records
    batches.append(
        _payload(
            sections,
            total,
            len(batches) + 1,
            _is_met_batch(batch_records),
            max_minutes,
        )
    )
    return batches


def _payload(sections, total, part, met=False, max_minutes=60):
    suffix = f" - part {part}" if part > 1 else ""
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": f"{_alert_title(met, max_minutes)} ({total}){suffix}",
                            "size": "Large",
                            "weight": "Bolder",
                            "wrap": True,
                        },
                        *(
                            sections
                            if sections
                            else [
                                {
                                    "type": "TextBlock",
                                    "text": "There are no pending cases.",
                                    "wrap": True,
                                }
                            ]
                        ),
                    ],
                },
            }
        ],
    }


def post_pending_cases(webhook_url, records, timeout=30, max_minutes=60):
    """Post every pending case to the Teams channel associated with webhook_url."""
    if not webhook_url:
        raise TeamsNotificationError(
            "TEAMS_WEBHOOK_URL is not set for the Teams destination."
        )

    payloads = build_payloads(records, max_minutes=max_minutes)
    for payload in payloads:
        response = requests.post(webhook_url, json=payload, timeout=timeout)
        if response.status_code >= 400:
            raise TeamsNotificationError(
                f"Teams webhook returned {response.status_code}: {response.text[:300]}"
            )
    return len(payloads)

