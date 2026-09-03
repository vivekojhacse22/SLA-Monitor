"""Live SLA monitor for Dynamics 365 service requests.

Run:  python app.py
Then open http://127.0.0.1:8081 - the page refreshes itself continuously.

The dashboard reads the local hot cache rather than querying Dataverse on every
refresh, so the page stays fast no matter how many businesses are configured.
A background thread runs the pipeline (sync -> cache -> Teams alerts) on the
configured refresh cadence.
"""

import asyncio
import threading
import traceback
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template

import config
import dataverse
import pipeline
import rulesets

app = Flask(__name__)

_state = {"token": None, "last_cycle": None, "last_error": ""}
_store = pipeline.get_store()
_startup_lock = threading.Lock()
_started = False


def _token():
    if not _state["token"]:
        _state["token"] = dataverse.get_token()
    return _state["token"]


def _ruleset_config():
    return rulesets.load(store=_store)


def run_cycle_once(force_full=False):
    """Run one pipeline cycle, refreshing the token if it has expired."""
    try:
        summary = asyncio.run(pipeline.run_cycle(token=_token(), force_full=force_full))
    except dataverse.QueryError as exc:
        if "401" not in str(exc):
            raise
        _state["token"] = None
        summary = asyncio.run(pipeline.run_cycle(token=_token(), force_full=force_full))
    _state["last_cycle"] = summary
    _state["last_error"] = ""
    return summary


def _background_loop():
    while True:
        try:
            run_cycle_once()
        except Exception as exc:
            _state["last_error"] = f"{type(exc).__name__}: {exc}"
            print(traceback.format_exc(), flush=True)
        threading.Event().wait(config.REFRESH_SECONDS)


def initialize_monitor():
    """Sign in, run the first cycle, and start the background worker once."""
    global _started
    with _startup_lock:
        if _started:
            return _state["last_cycle"]

        print(
            "Signing in to",
            config.DATAVERSE_URL,
            "as",
            config.LOGIN_HINT or "(application identity)",
            flush=True,
        )
        _token()
        print("Signed in.", flush=True)

        loaded = _ruleset_config()
        print(f"Businesses          : {len(loaded.enabled_businesses)}", flush=True)
        print(f"Rulesets            : {len(loaded.rulesets)}", flush=True)
        print(
            f"Concurrency         : up to {loaded.max_concurrent_runs} runs, "
            f"{loaded.run_timeout_seconds}s each",
            flush=True,
        )
        print(f"Local DB            : {config.LOCAL_DB_PATH}", flush=True)

        print("\nRunning the first cycle...", flush=True)
        first = run_cycle_once(force_full=True)
        print(
            f"  pulled {first['sync']['pulled']} rows, "
            f"cache holds {first['cache_size']} cases",
            flush=True,
        )

        threading.Thread(
            target=_background_loop,
            name="sla-monitor-cycle",
            daemon=True,
        ).start()
        _started = True
        return first


@app.route("/")
def index():
    return render_template(
        "index.html",
        refresh_seconds=config.REFRESH_SECONDS,
        warn_minutes=config.WARN_MINUTES,
        environment=config.DATAVERSE_URL,
    )


@app.route("/health")
def health():
    status = "degraded" if _state["last_error"] else "healthy"
    return jsonify(
        {
            "status": status,
            "started": _started,
            "last_cycle_utc": (_state["last_cycle"] or {}).get("generated_utc"),
            "last_error": _state["last_error"],
        }
    ), (200 if status == "healthy" else 503)


def _is_active_pending(record):
    minutes = record.get("minutes_remaining")
    return record.get("sla_state") == "Pending" and isinstance(
        minutes, (int, float)
    ) and minutes > 0


def _dashboard_state(record):
    sla_state = record.get("sla_state", "No SLA state")
    if sla_state != "Pending":
        return sla_state
    minutes = record.get("minutes_remaining")
    if isinstance(minutes, (int, float)):
        return "Pending" if minutes > 0 else "Overdue"
    return "No SLA state"


