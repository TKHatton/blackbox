"""Governance rules as data, so they can be swapped at replay time.

Phase 4 wrote its rules as Python functions. That was fine until Phase 6, whose
whole premise is rewinding to a past moment, altering a rule, and replaying. A
rule compiled into an agent cannot be altered without editing and redeploying the
agent, which means the replay would be running different code against different
data and proving nothing. So the rules move here, into CEL expressions evaluated
against a context dictionary.

What this buys, concretely: the Gate A threshold is a number in a policy set, not
a constant in a module. Replaying a case with a $100 threshold means loading a
policy set that says 100 and running exactly the same agent code.

**Failing closed.** A rule whose expression will not compile, or which throws
when evaluated, does not silently return false. False means "this restriction
does not apply", and a broken rule quietly meaning that is how a governance
system develops a hole nobody can see. A rule that cannot be evaluated raises,
and callers in the outbound path treat that as a block.

**What is not here.** Rules that are facts about the world rather than policy
choices stay in code: whether a ULID sorts before another, whether a CommsVault
job has finished. Making those swappable would invite a replay that changes
history rather than policy.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import celpy

logger = logging.getLogger(__name__)


class PolicyError(RuntimeError):
    """A rule could not be compiled or evaluated.

    Never caught and turned into a false. See the module docstring.
    """


@dataclass(frozen=True)
class PolicyRule:
    """One governance rule, as an expression rather than a function."""

    rule_id: str
    description: str
    #: A CEL expression returning a boolean. True means the rule fires.
    expression: str
    #: What firing means. "block" stops an action, "escalate" sends it to a human,
    #: "allow" clears something an earlier rule would otherwise have stopped.
    effect: str
    #: Written into the recorded decision when the rule fires. May reference
    #: context values with {curly braces}.
    reason: str
    category: str = "general"

    def render_reason(self, context: Dict[str, Any]) -> str:
        """Fill the reason template from the evaluation context."""
        try:
            return self.reason.format(**context)
        except (KeyError, IndexError, ValueError):
            # A template referring to something absent should not become an
            # exception in the middle of recording a decision.
            return self.reason


@dataclass(frozen=True)
class PolicySet:
    """A named, versioned collection of rules and the constants they read.

    Immutable. Changing a policy means constructing a new set with a new version,
    which is what lets a replay say precisely which rules it ran under.
    """

    policy_set_id: str
    version: str
    description: str
    constants: Dict[str, Any] = field(default_factory=dict)
    rules: Tuple[PolicyRule, ...] = field(default_factory=tuple)

    def rule(self, rule_id: str) -> PolicyRule:
        for candidate in self.rules:
            if candidate.rule_id == rule_id:
                return candidate
        raise PolicyError(f"No rule {rule_id!r} in policy set {self.policy_set_id}")

    def rules_in(self, category: str) -> List[PolicyRule]:
        return [r for r in self.rules if r.category == category]

    def with_constants(self, **overrides: Any) -> "PolicySet":
        """A new policy set with some constants changed.

        This is the Time Machine's main lever. Tightening Gate A from 500 to 100
        is ``policies.with_constants(gate_a_threshold=100)``, and the result is a
        different set with a different version, not a mutated original.
        """
        merged = {**self.constants, **overrides}
        suffix = ",".join(f"{k}={v}" for k, v in sorted(overrides.items()))
        return PolicySet(
            policy_set_id=self.policy_set_id,
            version=f"{self.version}+{suffix}",
            description=f"{self.description} (amended: {suffix})",
            constants=merged,
            rules=self.rules,
        )

    def with_rule(self, rule: PolicyRule) -> "PolicySet":
        """A new policy set with one rule added or replaced."""
        others = tuple(r for r in self.rules if r.rule_id != rule.rule_id)
        return PolicySet(
            policy_set_id=self.policy_set_id,
            version=f"{self.version}+rule:{rule.rule_id}",
            description=f"{self.description} (rule {rule.rule_id} amended)",
            constants=self.constants,
            rules=others + (rule,),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "policy_set_id": self.policy_set_id,
            "version": self.version,
            "description": self.description,
            "constants": self.constants,
            "rules": [
                {
                    "rule_id": r.rule_id,
                    "description": r.description,
                    "expression": r.expression,
                    "effect": r.effect,
                    "reason": r.reason,
                    "category": r.category,
                }
                for r in self.rules
            ],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PolicySet":
        return cls(
            policy_set_id=data["policy_set_id"],
            version=data["version"],
            description=data.get("description", ""),
            constants=data.get("constants", {}),
            rules=tuple(PolicyRule(**r) for r in data.get("rules", [])),
        )

    @classmethod
    def load(cls, path: Path) -> "PolicySet":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


@dataclass
class RuleResult:
    """Whether one rule fired, and what it means."""

    rule: PolicyRule
    fired: bool
    reason: str

    @property
    def rule_id(self) -> str:
        return self.rule.rule_id


class PolicyEngine:
    """Compiles and evaluates a policy set.

    Compiled programs are cached per rule, because the same rule is evaluated on
    every outbound action and recompiling CEL each time would be waste. The cache
    is keyed on the rule's expression, so an amended policy set does not serve a
    stale program.
    """

    def __init__(self, policies: PolicySet):
        self.policies = policies
        self._env = celpy.Environment()
        self._programs: Dict[str, Any] = {}

    def _program(self, rule: PolicyRule):
        cached = self._programs.get(rule.expression)
        if cached is not None:
            return cached
        try:
            program = self._env.program(self._env.compile(rule.expression))
        except Exception as exc:
            raise PolicyError(
                f"Rule {rule.rule_id!r} has an expression that will not compile: {exc}"
            ) from exc
        self._programs[rule.expression] = program
        return program

    def evaluate(self, rule_id: str, context: Dict[str, Any]) -> RuleResult:
        """Evaluate one rule against a context.

        The policy set's constants are merged in underneath the context, so a
        rule can read ``gate_a_threshold`` without every caller remembering to
        pass it. Caller values win, which is what lets a test pin a constant.

        Raises:
            PolicyError: If the rule cannot be compiled or evaluated. Never
                returns a false in place of an error.
        """
        rule = self.policies.rule(rule_id)
        merged = {**self.policies.constants, **context}
        program = self._program(rule)

        try:
            activation = celpy.json_to_cel(merged)
            outcome = program.evaluate(activation)
        except Exception as exc:
            raise PolicyError(
                f"Rule {rule.rule_id!r} could not be evaluated: {exc}. "
                f"Context supplied: {sorted(merged)}"
            ) from exc

        if isinstance(outcome, celpy.CELEvalError):
            raise PolicyError(f"Rule {rule.rule_id!r} evaluated to an error: {outcome}")

        return RuleResult(rule=rule, fired=bool(outcome), reason=rule.render_reason(merged))

    def evaluate_category(self, category: str, context: Dict[str, Any]) -> List[RuleResult]:
        """Evaluate every rule in a category, in the order they are defined."""
        return [
            self.evaluate(rule.rule_id, context)
            for rule in self.policies.rules_in(category)
        ]

    def constant(self, name: str, default: Any = None) -> Any:
        """Read one constant from the policy set."""
        return self.policies.constants.get(name, default)


# ----------------------------------------------------------------------
# The policy set the fleet runs under
# ----------------------------------------------------------------------

DEFAULT_POLICIES = PolicySet(
    policy_set_id="blackbox_default",
    version="1.0.0",
    description="The rules the complaint fleet runs under.",
    constants={
        # Gate A. WORKFLOW.md notes 500 makes the gate fire often, which is good
        # for demonstrating the wait. The Time Machine's headline demonstration
        # is replaying a case with this set to 100.
        "gate_a_threshold": 500.0,
        "appeal_window_days": 30,
        "acknowledgment_due_days": 3,
        "final_response_due_days": 56,
        # How close to the final response deadline a case has to be before the
        # Compliance Officer is asked to look at it.
        "compliance_review_lead_days": 14,
        "restricted_transfer_jurisdictions": ["EU_IE", "EU_DE", "UK"],
        "adequate_regions": ["EU", "EEA"],
    },
    rules=(
        PolicyRule(
            rule_id="gate_a_monetary_threshold",
            description="Remedies above the threshold need an adjudicator's sign-off.",
            expression="remedy_amount > gate_a_threshold",
            effect="escalate",
            reason=(
                "Proposed remedy of {remedy_amount} is above the {gate_a_threshold} "
                "adjudicator threshold, so it needs sign-off."
            ),
            category="approval",
        ),
        PolicyRule(
            rule_id="gate_b_systemic_flag",
            description=(
                "A complaint that may indicate a pattern needs Compliance sign-off "
                "before any customer-facing statement."
            ),
            expression="looks_systemic",
            effect="escalate",
            reason=(
                "The assessment flagged this as possibly affecting other customers, "
                "so Compliance must sign off before the customer is told anything."
            ),
            category="approval",
        ),
        PolicyRule(
            rule_id="pii_high_never_leaves_the_bank",
            description="National identifiers do not leave the bank under any basis.",
            expression="has_pii_high && destination != 'internal'",
            effect="block",
            reason=(
                "This content is derived from a national identifier, which does not "
                "leave the bank's systems under any basis."
            ),
            category="disclosure",
        ),
        PolicyRule(
            rule_id="special_category_third_country_transfer",
            description=(
                "Special category data from a restricted jurisdiction may not go to a "
                "third country without a recorded basis."
            ),
            expression=(
                "has_special_category"
                " && jurisdictions.exists(j, j in restricted_transfer_jurisdictions)"
                " && !(destination_region in adequate_regions)"
                " && !has_adequacy_basis"
            ),
            effect="block",
            reason=(
                "Special category data originating in a restricted jurisdiction would "
                "be transferred to {destination_region}, a third country with no "
                "adequacy basis recorded for this transfer."
            ),
            category="disclosure",
        ),
        PolicyRule(
            rule_id="third_party_pii_not_to_complainant",
            description="Another person's details are not the complainant's to receive.",
            expression="has_third_party_pii && destination == 'customer'",
            effect="block",
            reason=(
                "This content is derived from a record naming someone who is not the "
                "complainant. The bank has no right to disclose their details."
            ),
            category="disclosure",
        ),
        PolicyRule(
            rule_id="special_category_transfer_with_adequacy_basis",
            description="A recorded transfer basis clears the cross-border restriction.",
            expression=(
                "has_special_category"
                " && jurisdictions.exists(j, j in restricted_transfer_jurisdictions)"
                " && !(destination_region in adequate_regions)"
                " && has_adequacy_basis"
            ),
            effect="allow",
            reason=(
                "Special category data is being transferred to {destination_region}, "
                "a third country. An adequacy basis is recorded for this transfer: "
                "{adequacy_basis}."
            ),
            category="disclosure",
        ),
        PolicyRule(
            rule_id="compliance_review_due",
            description="A case close to its final response deadline needs a look.",
            expression=(
                "days_to_final_response <= compliance_review_lead_days"
                " && !final_response_sent"
                " && !holding_letter_sent"
            ),
            effect="escalate",
            reason=(
                "The final response deadline is {days_to_final_response} days away and "
                "no holding letter has been sent."
            ),
            category="deadline",
        ),
    ),
)


_ACTIVE: Optional[PolicyEngine] = None


def get_policy_engine() -> PolicyEngine:
    """The engine the fleet runs under during normal operation.

    A replay does not use this. It constructs its own engine over whichever
    policy set is being tested, which is what keeps a replay from altering the
    rules the live fleet is running.
    """
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = PolicyEngine(DEFAULT_POLICIES)
    return _ACTIVE


def set_policy_engine(engine: Optional[PolicyEngine]) -> None:
    """Replace the active engine. Used by tests, and by nothing else."""
    global _ACTIVE
    _ACTIVE = engine
