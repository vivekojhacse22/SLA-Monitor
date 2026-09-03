"""Concurrent per-ruleset execution.

Every ruleset is an independent run reading from the local hot cache. Runs are
executed concurrently, bounded by max_concurrent_runs (50 by default), and each
one is given a hard deadline of run_timeout_seconds (20 by default). A ruleset
that overruns or fails is recorded and skipped - it can never hold up the rest
of the fan-out or the next cycle.
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import alerting
import mailer
import teams


SELECTORS = {
    "pending": mailer.select_pending,
    "missed": mailer.select_missed,
    "met": mailer.select_met,
}


@dataclass
class RulesetRunResult:
    ruleset_id: str
    ruleset_name: str
    business: str
    outcome: str = "ok"           # ok | timeout | error | skipped
    eligible: int = 0
    sent: int = 0
    skipped_duplicate: int = 0
    messages: int = 0
    duration_ms: int = 0
    detail: str = ""

    def as_dict(self):
        return dict(self.__dict__)


@dataclass
class CycleResult:
    started_utc: str
    duration_ms: int = 0
    results: list = field(default_factory=list)

    @property
    def sent(self):
        return sum(r.sent for r in self.results)

    @property
    def failures(self):
        return [r for r in self.results if r.outcome not in ("ok", "skipped")]

    def as_dict(self):
        return {
            "started_utc": self.started_utc,
            "duration_ms": self.duration_ms,
            "runs": len(self.results),
            "sent": self.sent,
            "failures": len(self.failures),
            "results": [r.as_dict() for r in self.results],
        }


def select_for(ruleset, records, kind):
    """Pick the cases a ruleset should alert on, using its own threshold."""
    selector = SELECTORS[kind]
    if kind == "pending":
        return selector(
            ruleset.select(records),
            max_minutes=ruleset.warn_minutes,
            after_minutes=ruleset.after_minutes,
        )
    return selector(ruleset.select(records), max_minutes=ruleset.warn_minutes)


async def _run_one(ruleset, records, store, senders, timeout_seconds):
    started = time.perf_counter()
    result = RulesetRunResult(
        ruleset_id=ruleset.id, ruleset_name=ruleset.name, business=ruleset.business
    )
    scoped = ruleset.select(records)
    if not scoped:
        result.outcome = "skipped"
        result.detail = "No cases matched this ruleset."
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result

    try:
        async with asyncio.timeout(timeout_seconds):
            for kind in ruleset.alert_on:
                sender = senders.get(kind)
                if sender is None:
                    continue
                selector = SELECTORS[kind]
                if kind == "pending":
                    selector = (
                        lambda batch, max_minutes, base=selector,
                        after=ruleset.after_minutes: base(
                            batch,
                            max_minutes=max_minutes,
                            after_minutes=after,
                        )
                    )
                cycle = await alerting.run_alert_cycle(
                    scoped,
                    _RulesetState(store, ruleset.id),
                    lambda batch, rs=ruleset, k=kind: senders[k](rs, batch),
                    max_minutes=ruleset.warn_minutes,
                    selector=selector,
                    namespace=f"{ruleset.id}:{kind}",
                )
                result.eligible += cycle.eligible
                result.sent += cycle.sent
                result.skipped_duplicate += cycle.skipped_duplicate
                result.messages += cycle.messages
    except TimeoutError:
        result.outcome = "timeout"
        result.detail = (
            f"Exceeded the {timeout_seconds}s budget for a single ruleset run."
        )
    except Exception as exc:  # one ruleset must never break the fan-out
        result.outcome = "error"
        result.detail = f"{type(exc).__name__}: {exc}"

    result.duration_ms = int((time.perf_counter() - started) * 1000)
    return result


class _RulesetState:
    """Notification state scoped to one ruleset, backed by the local DB."""

    def __init__(self, store, ruleset_id):
        self._store = store
        self._ruleset_id = ruleset_id

    def was_sent(self, fingerprint):
        return self._store.was_sent(fingerprint)

    def mark_sent(self, fingerprint, case_number=""):
        self._store.mark_sent(fingerprint, case_number, self._ruleset_id)


async def run_cycle(ruleset_config, records, store, senders):
    """Fan out across every enabled ruleset, concurrently and bounded."""
    started_utc = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    semaphore = asyncio.Semaphore(ruleset_config.max_concurrent_runs)

    async def guarded(ruleset):
        async with semaphore:
            return await _run_one(
                ruleset,
                records,
                store,
                senders,
                ruleset_config.run_timeout_seconds,
            )

    results = await asyncio.gather(
        *(guarded(ruleset) for ruleset in ruleset_config.rulesets),
        return_exceptions=True,
    )

    collected = []
    for ruleset, outcome in zip(ruleset_config.rulesets, results):
        if isinstance(outcome, BaseException):
            outcome = RulesetRunResult(
                ruleset_id=ruleset.id,
                ruleset_name=ruleset.name,
                business=ruleset.business,
                outcome="error",
                detail=f"{type(outcome).__name__}: {outcome}",
            )
        collected.append(outcome)
        store.log_run(
            outcome.ruleset_id,
            started_utc,
            outcome.duration_ms,
            outcome.eligible,
            outcome.sent,
            outcome.skipped_duplicate,
            outcome.outcome,
            outcome.detail,
        )

    return CycleResult(
        started_utc=started_utc,
        duration_ms=int((time.perf_counter() - started) * 1000),
        results=collected,
    )