@app.route("/api/service-requests")
def api_service_requests():
    """Every case currently in the hot cache, most urgent first."""
    try:
        records = dataverse.refresh_timing(_store.pending_cases())
    except Exception as exc:
        detail = traceback.format_exc()
        print(detail, flush=True)
        return jsonify({"error": f"{type(exc).__name__}: {exc}", "detail": detail}), 502

    counts = {"Breached": 0, "Expiring soon": 0, "On track": 0, "No SLA set": 0}
    sla_state_counts = {}
    for record in records:
        status = record.get("status", "No SLA set")
        counts[status] = counts.get(status, 0) + 1
        record["pending_active"] = _is_active_pending(record)
        record["dashboard_state"] = _dashboard_state(record)
        dashboard_state = record["dashboard_state"]
        sla_state_counts[dashboard_state] = sla_state_counts.get(dashboard_state, 0) + 1

    order = {"Breached": 0, "Expiring soon": 1, "No SLA set": 2, "On track": 3}
    records.sort(
        key=lambda r: (
            order.get(r.get("status"), 9),
            r.get("minutes_remaining")
            if r.get("minutes_remaining") is not None
            else 10**9,
        )
    )

    last = _state["last_cycle"] or {}
    notes = list((last.get("sync") or {}).get("notes") or [])
    if _state["last_error"]:
        notes.append(f"Last cycle failed: {_state['last_error']}")

    return jsonify(
        {
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "notes": notes,
            "counts": counts,
            "sla_state_counts": sla_state_counts,
            "warn_minutes": config.WARN_MINUTES,
            "sync": last.get("sync", {}),
            "records": records,
        }
    )


@app.route("/api/rulesets")
def api_rulesets():
    """Configured businesses and rulesets, plus how their last runs went."""
    try:
        ruleset_config = _ruleset_config()
    except rulesets.RulesetConfigError as exc:
        return jsonify({"error": str(exc)}), 500

    runs = {}
    for entry in _store.recent_runs(500):
        runs.setdefault(entry["ruleset_id"], entry)

    records = dataverse.refresh_timing(_store.pending_cases())
    payload = []
    for business in ruleset_config.enabled_businesses:
        payload.append(
            {
                "business": business.name,
                "match": business.criteria.as_dict(),
                "cached_cases": len(
                    [r for r in records if r.get("business") == business.name]
                ),
                "rulesets": [
                    {
                        "id": ruleset.id,
                        "name": ruleset.name,
                        "warn_minutes": ruleset.warn_minutes,
                        "after_minutes": ruleset.after_minutes,
                        "alert_on": list(ruleset.alert_on),
                        "match": ruleset.criteria.as_dict(),
                        "matched_cases": len(ruleset.select(records)),
                        "last_run": runs.get(ruleset.id),
                    }
                    for ruleset in business.rulesets
                    if ruleset.enabled
                ],
            }
        )

    return jsonify(
        {
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "max_concurrent_runs": ruleset_config.max_concurrent_runs,
            "run_timeout_seconds": ruleset_config.run_timeout_seconds,
            "cache_size": _store.count_cases(),
            "businesses": payload,
        }
    )


@app.route("/api/cycle", methods=["POST"])
def api_cycle():
    """Trigger a full cycle on demand instead of waiting for the timer."""
    try:
        return jsonify(run_cycle_once(force_full=True))
    except Exception as exc:
        detail = traceback.format_exc()
        print(detail, flush=True)
        return jsonify({"error": f"{type(exc).__name__}: {exc}", "detail": detail}), 502


if __name__ == "__main__":
    initialize_monitor()
    print(f"\nSLA monitor ready - open http://{config.HOST}:{config.PORT}\n")
    app.run(host=config.HOST, port=config.PORT, debug=False)
