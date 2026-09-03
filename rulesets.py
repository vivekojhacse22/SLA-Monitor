"""Business and ruleset configuration - the fan-out layer of the pipeline.

The orchestrator pulls every business in one Dataverse query, then hands the
rows to the rulesets defined here. Each ruleset is an independent unit of work:
it owns its own match criteria, SLA threshold, notification destinations and
de-duplication namespace, so runs never interfere with each other.

The configuration lives in rulesets.json. It is cached in the local database so
a run can start even when the file is temporarily unavailable.
"""

import json
import os
from dataclasses import dataclass, field

CONFIG_FILE = os.environ.get("RULESETS_FILE", "rulesets.json")

DEFAULT_MAX_CONCURRENT_RUNS = 50
DEFAULT_RUN_TIMEOUT_SECONDS = 20


class RulesetConfigError(RuntimeError):
    pass


# Record field -> the value used for matching. Every criterion is matched
# against the classified record, never against the raw Dataverse row, so a
# ruleset does not need to know the column names.
MATCHABLE_FIELDS = (
    "country",
    "product",
    "support_region",
    "pod",
    "case_owner",
    "active_status",
    "sr_status",
    "sla_state",
    "transfer_reason",
    "status",
)


@dataclass(frozen=True)
class Criteria:
    """One set of match rules. Empty criteria match everything."""

    equals: dict = field(default_factory=dict)
    contains: dict = field(default_factory=dict)

    def matches(self, record):
        for name, wanted in self.equals.items():
            value = str(record.get(name) or "").strip().lower()
            if value not in {str(w).strip().lower() for w in wanted}:
                return False
        for name, wanted in self.contains.items():
            value = str(record.get(name) or "").lower()
            if not any(str(w).strip().lower() in value for w in wanted):
                return False
        return True

    @staticmethod
    def parse(raw, where):
        raw = raw or {}
        equals, contains = {}, {}
        for name, value in raw.items():
            target, key = (
                (contains, name[:-9]) if name.endswith("_contains") else (equals, name)
            )
            if key not in MATCHABLE_FIELDS:
                raise RulesetConfigError(
                    f"{where}: '{name}' is not a field a ruleset can match on. "
                    f"Valid fields: {', '.join(MATCHABLE_FIELDS)} "
                    "(add '_contains' for a substring match)."
                )
            target[key] = value if isinstance(value, list) else [value]
        return Criteria(equals=equals, contains=contains)

    def as_dict(self):
        merged = dict(self.equals)
        merged.update({f"{k}_contains": v for k, v in self.contains.items()})
        return merged


@dataclass(frozen=True)
class Ruleset:
    """One concurrent run: a slice of cases plus where to notify about them."""

    id: str
    name: str
    business: str
    criteria: Criteria
    warn_minutes: int = 60
    after_minutes: int | None = None
    alert_on: tuple = ("pending", "missed")
    teams_chat_id: str = ""
    teams_webhook_url: str = ""
    email_recipients: tuple = ()
    enabled: bool = True

    def select(self, records):
        return [record for record in records if self.criteria.matches(record)]

    @property
    def namespace(self):
        """De-duplication namespace, so two rulesets can both alert on a case."""
        return self.id


@dataclass(frozen=True)
class Business:
    """A Dataverse slice. Its criteria become part of the single pulled query."""

    name: str
    criteria: Criteria
    rulesets: tuple = ()
    enabled: bool = True

    def matches(self, record):
        return self.criteria.matches(record)


@dataclass(frozen=True)
class RulesetConfig:
    businesses: tuple = ()
    max_concurrent_runs: int = DEFAULT_MAX_CONCURRENT_RUNS
    run_timeout_seconds: int = DEFAULT_RUN_TIMEOUT_SECONDS
    raw: dict = field(default_factory=dict)

    @property
    def enabled_businesses(self):
        return tuple(b for b in self.businesses if b.enabled)

    @property
    def rulesets(self):
        return tuple(
            ruleset
            for business in self.enabled_businesses
            for ruleset in business.rulesets
            if ruleset.enabled
        )

    def business_for(self, record):
        for business in self.enabled_businesses:
            if business.matches(record):
                return business.name
        return ""


