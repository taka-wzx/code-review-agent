"""Durable, hash-only acknowledgement for signed GitHub App webhooks."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import re
from typing import Any

from code_review_agent.database import Database, WebhookDeliveryConflict
from code_review_agent.service_core import IdempotencyConflict, InvalidRequest, SCHEMA_VERSION


_DELIVERY_ID = re.compile(r"[A-Za-z0-9-]{1,128}\Z")
_EVENT = re.compile(r"[a-z][a-z_]{0,63}\Z")
_SHA = re.compile(r"[0-9a-fA-F]{40,64}\Z")
_PULL_REQUEST_ACTIONS = frozenset({"opened", "reopened", "synchronize", "ready_for_review"})
_INSTALLATION_ACTIONS = frozenset(
    {"created", "suspend", "unsuspend", "deleted", "new_permissions_accepted"}
)


class GitHubWebhookDeliveryConflict(IdempotencyConflict):
    """A delivery ID cannot be reused for different signed content."""

    code = "github_webhook_delivery_conflict"


@dataclass(frozen=True)
class WebhookAcknowledgement:
    status_code: int
    body: dict[str, Any]


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidRequest(f"webhook {field} is invalid")
    return value


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidRequest(f"webhook {field} is invalid")
    return value


def _event(value: object) -> str:
    if not isinstance(value, str) or _EVENT.fullmatch(value) is None:
        raise InvalidRequest("webhook event is invalid")
    return value


def _delivery_id(value: object) -> str:
    if not isinstance(value, str) or _DELIVERY_ID.fullmatch(value) is None:
        raise InvalidRequest("webhook delivery ID is required")
    return value


def _installation_fields(payload: object) -> tuple[str, int, int, int]:
    root = _mapping(payload, "payload")
    action = root.get("action")
    if action not in _INSTALLATION_ACTIONS:
        raise InvalidRequest("webhook installation action is not supported")
    installation = _mapping(root.get("installation"), "installation")
    account = _mapping(installation.get("account"), "installation account")
    return (
        str(action),
        _positive_int(installation.get("id"), "installation ID"),
        _positive_int(installation.get("app_id"), "App ID"),
        _positive_int(account.get("id"), "installation account ID"),
    )


def _pull_request_fields(
    payload: object,
) -> tuple[str, str, str, int | None, int | None]:
    root = _mapping(payload, "payload")
    if root.get("action") not in _PULL_REQUEST_ACTIONS:
        raise InvalidRequest("pull_request action is not reviewable")
    repository = _mapping(root.get("repository"), "repository")
    pull_request = _mapping(root.get("pull_request"), "pull_request")
    alias = repository.get("full_name")
    number = pull_request.get("number")
    head = _mapping(pull_request.get("head"), "pull_request head")
    head_sha = head.get("sha")
    if not isinstance(alias, str) or isinstance(number, bool) or not isinstance(number, int):
        raise InvalidRequest("webhook repository or pull_request identity is invalid")
    if not isinstance(head_sha, str) or _SHA.fullmatch(head_sha) is None:
        raise InvalidRequest("webhook pull_request head SHA is invalid")

    installation = root.get("installation")
    if installation is None:
        return alias, str(number), head_sha.casefold(), None, None
    installation_mapping = _mapping(installation, "installation")
    owner = _mapping(repository.get("owner"), "repository owner")
    return (
        alias,
        str(number),
        head_sha.casefold(),
        _positive_int(installation_mapping.get("id"), "installation ID"),
        _positive_int(owner.get("id"), "repository owner ID"),
    )


class GitHubWebhookProcessor:
    """Validate durable delivery semantics after the HTTP layer verifies HMAC."""

    def __init__(
        self,
        database: Database,
        *,
        submit_pull_request: Callable[..., tuple[dict[str, Any], bool]],
        get_job: Callable[[str], dict[str, Any]],
    ) -> None:
        self._database = database
        self._submit_pull_request = submit_pull_request
        self._get_job = get_job

    def acknowledge(
        self, *, event: object, delivery_id: object, body: bytes, payload: object
    ) -> WebhookAcknowledgement:
        event_name = _event(event)
        delivery = _delivery_id(delivery_id)
        payload_sha256 = hashlib.sha256(body).hexdigest()
        if event_name not in {"ping", "installation", "pull_request"}:
            return WebhookAcknowledgement(
                status_code=202,
                body={"schema_version": SCHEMA_VERSION, "status": "ignored"},
            )
        existing = self._existing(delivery, event_name, payload_sha256)
        if existing is not None:
            return self._stored_acknowledgement(existing, duplicate=True)

        if event_name == "ping":
            record, duplicate = self._record(
                delivery_id=delivery,
                event=event_name,
                payload_sha256=payload_sha256,
                status="pong",
                http_status=200,
            )
            return self._stored_acknowledgement(record, duplicate=duplicate)
        if event_name == "installation":
            action, installation_id, app_id, account_id = _installation_fields(payload)
            try:
                record, duplicate = self._database.apply_github_installation_webhook(
                    delivery_id=delivery,
                    payload_sha256=payload_sha256,
                    action=action,
                    installation_id=installation_id,
                    app_id=app_id,
                    account_id=account_id,
                )
            except WebhookDeliveryConflict as exc:
                raise GitHubWebhookDeliveryConflict("webhook delivery conflicts") from exc
            return self._stored_acknowledgement(record, duplicate=duplicate)
        if event_name == "pull_request":
            return self._acknowledge_pull_request(
                delivery=delivery,
                payload_sha256=payload_sha256,
                payload=payload,
            )

        raise AssertionError("recognized webhook event was not handled")

    def _acknowledge_pull_request(
        self, *, delivery: str, payload_sha256: str, payload: object
    ) -> WebhookAcknowledgement:
        repository, pull_request, head_sha, installation_id, owner_id = _pull_request_fields(
            payload
        )
        if installation_id is not None:
            installation = self._database.github_app_installation(installation_id)
            if installation is None:
                return self._ignored_pull_request(
                    delivery=delivery,
                    payload_sha256=payload_sha256,
                    installation_id=installation_id,
                    reason="installation_unknown",
                )
            if str(installation["state"]) != "active":
                return self._ignored_pull_request(
                    delivery=delivery,
                    payload_sha256=payload_sha256,
                    installation_id=installation_id,
                    reason="installation_inactive",
                )
            if owner_id != int(installation["account_id"]):
                return self._ignored_pull_request(
                    delivery=delivery,
                    payload_sha256=payload_sha256,
                    installation_id=installation_id,
                    reason="installation_account_mismatch",
                )

        job, job_duplicate = self._submit_pull_request(
            repository,
            pull_request,
            delivery_id=delivery,
            correlation_id=delivery,
            head_sha=head_sha,
        )
        record, receipt_duplicate = self._record(
            delivery_id=delivery,
            event="pull_request",
            payload_sha256=payload_sha256,
            status="review_queued",
            review_job_id=str(job["review_id"]),
            installation_id=None if installation_id is None else str(installation_id),
        )
        return self._stored_acknowledgement(
            record, duplicate=job_duplicate or receipt_duplicate
        )

    def _ignored_pull_request(
        self,
        *,
        delivery: str,
        payload_sha256: str,
        installation_id: int,
        reason: str,
    ) -> WebhookAcknowledgement:
        record, duplicate = self._record(
            delivery_id=delivery,
            event="pull_request",
            payload_sha256=payload_sha256,
            status="ignored",
            reason=reason,
            installation_id=str(installation_id),
        )
        return self._stored_acknowledgement(record, duplicate=duplicate)

    def _existing(
        self, delivery_id: str, event: str, payload_sha256: str
    ) -> dict[str, Any] | None:
        try:
            return self._database.github_webhook_delivery(
                delivery_id, event=event, payload_sha256=payload_sha256
            )
        except WebhookDeliveryConflict as exc:
            raise GitHubWebhookDeliveryConflict("webhook delivery conflicts") from exc

    def _record(self, **kwargs: Any) -> tuple[dict[str, Any], bool]:
        try:
            return self._database.record_github_webhook_delivery(**kwargs)
        except WebhookDeliveryConflict as exc:
            raise GitHubWebhookDeliveryConflict("webhook delivery conflicts") from exc

    def _stored_acknowledgement(
        self, record: Mapping[str, Any], *, duplicate: bool
    ) -> WebhookAcknowledgement:
        status = str(record["status"])
        status_code = int(record["http_status"])
        if status == "review_queued":
            review_job_id = record.get("review_job_id")
            if not isinstance(review_job_id, str) or not review_job_id:
                raise InvalidRequest("webhook delivery receipt is invalid")
            return WebhookAcknowledgement(
                status_code=status_code,
                body={**self._get_job(review_job_id), "duplicate": duplicate},
            )
        body: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "duplicate": duplicate,
        }
        reason = record.get("reason")
        if isinstance(reason, str) and reason:
            body["reason"] = reason
        return WebhookAcknowledgement(status_code=status_code, body=body)
