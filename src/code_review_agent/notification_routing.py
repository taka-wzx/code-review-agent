"""Offline notification routing with bounded retries and incident escalation.

The module keeps notification delivery behind an injected sender protocol. It
deliberately stores only safe identifiers and bounded enums, so future channel
adapters can be added without retaining raw error details or notification
content in the routing core.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
import re
from typing import Iterable, Protocol
import uuid


_MAX_ATTEMPTS = 8
_MAX_RETRY_SECONDS = 24 * 60 * 60
_MAX_DEDUP_WINDOW_SECONDS = 7 * 24 * 60 * 60
_MAX_DISPATCH_BATCH = 1_000
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_REASON_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class NotificationRoutingError(RuntimeError):
    """Base class for bounded notification-routing failures."""


class InvalidNotificationInput(NotificationRoutingError):
    """The caller supplied an unsafe or unsupported routing value."""


class DeliveryNotFound(NotificationRoutingError):
    """A requested notification delivery does not exist."""


class EventKind(str, Enum):
    SERVICE_HEALTH = "service_health"
    SECURITY = "security"
    APPROVAL = "approval"
    PUBLICATION = "publication"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class NotificationRoute(str, Enum):
    OPERATIONS = "operations"
    SECURITY = "security"
    APPROVALS = "approvals"
    PUBLISHING = "publishing"
    ON_CALL = "on_call"


class DeliveryOutcome(str, Enum):
    DELIVERED = "delivered"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"


class DeliveryState(str, Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    DEAD_LETTER = "dead_letter"


_SEVERITY_RANK = {
    Severity.INFO: 0,
    Severity.WARNING: 1,
    Severity.CRITICAL: 2,
}


def _require_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise InvalidNotificationInput(f"{field} is invalid")
    return value


def _require_reason_code(value: object) -> str:
    if not isinstance(value, str) or _REASON_CODE.fullmatch(value) is None:
        raise InvalidNotificationInput("reason code is invalid")
    return value


def _require_utc(value: object, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise InvalidNotificationInput(f"{field} must be a UTC timestamp")
    return value


def _require_int(value: object, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvalidNotificationInput(f"{field} is invalid")
    return value


def _severity_at_least(actual: Severity, minimum: Severity) -> bool:
    return _SEVERITY_RANK[actual] >= _SEVERITY_RANK[minimum]


@dataclass(frozen=True)
class IncidentSignal:
    """Safe incident identity used for policy routing and alert deduplication."""

    event_id: str
    event_kind: EventKind
    severity: Severity
    reason_code: str
    dedup_key: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        _require_identifier(self.event_id, "event identifier")
        _require_identifier(self.dedup_key, "deduplication key")
        _require_reason_code(self.reason_code)
        _require_utc(self.occurred_at, "occurred_at")
        if not isinstance(self.event_kind, EventKind):
            raise InvalidNotificationInput("event kind is invalid")
        if not isinstance(self.severity, Severity):
            raise InvalidNotificationInput("severity is invalid")


@dataclass(frozen=True)
class RoutingPolicy:
    """Bounded policy governing one incident kind's delivery lifecycle."""

    policy_id: str
    event_kind: EventKind
    minimum_severity: Severity
    primary_route: NotificationRoute
    max_attempts: int
    retry_base_seconds: int
    retry_max_seconds: int
    dedup_window_seconds: int
    escalation_route: NotificationRoute | None = None
    escalate_after_attempt: int | None = None
    escalation_minimum_severity: Severity | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.policy_id, "policy identifier")
        if not isinstance(self.event_kind, EventKind):
            raise InvalidNotificationInput("policy event kind is invalid")
        if not isinstance(self.minimum_severity, Severity):
            raise InvalidNotificationInput("minimum severity is invalid")
        if not isinstance(self.primary_route, NotificationRoute):
            raise InvalidNotificationInput("primary route is invalid")
        _require_int(self.max_attempts, "max attempts", 1, _MAX_ATTEMPTS)
        _require_int(self.retry_base_seconds, "retry base seconds", 1, _MAX_RETRY_SECONDS)
        _require_int(self.retry_max_seconds, "retry maximum seconds", 1, _MAX_RETRY_SECONDS)
        _require_int(
            self.dedup_window_seconds,
            "deduplication window seconds",
            0,
            _MAX_DEDUP_WINDOW_SECONDS,
        )
        if self.retry_base_seconds > self.retry_max_seconds:
            raise InvalidNotificationInput("retry base exceeds retry maximum")
        if self.escalation_route is None:
            if self.escalate_after_attempt is not None or self.escalation_minimum_severity is not None:
                raise InvalidNotificationInput("escalation configuration is incomplete")
            return
        if self.escalation_route == self.primary_route:
            raise InvalidNotificationInput("escalation route must differ from primary route")
        if not isinstance(self.escalation_minimum_severity, Severity):
            raise InvalidNotificationInput("escalation minimum severity is invalid")
        _require_int(
            self.escalate_after_attempt,
            "escalation attempt",
            1,
            self.max_attempts,
        )