def parse(document):
    """Turn the configuration document into validated objects."""
    if not isinstance(document, dict):
        raise RulesetConfigError("The ruleset configuration must be a JSON object.")

    concurrency = document.get("concurrency") or {}
    max_runs = int(concurrency.get("max_concurrent_runs", DEFAULT_MAX_CONCURRENT_RUNS))
    timeout = int(concurrency.get("run_timeout_seconds", DEFAULT_RUN_TIMEOUT_SECONDS))
    if max_runs < 1:
        raise RulesetConfigError("concurrency.max_concurrent_runs must be at least 1.")
    if timeout < 1:
        raise RulesetConfigError("concurrency.run_timeout_seconds must be at least 1.")

    businesses, seen_ids = [], set()
    for entry in document.get("businesses") or []:
        name = str(entry.get("name") or "").strip()
        if not name:
            raise RulesetConfigError("Every business needs a name.")
        criteria = Criteria.parse(entry.get("match"), f"business '{name}'")

        rulesets = []
        for raw_ruleset in entry.get("rulesets") or []:
            ruleset_id = str(raw_ruleset.get("id") or "").strip()
            if not ruleset_id:
                raise RulesetConfigError(f"A ruleset in '{name}' has no id.")
            if ruleset_id in seen_ids:
                raise RulesetConfigError(f"Duplicate ruleset id '{ruleset_id}'.")
            seen_ids.add(ruleset_id)

            alert_on = tuple(
                str(value).lower()
                for value in raw_ruleset.get("alert_on", ["pending", "missed"])
            )
            unknown = set(alert_on) - {"pending", "missed", "met"}
            if unknown:
                raise RulesetConfigError(
                    f"Ruleset '{ruleset_id}': alert_on may only contain "
                    f"'pending', 'missed', or 'met', got {', '.join(sorted(unknown))}."
                )

            notify = raw_ruleset.get("notify") or {}
            rulesets.append(
                Ruleset(
                    id=ruleset_id,
                    name=str(raw_ruleset.get("name") or ruleset_id),
                    business=name,
                    criteria=Criteria.parse(
                        raw_ruleset.get("match"), f"ruleset '{ruleset_id}'"
                    ),
                    warn_minutes=int(raw_ruleset.get("warn_minutes", 60)),
                    after_minutes=(
                        int(raw_ruleset["after_minutes"])
                        if "after_minutes" in raw_ruleset
                        else None
                    ),
                    alert_on=alert_on,
                    teams_chat_id=str(notify.get("teams_chat_id") or ""),
                    teams_webhook_url=str(notify.get("teams_webhook_url") or ""),
                    email_recipients=tuple(notify.get("email_recipients") or ()),
                    enabled=bool(raw_ruleset.get("enabled", True)),
                )
            )

        businesses.append(
            Business(
                name=name,
                criteria=criteria,
                rulesets=tuple(rulesets),
                enabled=bool(entry.get("enabled", True)),
            )
        )

    if not businesses:
        raise RulesetConfigError("The configuration contains no businesses.")

    return RulesetConfig(
        businesses=tuple(businesses),
        max_concurrent_runs=max_runs,
        run_timeout_seconds=timeout,
        raw=document,
    )


def load(path=None, store=None):
    """Read rulesets.json, falling back to the copy cached in the local DB."""
    path = path or CONFIG_FILE
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        cached = store.load_config("rulesets") if store else None
        if cached is None:
            raise RulesetConfigError(
                f"{path} was not found and no cached copy exists in the local database."
            )
        return parse(cached)
    except json.JSONDecodeError as exc:
        raise RulesetConfigError(f"{path} is not valid JSON: {exc}") from exc

    config = parse(document)
    if store is not None:
        store.save_config("rulesets", document)
    return config
