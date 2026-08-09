from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from code_review_agent.notification_routing import (
    DeliveryNotFound,
    DeliveryOutcome,
    DeliveryState,
    EventKind,
    IncidentSignal,
    InvalidNotificationInput,
    NotificationRoute,
    NotificationRouter,
    RoutingPolicy,
    Severity,
)


NOW = datetime(2026, 8, 8, 8, 0, tzinfo=timezone.utc)


class ScriptedSender:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.requests = []

    def deliver(self, request: object) -> DeliveryOutcome:
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if self.outcomes else DeliveryOutcome.DELIVERED
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[return-value]


def signal(
    event_kind: EventKind,
    *,
    event_id: str,
    dedup_key: str,
    severity: Severity = Severity.WARNING,
    occurred_at: datetime = NOW,
) -> IncidentSignal:
    return IncidentSignal(
        event_id=event_id,
        event_kind=event_kind,
        severity=severity,
        reason_code="bounded_failure",
        dedup_key=dedup_key,
        occurred_at=occurred_at,
    )


def policy(
    policy_id: str,
    event_kind: EventKind,
    route: NotificationRoute,
    **overrides: object,
) -> RoutingPolicy:
    values: dict[str, object] = {
        "policy_id": policy_id,
        "event_kind": event_kind,
        "minimum_severity": Severity.WARNING,
        "primary_route": route,
        "max_attempts": 3,
        "retry_base_seconds": 10,
        "retry_max_seconds": 60,
        "dedup_window_seconds": 60,
    }
    values.update(overrides)
    return RoutingPolicy(**values)  # type: ignore[arg-type]


