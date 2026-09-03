"""Select service requests for each SLA alert stage."""


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
