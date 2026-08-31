"""Breaking things on purpose, where the agents can see it.

The failure mode is faults injected at a layer the agents never see, so the test
proves nothing. A fault swallowed by a retry in the HTTP client demonstrates that
the HTTP client retries. It says nothing about whether the fleet would notice a
system lying to it.

So every fault here surfaces as a **tool result the agent reads**. A timeout comes
back as a tool result saying the call timed out. A contradiction comes back as
two source systems returning different numbers for the same fact, with both
numbers visible. The agent has to decide what to do about it, and the decision is
recorded.

## Retrying a contradiction is not handling it

The second failure mode is retry loops presented as resilience. It is worth being
precise about which faults a retry can legitimately answer:

- A **timeout** may be transient. Asking again is a reasonable first move, and
  the fleet is allowed one before it treats the system as down.
- A **contradiction** is not transient. If CoreBank says the balance is one
  number and CRM360 says another, asking either of them again returns the same
  answer, more confidently. There is no amount of retrying that resolves which
  one is right. The only correct move is to stop and escalate, because acting on
  either number means acting on data the bank knows is disputed.

That distinction is enforced rather than described: ``ContradictionFault`` returns
the same conflicting pair however many times it is called, so a fleet that
retries its way past one cannot.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class FaultType(str, Enum):
    """The ways this workflow can break."""

    #: A source system stops answering. CommsVault is the realistic case.
    TIMEOUT = "timeout"
    #: A system answers with something that does not parse as what it should be.
    MALFORMED_RESPONSE = "malformed_response"
    #: Two systems give different answers for the same fact.
    CONTRADICTION = "contradiction"
    #: Gemini declines to act, usually on abusive or distressing content.
    MODEL_REFUSAL = "model_refusal"
    #: The process dies between two steps that should have been one.
    MIDFLIGHT_INTERRUPTION = "midflight_interruption"


@dataclass
class Fault:
    """One fault, armed and waiting for the call it applies to."""

    fault_type: FaultType
    #: Which system this affects: corebank, crm360, commsvault. Empty means any.
    target_system: str = ""
    #: Which method. Empty means any method on the target.
    target_method: str = ""
    #: How many times it fires before expiring. None means until disarmed.
    remaining: Optional[int] = None
    detail: Dict[str, Any] = field(default_factory=dict)
    armed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    fired: int = 0

    def matches(self, system: str, method: str) -> bool:
        if self.remaining is not None and self.remaining <= 0:
            return False
        if self.target_system and self.target_system != system:
            return False
        if self.target_method and self.target_method != method:
            return False
        return True

    def fire(self) -> None:
        self.fired += 1
        if self.remaining is not None:
            self.remaining -= 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fault_type": self.fault_type.value,
            "target_system": self.target_system or "any",
            "target_method": self.target_method or "any",
            "remaining": self.remaining,
            "fired": self.fired,
            "detail": self.detail,
            "armed_at": self.armed_at.isoformat(),
        }


class FaultRegistry:
    """What is currently broken.

    Process-wide so a fault can be armed from an endpoint during a demo and take
    effect on the next agent run without a redeploy.
    """

    def __init__(self) -> None:
        self.faults: List[Fault] = []

    def arm(self, fault: Fault) -> Fault:
        self.faults.append(fault)
        logger.warning(
            "Fault armed: %s on %s.%s",
            fault.fault_type.value,
            fault.target_system or "any",
            fault.target_method or "any",
        )
        return fault

    def disarm_all(self) -> int:
        count = len(self.faults)
        self.faults.clear()
        return count

    def find(self, system: str, method: str) -> Optional[Fault]:
        for fault in self.faults:
            if fault.matches(system, method):
                return fault
        return None

    def active(self) -> List[Fault]:
        return [
            f for f in self.faults if f.remaining is None or f.remaining > 0
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {"armed": [f.to_dict() for f in self.faults]}


_REGISTRY: Optional[FaultRegistry] = None


def get_fault_registry() -> FaultRegistry:
    """The process-wide registry of armed faults."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = FaultRegistry()
    return _REGISTRY


def reset_fault_registry() -> None:
    """Clear everything. Used by tests."""
    global _REGISTRY
    _REGISTRY = None


class SourceSystemTimeout(RuntimeError):
    """A source system stopped answering."""


class FaultySystems:
    """The stub estate with faults applied where the agent can see them.

    Wraps a real estate rather than replacing it, so an unfaulted call returns
    genuine data and the difference between working and broken is exactly the
    fault.
    """

    def __init__(self, live: Any, registry: Optional[FaultRegistry] = None):
        self._live = live
        self._registry = registry or get_fault_registry()

        self.crm360 = _Faulty("crm360", live.crm360, self._registry, self)
        self.corebank = _Faulty("corebank", live.corebank, self._registry, self)
        self.commsvault = _Faulty("commsvault", live.commsvault, self._registry, self)
        self.printpost = _Faulty("printpost", live.printpost, self._registry, self)
        self.regportal = _Faulty("regportal", live.regportal, self._registry, self)

        #: Every fault the agent was actually shown, for the degradation report.
        self.injected: List[Dict[str, Any]] = []

    def record(self, system: str, method: str, fault: Fault) -> None:
        self.injected.append(
            {
                "system": system,
                "method": method,
                "fault_type": fault.fault_type.value,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )


class _Faulty:
    """One system, with faults applied to its answers."""

    def __init__(self, key: str, target: Any, registry: FaultRegistry, owner: FaultySystems):
        self._key = key
        self._target = target
        self._registry = registry
        self._owner = owner
        self.name = getattr(target, "name", key)
        self.region = getattr(target, "region", None)

    def __getattr__(self, method: str):
        def call(*args: Any, **kwargs: Any) -> Any:
            fault = self._registry.find(self._key, method)
            if fault is None:
                return getattr(self._target, method)(*args, **kwargs)

            fault.fire()
            self._owner.record(self._key, method, fault)

            if fault.fault_type is FaultType.TIMEOUT:
                # Surfaced as a result the agent reads, not as an exception the
                # infrastructure swallows.
                return {
                    "error": (
                        f"{self.name}.{method} did not respond within the timeout. "
                        f"No data was returned."
                    ),
                    "system": self.name,
                    "fault": "timeout",
                    "retryable": True,
                }

            if fault.fault_type is FaultType.MALFORMED_RESPONSE:
                return {
                    "error": (
                        f"{self.name}.{method} returned a response that could not be "
                        f"parsed as a valid record."
                    ),
                    "system": self.name,
                    "fault": "malformed_response",
                    "raw": fault.detail.get("raw", "<unparseable>"),
                    "retryable": False,
                }

            if fault.fault_type is FaultType.CONTRADICTION:
                # Both answers are shown. Asking again returns the same pair, so
                # a retry cannot resolve it and the agent has to decide.
                truthful = getattr(self._target, method)(*args, **kwargs)
                return {
                    "contradiction": True,
                    "system": self.name,
                    "field": fault.detail.get("field", "balance"),
                    "answers": [
                        {
                            "source": fault.detail.get("source_a", "CoreBank"),
                            "value": fault.detail.get("value_a"),
                        },
                        {
                            "source": fault.detail.get("source_b", "CRM360"),
                            "value": fault.detail.get("value_b"),
                        },
                    ],
                    "note": (
                        "Two systems of record disagree on this field. Asking either "
                        "of them again returns the same answer. This cannot be "
                        "resolved by retrying."
                    ),
                    "retryable": False,
                    "underlying": truthful,
                }

            return getattr(self._target, method)(*args, **kwargs)

        return call
