"""Durable states and transition validation for the repair workflow."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RepairState(str, Enum):
    DISCOVER = "DISCOVER"
    PLAN = "PLAN"
    PATCH = "PATCH"
    TEST = "TEST"
    REFLECT = "REFLECT"
    WAIT_APPROVAL = "WAIT_APPROVAL"
    SUBMIT = "SUBMIT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# SUBMIT is the happy-path endpoint, but a failed commit returns to WAIT_APPROVAL
# for a newly bound approval. Only failure/cancellation are unconditional terminals.
TERMINAL_STATES = frozenset({RepairState.FAILED, RepairState.CANCELLED})

_ALLOWED_TRANSITIONS: dict[RepairState, frozenset[RepairState]] = {
    RepairState.DISCOVER: frozenset(
        {RepairState.PLAN, RepairState.FAILED, RepairState.CANCELLED}
    ),
    RepairState.PLAN: frozenset(
        {RepairState.PATCH, RepairState.FAILED, RepairState.CANCELLED}
    ),
    RepairState.PATCH: frozenset(
        {RepairState.TEST, RepairState.FAILED, RepairState.CANCELLED}
    ),
    RepairState.TEST: frozenset(
        {RepairState.REFLECT, RepairState.FAILED, RepairState.CANCELLED}
    ),
    RepairState.REFLECT: frozenset(
        {
            RepairState.PATCH,
            RepairState.WAIT_APPROVAL,
            RepairState.FAILED,
            RepairState.CANCELLED,
        }
    ),
    RepairState.WAIT_APPROVAL: frozenset(
        {RepairState.SUBMIT, RepairState.FAILED, RepairState.CANCELLED}
    ),
    # A failed commit consumes its approval and returns to WAIT_APPROVAL.
    RepairState.SUBMIT: frozenset({RepairState.WAIT_APPROVAL}),
    RepairState.FAILED: frozenset(),
    RepairState.CANCELLED: frozenset(),
}


class IllegalTransitionError(RuntimeError):
    def __init__(self, current: RepairState, target: RepairState):
        self.current = current
        self.target = target
        super().__init__(f"illegal repair transition: {current.value} -> {target.value}")


def allowed_targets(state: RepairState) -> frozenset[RepairState]:
    return _ALLOWED_TRANSITIONS[state]


def validate_transition(current: RepairState, target: RepairState) -> None:
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise IllegalTransitionError(current, target)


@dataclass
class RepairStateMachine:
    """Small in-memory facade; the checkpoint store persists its history."""

    state: RepairState = RepairState.DISCOVER
    history: list[RepairState] = field(default_factory=lambda: [RepairState.DISCOVER])

    def __post_init__(self) -> None:
        if not self.history:
            self.history = [self.state]
        elif self.history[-1] != self.state:
            raise ValueError("state-machine history must end at the current state")

    def transition(self, target: RepairState) -> RepairState:
        validate_transition(self.state, target)
        self.state = target
        self.history.append(target)
        return target