@dataclass(frozen=True)
class DeliveryRequest:
    """Safe sender projection with no raw incident or exception data."""

    delivery_id: str
    event_id: str
    event_kind: EventKind
    severity: Severity
    reason_code: str
    policy_id: str
    route: NotificationRoute
    attempt: int
    escalated: bool


class NotificationSender(Protocol):
    """Pluggable delivery boundary; implementations must return a bounded outcome."""

    def deliver(self, request: DeliveryRequest) -> DeliveryOutcome: ...


@dataclass(frozen=True)
class NotificationDelivery:
    """In-memory lifecycle state for one policy-matched alert."""

    delivery_id: str
    policy_id: str
    event_id: str
    event_kind: EventKind
    severity: Severity
    reason_code: str
    state: DeliveryState
    attempt_count: int
    next_attempt_at: datetime | None
    created_at: datetime
    completed_at: datetime | None
    last_outcome: DeliveryOutcome | None
    last_route: NotificationRoute | None
    escalated: bool


@dataclass(frozen=True)
class RoutingSubmission:
    created_delivery_ids: tuple[str, ...]
    suppressed_policy_ids: tuple[str, ...]
    unmatched: bool


@dataclass(frozen=True)
class DispatchSummary:
    attempted_delivery_ids: tuple[str, ...]
    delivered_delivery_ids: tuple[str, ...]
    retried_delivery_ids: tuple[str, ...]
    dead_letter_delivery_ids: tuple[str, ...]