class Issue32NotificationRoutingTests(unittest.TestCase):
    def test_routes_all_required_incident_kinds_and_respects_severity(self) -> None:
        sender = ScriptedSender([DeliveryOutcome.DELIVERED] * 4)
        router = NotificationRouter(
            [
                policy("health", EventKind.SERVICE_HEALTH, NotificationRoute.OPERATIONS),
                policy("security", EventKind.SECURITY, NotificationRoute.SECURITY),
                policy("approval", EventKind.APPROVAL, NotificationRoute.APPROVALS),
                policy("publication", EventKind.PUBLICATION, NotificationRoute.PUBLISHING),
                policy(
                    "critical-security",
                    EventKind.SECURITY,
                    NotificationRoute.ON_CALL,
                    minimum_severity=Severity.CRITICAL,
                ),
            ],
            sender,
        )

        submitted = [
            router.submit(
                signal(kind, event_id=f"event-{index}", dedup_key=f"dedup-{index}"), NOW
            )
            for index, kind in enumerate(
                (
                    EventKind.SERVICE_HEALTH,
                    EventKind.SECURITY,
                    EventKind.APPROVAL,
                    EventKind.PUBLICATION,
                ),
                start=1,
            )
        ]
        summary = router.dispatch_due(NOW)

        self.assertEqual([len(item.created_delivery_ids) for item in submitted], [1, 1, 1, 1])
        self.assertEqual(summary.delivered_delivery_ids, summary.attempted_delivery_ids)
        self.assertEqual(
            {request.route for request in sender.requests},
            {
                NotificationRoute.OPERATIONS,
                NotificationRoute.SECURITY,
                NotificationRoute.APPROVALS,
                NotificationRoute.PUBLISHING,
            },
        )
        low_security = router.submit(
            signal(
                EventKind.SECURITY,
                event_id="security-info",
                dedup_key="security-info",
                severity=Severity.INFO,
            ),
            NOW,
        )
        self.assertTrue(low_security.unmatched)
        self.assertEqual(low_security.created_delivery_ids, ())

    def test_deduplication_preserves_pending_retry_state_and_expires_at_boundary(self) -> None:
        sender = ScriptedSender([DeliveryOutcome.RETRYABLE_FAILURE, DeliveryOutcome.DELIVERED])
        router = NotificationRouter(
            [policy("approval", EventKind.APPROVAL, NotificationRoute.APPROVALS)], sender
        )
        first = router.submit(
            signal(EventKind.APPROVAL, event_id="approval-1", dedup_key="same-alert"), NOW
        )
        delivery_id = first.created_delivery_ids[0]
        router.dispatch_due(NOW)
        before = router.get_delivery(delivery_id)

        duplicate = router.submit(
            signal(EventKind.APPROVAL, event_id="approval-2", dedup_key="same-alert"),
            NOW + timedelta(seconds=1),
        )
        after = router.get_delivery(delivery_id)

        self.assertEqual(duplicate.created_delivery_ids, ())
        self.assertEqual(duplicate.suppressed_policy_ids, ("approval",))
        self.assertEqual(before.attempt_count, 1)
        self.assertEqual(after.attempt_count, 1)
        self.assertEqual(after.next_attempt_at, NOW + timedelta(seconds=10))
        self.assertEqual(len(sender.requests), 1)

        at_boundary = router.submit(
            signal(EventKind.APPROVAL, event_id="approval-3", dedup_key="same-alert"),
            NOW + timedelta(seconds=60),
        )
        self.assertEqual(len(at_boundary.created_delivery_ids), 1)

    def test_retry_backoff_escalation_and_eventual_delivery(self) -> None:
        sender = ScriptedSender(
            [
                DeliveryOutcome.RETRYABLE_FAILURE,
                DeliveryOutcome.RETRYABLE_FAILURE,
                DeliveryOutcome.DELIVERED,
            ]
        )
        router = NotificationRouter(
            [
                policy(
                    "security",
                    EventKind.SECURITY,
                    NotificationRoute.SECURITY,
                    escalation_route=NotificationRoute.ON_CALL,
                    escalate_after_attempt=2,
                    escalation_minimum_severity=Severity.CRITICAL,
                    retry_max_seconds=20,
                )
            ],
            sender,
        )
        delivery_id = router.submit(
            signal(
                EventKind.SECURITY,
                event_id="security-1",
                dedup_key="security-1",
                severity=Severity.CRITICAL,
            ),
            NOW,
        ).created_delivery_ids[0]

        first = router.dispatch_due(NOW)
        second = router.dispatch_due(NOW + timedelta(seconds=10))
        third = router.dispatch_due(NOW + timedelta(seconds=30))
        delivery = router.get_delivery(delivery_id)

        self.assertEqual(first.retried_delivery_ids, (delivery_id,))
        self.assertEqual(second.retried_delivery_ids, (delivery_id,))
        self.assertEqual(third.delivered_delivery_ids, (delivery_id,))
        self.assertEqual(delivery.state, DeliveryState.DELIVERED)
        self.assertEqual(delivery.attempt_count, 3)
        self.assertEqual(
            [request.route for request in sender.requests],
            [NotificationRoute.SECURITY, NotificationRoute.ON_CALL, NotificationRoute.ON_CALL],
        )
        self.assertEqual([request.escalated for request in sender.requests], [False, True, True])

    def test_retry_exhaustion_and_permanent_failures_dead_letter(self) -> None:
        retry_sender = ScriptedSender(
            [DeliveryOutcome.RETRYABLE_FAILURE, DeliveryOutcome.RETRYABLE_FAILURE]
        )
        retry_router = NotificationRouter(
            [policy("publication", EventKind.PUBLICATION, NotificationRoute.PUBLISHING, max_attempts=2)],
            retry_sender,
        )
        retry_id = retry_router.submit(
            signal(EventKind.PUBLICATION, event_id="publication-1", dedup_key="publication-1"), NOW
        ).created_delivery_ids[0]
        retry_router.dispatch_due(NOW)
        exhausted = retry_router.dispatch_due(NOW + timedelta(seconds=10))

        self.assertEqual(exhausted.dead_letter_delivery_ids, (retry_id,))
        self.assertEqual(retry_router.get_delivery(retry_id).state, DeliveryState.DEAD_LETTER)
        self.assertEqual(retry_router.get_delivery(retry_id).attempt_count, 2)
        self.assertEqual(retry_router.dispatch_due(NOW + timedelta(minutes=1)).attempted_delivery_ids, ())

        permanent_sender = ScriptedSender([DeliveryOutcome.PERMANENT_FAILURE])
        permanent_router = NotificationRouter(
            [policy("health", EventKind.SERVICE_HEALTH, NotificationRoute.OPERATIONS)], permanent_sender
        )
        permanent_id = permanent_router.submit(
            signal(EventKind.SERVICE_HEALTH, event_id="health-1", dedup_key="health-1"), NOW
        ).created_delivery_ids[0]
        permanent = permanent_router.dispatch_due(NOW)

        self.assertEqual(permanent.dead_letter_delivery_ids, (permanent_id,))
        self.assertEqual(permanent_router.get_delivery(permanent_id).attempt_count, 1)

    def test_sender_exceptions_and_invalid_outcomes_are_redacted_retryable_failures(self) -> None:
        sender = ScriptedSender([RuntimeError("sensitive sender detail"), "not-an-outcome"])
        router = NotificationRouter(
            [policy("health", EventKind.SERVICE_HEALTH, NotificationRoute.OPERATIONS)], sender
        )
        delivery_id = router.submit(
            signal(EventKind.SERVICE_HEALTH, event_id="health-1", dedup_key="health-1"), NOW
        ).created_delivery_ids[0]

        first = router.dispatch_due(NOW)
        second = router.dispatch_due(NOW + timedelta(seconds=10))
        delivery = router.get_delivery(delivery_id)

        self.assertEqual(first.retried_delivery_ids, (delivery_id,))
        self.assertEqual(second.retried_delivery_ids, (delivery_id,))
        self.assertEqual(delivery.last_outcome, DeliveryOutcome.RETRYABLE_FAILURE)
        self.assertNotIn("sensitive sender detail", str(delivery))

    def test_invalid_inputs_and_duplicate_policies_fail_closed(self) -> None:
        with self.assertRaises(InvalidNotificationInput):
            IncidentSignal(
                event_id="event-1",
                event_kind=EventKind.APPROVAL,
                severity=Severity.WARNING,
                reason_code="bad-code",
                dedup_key="dedup-1",
                occurred_at=NOW,
            )
        with self.assertRaises(InvalidNotificationInput):
            IncidentSignal(
                event_id="event-1",
                event_kind=EventKind.APPROVAL,
                severity=Severity.WARNING,
                reason_code="valid_reason",
                dedup_key="dedup-1",
                occurred_at=datetime(2026, 8, 8, 8, 0),
            )
        with self.assertRaises(InvalidNotificationInput):
            policy(
                "bad-escalation",
                EventKind.SECURITY,
                NotificationRoute.SECURITY,
                escalation_route=NotificationRoute.ON_CALL,
            )
        duplicate = policy("approval", EventKind.APPROVAL, NotificationRoute.APPROVALS)
        with self.assertRaises(InvalidNotificationInput):
            NotificationRouter([duplicate, duplicate], ScriptedSender([]))
        with self.assertRaises(InvalidNotificationInput):
            NotificationRouter([], ScriptedSender([]))

        router = NotificationRouter([duplicate], ScriptedSender([]))
        with self.assertRaises(InvalidNotificationInput):
            router.dispatch_due(NOW, limit=0)
        with self.assertRaises(InvalidNotificationInput):
            router.get_delivery("invalid/path")
        with self.assertRaises(DeliveryNotFound):
            router.get_delivery("missing-delivery")


if __name__ == "__main__":
    unittest.main()
