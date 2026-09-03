"""Persistent state for proactive Teams destinations and delivered SLA alerts."""

import hashlib
import os
from datetime import datetime, timezone


def _row_key(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class MemoryNotificationState:
    """Small in-memory implementation used by tests and local development."""

    def __init__(self):
        self.conversations = {}
        self.alerts = {}

    def save_conversation(self, conversation_id, service_url="", team_id="", channel_id=""):
        self.conversations[conversation_id] = {
            "conversation_id": conversation_id,
            "service_url": service_url,
            "team_id": team_id,
            "channel_id": channel_id,
        }

    def get_destination(self, configured_id=""):
        if configured_id:
            return self.conversations.get(configured_id)
        if len(self.conversations) == 1:
            return next(iter(self.conversations.values()))
        return None

    def was_sent(self, fingerprint):
        return fingerprint in self.alerts

    def mark_sent(self, fingerprint, case_number=""):
        self.alerts[fingerprint] = case_number


class TableNotificationState:
    """Azure Table Storage implementation shared by all Function instances."""

    def __init__(self, table_name, endpoint="", connection_string=""):
        from azure.data.tables import TableServiceClient

        if endpoint:
            from azure.identity import DefaultAzureCredential

            service = TableServiceClient(endpoint, credential=DefaultAzureCredential())
        elif connection_string:
            service = TableServiceClient.from_connection_string(connection_string)
        else:
            raise ValueError(
                "Set TABLE_STORAGE_ENDPOINT or AzureWebJobsStorage for notification state."
            )
        self.table = service.create_table_if_not_exists(table_name)

    def save_conversation(self, conversation_id, service_url="", team_id="", channel_id=""):
        self.table.upsert_entity(
            {
                "PartitionKey": "destinations",
                "RowKey": _row_key(conversation_id),
                "ConversationId": conversation_id,
                "ServiceUrl": service_url or "",
                "TeamId": team_id or "",
                "ChannelId": channel_id or "",
                "UpdatedUtc": datetime.now(timezone.utc).isoformat(),
            }
        )

    def get_destination(self, configured_id=""):
        if configured_id:
            try:
                entity = self.table.get_entity("destinations", _row_key(configured_id))
            except Exception as exc:
                if getattr(exc, "status_code", None) == 404:
                    return None
                raise
            return self._destination(entity)

        entities = list(
            self.table.query_entities(
                "PartitionKey eq 'destinations'", select=["ConversationId", "ServiceUrl", "TeamId", "ChannelId"]
            )
        )
        if len(entities) != 1:
            return None
        return self._destination(entities[0])

    @staticmethod
    def _destination(entity):
        return {
            "conversation_id": entity["ConversationId"],
            "service_url": entity.get("ServiceUrl", ""),
            "team_id": entity.get("TeamId", ""),
            "channel_id": entity.get("ChannelId", ""),
        }

    def was_sent(self, fingerprint):
        try:
            self.table.get_entity("alerts", _row_key(fingerprint))
            return True
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                return False
            raise

    def mark_sent(self, fingerprint, case_number=""):
        self.table.upsert_entity(
            {
                "PartitionKey": "alerts",
                "RowKey": _row_key(fingerprint),
                "Fingerprint": fingerprint,
                "CaseNumber": case_number,
                "SentUtc": datetime.now(timezone.utc).isoformat(),
            }
        )


def create_notification_state(table_name, endpoint=""):
    connection_string = os.environ.get("AzureWebJobsStorage", "").strip()
    if endpoint or connection_string:
        return TableNotificationState(table_name, endpoint, connection_string)
    return MemoryNotificationState()