class NotificationRouter:
    """Route safe incident signals and manage bounded in-memory delivery state."""

    def __init__(self, policies: Iterable[RoutingPolicy], sender: NotificationSender) -> None:
        self._policies: dict[str, RoutingPolicy] = {}
        for policy in policies:
            if not isinstance(policy, RoutingPolicy):
                raise InvalidNotificationInput("routing policy is invalid")
            if policy.policy_id in self._policies:
                raise InvalidNotificationInput("routing policy identifier is duplicated")
            self._policies[policy.policy_id] = policy
        if not self._policies:
            raise InvalidNotificationInput("at least one routing policy is required")
        self._sender = sender
        self._deliveries: dict[str, NotificationDelivery] = {}
        self._dedup_last_accepted: dict[tuple[str, str, str], datetime] = {}

    def submit(self, signal: IncidentSignal, now: datetime) -> RoutingSubmission:
        if not isinstance(signal, IncidentSignal):
            raise InvalidNotificationInput("incident signal is invalid")
        current = _require_utc(now, "now")
        self._prune_deduplication(current)
        created: list[str] = []
        suppressed: list[str] = []
        matched = False
        for policy in sorted(self._policies.values(), key=lambda item: item.policy_id):
            if policy.event_kind != signal.event_kind:
                continue
            if not _severity_at_least(signal.severity, policy.minimum_severity):
                continue
            matched = True
            dedup_key = (policy.policy_id, signal.event_kind.value, signal.dedup_key)
            accepted_at = self._dedup_last_accepted.get(dedup_key)
            if (
                accepted_at is not None
                and current < accepted_at + timedelta(seconds=policy.dedup_window_seconds)
            ):
                suppressed.append(policy.policy_id)
                continue
            delivery_id = uuid.uuid4().hex
            self._dedup_last_accepted[dedup_key] = current
            self._deliveries[delivery_id] = NotificationDelivery(
                delivery_id=delivery_id,
                policy_id=policy.policy_id,
                event_id=signal.event_id,
                event_kind=signal.event_kind,
                severity=signal.severity,
                reason_code=signal.reason_code,
                state=DeliveryState.PENDING,
                attempt_count=0,
                next_attempt_at=current,
                created_at=current,
                completed_at=None,
                last_outcome=None,
                last_route=None,
                escalated=False,
            )
            created.append(delivery_id)
        return RoutingSubmission(
            created_delivery_ids=tuple(created),
            suppressed_policy_ids=tuple(suppressed),
            unmatched=not matched,
        )

    def dispatch_due(self, now: datetime, *, limit: int = 100) -> DispatchSummary:
        current = _require_utc(now, "now")
        _require_int(limit, "dispatch limit", 1, _MAX_DISPATCH_BATCH)
        due = sorted(
            (
                delivery
                for delivery in self._deliveries.values()
                if delivery.state == DeliveryState.PENDING
                and delivery.next_attempt_at is not None
                and delivery.next_attempt_at <= current
            ),
            key=lambda delivery: (delivery.next_attempt_at, delivery.delivery_id),
        )[:limit]
        attempted: list[str] = []
        delivered: list[str] = []
        retried: list[str] = []
        dead_letter: list[str] = []
        for delivery in due:
            attempted.append(delivery.delivery_id)
            policy = self._policies[delivery.policy_id]
            attempt = delivery.attempt_count + 1
            route, escalated = self._route_for(policy, delivery.severity, attempt)
            request = DeliveryRequest(
                delivery_id=delivery.delivery_id,
                event_id=delivery.event_id,
                event_kind=delivery.event_kind,
                severity=delivery.severity,
                reason_code=delivery.reason_code,
                policy_id=policy.policy_id,
                route=route,
                attempt=attempt,
                escalated=escalated,
            )
            outcome = self._deliver(request)
            if outcome == DeliveryOutcome.DELIVERED:
                self._deliveries[delivery.delivery_id] = replace(
                    delivery,
                    state=DeliveryState.DELIVERED,
                    attempt_count=attempt,
                    next_attempt_at=None,
                    completed_at=current,
                    last_outcome=outcome,
                    last_route=route,
                    escalated=escalated,
                )
                delivered.append(delivery.delivery_id)
                continue
            if outcome == DeliveryOutcome.RETRYABLE_FAILURE and attempt < policy.max_attempts:
                delay_seconds = min(
                    policy.retry_max_seconds,
                    policy.retry_base_seconds * (2 ** (attempt - 1)),
                )
                self._deliveries[delivery.delivery_id] = replace(
                    delivery,
                    attempt_count=attempt,
                    next_attempt_at=current + timedelta(seconds=delay_seconds),
                    last_outcome=outcome,
                    last_route=route,
                    escalated=escalated,
                )
                retried.append(delivery.delivery_id)
                continue
            self._deliveries[delivery.delivery_id] = replace(
                delivery,
                state=DeliveryState.DEAD_LETTER,
                attempt_count=attempt,
                next_attempt_at=None,
                completed_at=current,
                last_outcome=outcome,
                last_route=route,
                escalated=escalated,
            )
            dead_letter.append(delivery.delivery_id)
        return DispatchSummary(
            attempted_delivery_ids=tuple(attempted),
            delivered_delivery_ids=tuple(delivered),
            retried_delivery_ids=tuple(retried),
            dead_letter_delivery_ids=tuple(dead_letter),
        )

    def get_delivery(self, delivery_id: str) -> NotificationDelivery:
        identifier = _require_identifier(delivery_id, "delivery identifier")
        try:
            return self._deliveries[identifier]
        except KeyError:
            raise DeliveryNotFound("notification delivery was not found") from None

    def deliveries(self) -> tuple[NotificationDelivery, ...]:
        return tuple(sorted(self._deliveries.values(), key=lambda delivery: delivery.delivery_id))

    def _prune_deduplication(self, now: datetime) -> None:
        for key, accepted_at in tuple(self._dedup_last_accepted.items()):
            policy = self._policies[key[0]]
            if now >= accepted_at + timedelta(seconds=policy.dedup_window_seconds):
                del self._dedup_last_accepted[key]

    @staticmethod
    def _route_for(
        policy: RoutingPolicy,
        severity: Severity,
        attempt: int,
    ) -> tuple[NotificationRoute, bool]:
        if (
            policy.escalation_route is not None
            and policy.escalate_after_attempt is not None
            and policy.escalation_minimum_severity is not None
            and attempt >= policy.escalate_after_attempt
            and _severity_at_least(severity, policy.escalation_minimum_severity)
        ):
            return policy.escalation_route, True
        return policy.primary_route, False

    def _deliver(self, request: DeliveryRequest) -> DeliveryOutcome:
        try:
            outcome = self._sender.deliver(request)
        except Exception:
            return DeliveryOutcome.RETRYABLE_FAILURE
        if not isinstance(outcome, DeliveryOutcome):
            return DeliveryOutcome.RETRYABLE_FAILURE
        return outcome
