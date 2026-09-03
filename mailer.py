"""Send unattended SLA alert emails through Microsoft Graph."""

import html
import time
from urllib.parse import quote

import requests


class EmailNotificationError(RuntimeError):
    pass


def select_pending(records, max_minutes=60, after_minutes=None):
    return [
        record
        for record in records
        if record.get("sla_state") == "Pending"
        and record.get("minutes_remaining") is not None
        and 0 < record["minutes_remaining"] <= max_minutes
        and (
            after_minutes is None
            or record["minutes_remaining"] > after_minutes
        )
    ]


def select_missed(records, max_minutes=60):
    del max_minutes
    return [record for record in records if record.get("sla_state") == "Missed"]


def select_met(records, max_minutes=1):
    met_states = {"achieved", "met", "complete", "completed"}
    return [
        record
        for record in records
        if str(record.get("sla_state") or "").strip().lower() in met_states
        and record.get("minutes_remaining") is not None
        and 0 <= record["minutes_remaining"] <= max_minutes
    ]


def _value(value):
    return html.escape(str(value if value not in (None, "") else "-"))


def _remaining(record):
    minutes = record.get("minutes_remaining")
    if minutes is None:
        return "-"
    if minutes < 0:
        return f"{abs(minutes):.1f} minutes overdue"
    return f"{minutes:.1f} minutes remaining"


def build_message(records):
    missed = all(record.get("sla_state") == "Missed" for record in records)
    met_states = {"achieved", "met", "complete", "completed"}
    met = all(
        str(record.get("sla_state") or "").strip().lower() in met_states
        for record in records
    )
    alert_type = "SLA met within 1 minute" if met else ("Missed" if missed else "Due soon")
    rows = "".join(
        "<tr>"
        f"<td>{_value(record.get('case_number'))}</td>"
        f"<td>{_value(record.get('case_owner'))}</td>"
        f"<td>{_value(record.get('pod'))}</td>"
        f"<td>{_value(record.get('support_region'))}</td>"
        f"<td>{_value(record.get('sla_due_utc'))}</td>"
        f"<td>{_value(_remaining(record))}</td>"
        f"<td>{_value(record.get('active_status'))}</td>"
        f"<td>{_value(record.get('transfer_reason'))}</td>"
        "</tr>"
        for record in records
    )
    content = (
        f"<h2>Service Request SLA: {alert_type} ({len(records)})</h2>"
        "<p>This is an automated SLA monitor notification.</p>"
        "<table border='1' cellpadding='6' cellspacing='0'>"
        "<thead><tr><th>Case</th><th>Owner</th><th>Pod / Team</th>"
        "<th>Support region</th><th>SLA due (UTC)</th><th>Timing</th>"
        "<th>Current status</th><th>Transfer reason</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        "<p><small>Transfer reason describes routing and may not be the root cause "
        "of an SLA miss.</small></p>"
    )
    return {
        "message": {
            "subject": f"[{alert_type}] SLA service requests ({len(records)})",
            "body": {"contentType": "HTML", "content": content},
        },
        "saveToSentItems": True,
    }


def _get_token(tenant_id, client_id, client_secret):
    import msal  # imported lazily so case selection works without it

    if not (tenant_id and client_id and client_secret):
        raise EmailNotificationError(
            "Set EMAIL_TENANT_ID, EMAIL_CLIENT_ID and EMAIL_CLIENT_SECRET."
        )
    client = msal.ConfidentialClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        client_credential=client_secret,
    )
    result = client.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )
    if "access_token" not in result:
        raise EmailNotificationError(
            result.get("error_description", "Microsoft Graph sign-in failed.")
        )
    return result["access_token"]


def send_alert_email(
    records,
    tenant_id,
    client_id,
    client_secret,
    sender,
    recipients,
    timeout=30,
    attempts=3,
):
    if not sender or not recipients:
        raise EmailNotificationError("Set EMAIL_SENDER and EMAIL_RECIPIENTS.")
    payload = build_message(records)
    payload["message"]["toRecipients"] = [
        {"emailAddress": {"address": address}} for address in recipients
    ]
    token = _get_token(tenant_id, client_id, client_secret)
    url = f"https://graph.microsoft.com/v1.0/users/{quote(sender, safe='')}/sendMail"
    for attempt in range(1, attempts + 1):
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=timeout,
        )
        if response.status_code == 202:
            return 1
        if response.status_code != 429 and response.status_code < 500:
            break
        if attempt < attempts:
            retry_after = response.headers.get("Retry-After", "")
            delay = int(retry_after) if retry_after.isdigit() else 2 ** (attempt - 1)
            time.sleep(min(delay, 30))
    raise EmailNotificationError(
        f"Microsoft Graph returned {response.status_code}: {response.text[:300]}"
    )