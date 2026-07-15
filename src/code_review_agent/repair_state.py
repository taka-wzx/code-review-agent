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
    # A failed commit consumes its approval and returns to WAIT_APPROVAL, but
    # SUBMIT revalidation (unsafe condition, mutated original checkout, budget)
    # and user cancellation must still be able to fail closed from SUBMIT.
    RepairState.SUBMIT: frozenset(
        {RepairState.WAIT_APPROVAL, RepairState.FAILED, RepairState.CANCELLED}
    ),
    RepairState.FAILED: frozenset(),
    RepairState.CANCELLED: frozenset(),
}


class IllegalTransitionError(RuntimeError):
    def __init__(self, current: RepairState, target: RepairState):
        self.current = current
        self.target = target
        super().__init__(f"illegal repair transition: {current.value} -> {target.value}")


def allowed_targets(state: RepairState) -> frozenset[RepairState]:
    return _ALLOWED_TRANSITIONS[RepairState(state)]


def validate_transition(current: RepairState, target: RepairState) -> None:
    current = RepairState(current)
    target = RepairState(target)
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise IllegalTransitionError(current, target)


@dataclass
class RepairStateMachine:
    """Small in-memory facade; the checkpoint store persists its history.

    A reconstructed machine (e.g. on resume) must supply its complete history:
    it has to start at DISCOVER, follow only legal transitions, and end at the
    current state, so a restored record cannot encode an illegal path.
    """

    state: RepairState = RepairState.DISCOVER
    history: list[RepairState] = field(default_factory=lambda: [RepairState.DISCOVER])

    def __post_init__(self) -> None:
        self.state = RepairState(self.state)
        if not self.history:
            self.history = [self.state]
        self.history = [RepairState(item) for item in self.history]
        if self.history[0] is not RepairState.DISCOVER:
            raise ValueError("state-machine history must start at DISCOVER")
        for current, target in zip(self.history, self.history[1:]):
            validate_transition(current, target)
        if self.history[-1] is not self.state:
            raise ValueError("state-machine history must end at the current state")

    def transition(self, target: RepairState) -> RepairState:
        target = RepairState(target)
        validate_transition(self.state, target)
        self.state = target
        self.history.append(target)
        return target
