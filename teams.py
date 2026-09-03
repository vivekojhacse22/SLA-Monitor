"""Post pending SLA case summaries to Microsoft Teams."""

import html
import json
import os
import sys
from urllib.parse import quote

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


def get_graph_token(tenant_id, client_id, login_hint, cache_file):
    """Acquire delegated Graph permission for posting to a Teams chat."""
    import msal  # imported lazily so case selection works without it

    cache = msal.SerializableTokenCache()
    if os.path.exists(cache_file):
        with open(cache_file, "r", encoding="utf-8") as file_handle:
            cache.deserialize(file_handle.read())

    kwargs = {
        "client_id": client_id,
        "authority": f"https://login.microsoftonline.com/{tenant_id}",
        "token_cache": cache,
    }
    broker = sys.platform == "win32"
    try:
        client = msal.PublicClientApplication(
            **kwargs, enable_broker_on_windows=broker
        )
    except Exception:
        broker = False
        client = msal.PublicClientApplication(**kwargs)

    scopes = ["https://graph.microsoft.com/ChatMessage.Send"]
    accounts = client.get_accounts(username=login_hint or None) or client.get_accounts()
    result = client.acquire_token_silent(scopes, account=accounts[0]) if accounts else None
    if not result:
        interactive_kwargs = {"scopes": scopes, "login_hint": login_hint or None}
        if broker:
            interactive_kwargs["parent_window_handle"] = client.CONSOLE_WINDOW_HANDLE
        result = client.acquire_token_interactive(**interactive_kwargs)
    if broker and (not result or "access_token" not in result):
        client = msal.PublicClientApplication(**kwargs)
        result = client.acquire_token_interactive(
            scopes=scopes, login_hint=login_hint or None
        )

    if cache.has_state_changed:
        with open(cache_file, "w", encoding="utf-8") as file_handle:
            file_handle.write(cache.serialize())
    if not result or "access_token" not in result:
        raise TeamsNotificationError(
            (result or {}).get("error_description", "Microsoft Graph sign-in failed.")
        )
    return result["access_token"]


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


def _chat_message(records, total, part, max_minutes=60):
    suffix = f" - part {part}" if part > 1 else ""
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(record.get('case_number') or '(no case number)'))}</td>"
        f"<td>{html.escape(str(record.get('case_owner') or '(unassigned)'))}</td>"
        f"<td>{html.escape(_remaining(record))}</td>"
        "</tr>"
        for record in records
    )
    if not rows:
        rows = '<tr><td colspan="3">There are no pending cases.</td></tr>'
    content = (
        f"<h2>{_alert_title(_is_met_batch(records), max_minutes)} ({total}){suffix}</h2>"
        "<p><b>Support Region:</b> DTP DP Integration - India</p>"
        "<table><thead><tr><th>Case number</th><th>Owner</th>"
        f"<th>SLA time remaining</th></tr></thead><tbody>{rows}</tbody></table>"
    )
    return {"body": {"contentType": "html", "content": content}}


def build_chat_messages(records, max_minutes=60):
    """Build size-bounded Graph chat messages containing every pending case."""
    if not records:
        return [_chat_message([], 0, 1, max_minutes)]

    messages = []
    batch = []
    total = len(records)
    for record in records:
        candidate = batch + [record]
        message = _chat_message(candidate, total, len(messages) + 1, max_minutes)
        if batch and len(json.dumps(message).encode("utf-8")) > MAX_PAYLOAD_BYTES:
            messages.append(
                _chat_message(batch, total, len(messages) + 1, max_minutes)
            )
            batch = [record]
        else:
            batch = candidate
    messages.append(_chat_message(batch, total, len(messages) + 1, max_minutes))
    return messages


def post_pending_cases_to_chat(
    access_token, chat_id, records, timeout=30, max_minutes=60
):
    """Post pending cases directly to a Teams group or meeting chat."""
    if not chat_id:
        raise TeamsNotificationError("TEAMS_CHAT_ID is not set.")

    url = (
        "https://graph.microsoft.com/v1.0/chats/"
        f"{quote(chat_id, safe='')}/messages"
    )
    headers = {"Authorization": f"Bearer {access_token}"}
    messages = build_chat_messages(records, max_minutes=max_minutes)
    for message in messages:
        response = requests.post(url, headers=headers, json=message, timeout=timeout)
        if response.status_code != 201:
            raise TeamsNotificationError(
                f"Microsoft Graph returned {response.status_code}: {response.text[:300]}"
            )
    return len(messages)