"""The exit checks. Nothing leaves without passing through here.

Every outbound path in the system funnels through ``check_disclosure``: letters
to customers, filings to the regulator, and anything else that reaches someone
outside the bank. Checking only the obvious exit is a named failure mode, so the
gateway is enforced in the tools rather than in the prompts, and a test walks the
tool modules to assert no outbound tool bypasses it.

## Rules first, Gemini for the rest

Some questions have correct answers and should not be put to a model. "Is this
special category data being sent to a US vendor with no adequacy basis recorded"
is one of those: it is a lookup, and asking a model to guess at it would be
slower, more expensive, and occasionally wrong.

What a rule cannot decide is whether a particular *paraphrase* of a labelled fact
is an appropriate thing to say to this recipient. A letter that acknowledges "a
difficult personal period" carries a special category label because of where it
came from, but whether saying it is a disclosure depends on what was actually
written and who is reading it. That judgment goes to Gemini, and its reasoning is
recorded as the basis for allowing or blocking.

This ordering matters for a second reason. If Gemini were making every call, the
deterministic protections would inherit the model's variance, and a compliance
control that holds most of the time is not a control. If rules were making every
call, the feature would be a keyword filter wearing the Invisible Ink name, and
it would miss precisely the four-hop paraphrase this system exists to catch.

## Why a keyword filter cannot do this

The letter in the headline case contains no medical vocabulary. It says the bank
is sorry for a difficult year. Every word in it is ordinary. The gateway blocks it
because the label says the sentence descends, through four transformations
including two model calls, from a sentence in which the customer described their
diagnosis. The label is attached to the derivation, not to the words.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .labels import Label, Sensitivity

logger = logging.getLogger(__name__)


class Destination(str, Enum):
    """Where an outbound action is headed."""

    CUSTOMER = "customer"
    REGULATOR = "regulator"
    THIRD_PARTY = "third_party"
    INTERNAL = "internal"


#: Which region each outbound stub operates in. PrintPost is US-based, which is
#: what makes a letter to an EU customer a third-country transfer.
DESTINATION_REGIONS: Dict[str, str] = {
    "PrintPost": "US",
    "RegPortal": "EU",
    "CoreBank": "EU",
    "internal": "EU",
}

#: Jurisdictions whose data may not be transferred to a third country without an
#: adequacy basis being recorded.
RESTRICTED_TRANSFER_JURISDICTIONS = {"EU_IE", "EU_DE", "UK"}

#: Regions considered adequate for those jurisdictions. The US is not on the list,
#: which is the whole point of the headline demonstration.
ADEQUATE_REGIONS = {"EU", "EEA"}


class Decision(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    ESCALATE = "escalate"


@dataclass
class DisclosureRequest:
    """One attempt to send something outside the bank."""

    content: str
    label: Label
    destination: Destination
    destination_system: str
    recipient: str
    purpose: str
    case_id: str
    #: A recorded legal basis for transferring restricted data to a third
    #: country: standard contractual clauses, an adequacy decision, or an
    #: explicit derogation. None means no basis has been recorded, which is not
    #: the same as no basis existing. The point of the block is to make somebody
    #: record it.
    adequacy_basis: Optional[str] = None

    @property
    def destination_region(self) -> str:
        return DESTINATION_REGIONS.get(self.destination_system, "UNKNOWN")


@dataclass
class GatewayVerdict:
    """What the gateway decided, and why."""

    decision: Decision
    rule_id: str
    reasoning: str
    judged_by: str = "rule"
    considered: List[str] = field(default_factory=list)
    #: The POLICY_CHECK event this verdict was recorded as. The taint path query
    #: starts from here.
    event_id: Optional[str] = None

    @property
    def allowed(self) -> bool:
        return self.decision == Decision.ALLOW

    def to_payload(self, request: DisclosureRequest) -> Dict[str, Any]:
        """The POLICY_CHECK payload recording this decision."""
        return {
            "policy_id": self.rule_id,
            "check_type": "data_disclosure",
            "input_data": {
                "destination": request.destination.value,
                "destination_system": request.destination_system,
                "destination_region": request.destination_region,
                "recipient": request.recipient,
                "purpose": request.purpose,
                "label": request.label.to_dict(),
                "content_characters": len(request.content),
                "judged_by": self.judged_by,
                "rules_considered": self.considered,
            },
            "decision": self.decision.value,
            "reasoning": self.reasoning,
        }


# ----------------------------------------------------------------------
# The deterministic rules
# ----------------------------------------------------------------------


def _rule_third_party_pii(request: DisclosureRequest) -> Optional[GatewayVerdict]:
    """Another customer's name is not the complainant's to receive."""
    if request.destination != Destination.CUSTOMER:
        return None
    if not request.label.has(Sensitivity.THIRD_PARTY_PII):
        return None
    return GatewayVerdict(
        Decision.BLOCK,
        "third_party_pii_not_to_complainant",
        "This content is derived from a transaction record naming someone who is "
        "not the complainant. The bank has no right to disclose that person's "
        f"details to them. Sources: {_origins_summary(request.label)}.",
    )


def _rule_pii_high(request: DisclosureRequest) -> Optional[GatewayVerdict]:
    """National identifiers do not leave the bank."""
    if request.destination == Destination.INTERNAL:
        return None
    if not request.label.has(Sensitivity.PII_HIGH):
        return None
    return GatewayVerdict(
        Decision.BLOCK,
        "pii_high_never_leaves_the_bank",
        "This content is derived from a national identifier, which does not leave "
        f"the bank's systems under any basis. Sources: {_origins_summary(request.label)}.",
    )


def _rule_cross_border_special_category(
    request: DisclosureRequest,
) -> Optional[GatewayVerdict]:
    """The headline rule: special category data, restricted origin, third country.

    Four conditions have to hold at once, and the interesting one is the first.
    The content need not mention health. It only has to *descend* from something
    that did.
    """
    if not request.label.has(Sensitivity.SPECIAL_CATEGORY):
        return None

    restricted = request.label.jurisdictions & RESTRICTED_TRANSFER_JURISDICTIONS
    if not restricted:
        return None

    region = request.destination_region
    if region in ADEQUATE_REGIONS:
        return None

    if request.adequacy_basis:
        # The transfer is still restricted. It is now also documented, which is
        # what the rule was asking for. Recorded as an allow with the basis
        # named, so an auditor sees the decision and who stands behind it.
        return GatewayVerdict(
            Decision.ALLOW,
            "special_category_transfer_with_adequacy_basis",
            f"Special category data originating in {', '.join(sorted(restricted))} is "
            f"being transferred to {request.destination_system} in {region}, which is "
            f"a third country. An adequacy basis is recorded for this transfer: "
            f"{request.adequacy_basis}.",
        )

    return GatewayVerdict(
        Decision.BLOCK,
        "special_category_third_country_transfer",
        f"Special category data originating in {', '.join(sorted(restricted))} would "
        f"be transferred to {request.destination_system}, which operates in "
        f"{region}. That is a third country with no adequacy basis recorded for "
        f"this transfer. Note that the content itself contains no health "
        f"vocabulary: it is restricted because of where it is derived from, not "
        f"because of what it says. To proceed, a transfer basis has to be recorded "
        f"against this case by someone willing to stand behind it. "
        f"Sources: {_origins_summary(request.label)}.",
    )


#: Only the unambiguous cases are decided by rule. Each of these has a correct
#: answer that does not depend on how the content is worded:
#:
#:   - a national identifier leaving the bank
#:   - special category data from a restricted jurisdiction crossing to a third
#:     country with no adequacy basis
#:   - a third party's name going to the complainant
#:
#: INTERNAL_ONLY is deliberately *not* here. Every final response letter is
#: derived from the assessment, so a rule that blocked on that derivation would
#: block every letter the bank ever sends. Whether a letter conveys the outcome
#: and its grounds, which the customer is entitled to, or repeats the internal
#: file note, which they are not, is a judgment about the words. That goes to
#: Gemini.
#:
#: Order affects only which reason is reported first. Every rule is evaluated, so
#: content that trips three rules reports all three.
_RULES = [
    _rule_pii_high,
    _rule_cross_border_special_category,
    _rule_third_party_pii,
]


def _origins_summary(label: Label) -> str:
    """A short human-readable list of where a label came from."""
    if not label.origins:
        return "no origin recorded"
    return "; ".join(sorted(o.describe() for o in label.origins))


def apply_rules(request: DisclosureRequest) -> Optional[GatewayVerdict]:
    """Run every deterministic rule. Returns the first block, or None."""
    verdicts = [rule(request) for rule in _RULES]
    fired = [v for v in verdicts if v is not None]
    if not fired:
        return None

    # A rule can also clear a transfer, by naming the basis that permits it. Any
    # block outranks any allow: one rule permitting a transfer says nothing about
    # a different rule forbidding it for an unrelated reason.
    blocks = [v for v in fired if v.decision == Decision.BLOCK]
    if not blocks:
        cleared = fired[0]
        cleared.considered = [v.rule_id for v in fired]
        return cleared

    fired = blocks
    first = fired[0]
    first.considered = [v.rule_id for v in fired]
    if len(fired) > 1:
        others = "; ".join(v.reasoning for v in fired[1:])
        first.reasoning = f"{first.reasoning} Additional grounds: {others}"
    return first


# ----------------------------------------------------------------------
# Gemini as the judge on what rules cannot decide
# ----------------------------------------------------------------------


JUDGE_INSTRUCTION = """
You decide whether a piece of outbound content may be sent, given what it is
derived from and where it is going. A bank's compliance gateway has already
applied its deterministic rules and none of them fired, so this is a judgment
call rather than a lookup.

You will be shown the content, the sensitivity classes it carries, where those
came from, and the destination.

Bear in mind that the label describes the content's *derivation*, not its
wording. Content can carry a strict class while saying nothing sensitive, because
a model read a sensitive source before writing it. That is normal and is not by
itself a reason to block.

Block only if sending this content to this recipient would actually disclose
something they should not receive. Consider:

- Would a reader learn a fact about the customer's health, finances, or
  circumstances that the bank should not be telling them?
- Would a reader learn something about a person who is not the recipient?
- Does it repeat internal reasoning rather than the decision and its grounds?
- For a customer receiving their own data: this is usually fine. Their own name,
  their own account, their own complaint. Do not block a customer from being told
  about their own case.

Answer with a single line beginning ALLOW or BLOCK, then a colon, then one or two
sentences of reasoning. Write the reasoning for a compliance officer who will
read it months from now with no other context.
""".strip()


def build_judge_prompt(request: DisclosureRequest) -> str:
    """Render the question put to Gemini."""
    return (
        f"Content proposed for sending:\n"
        f"<content>\n{request.content}\n</content>\n\n"
        f"Sensitivity classes carried: "
        f"{', '.join(sorted(c.value for c in request.label.classes))}\n"
        f"Jurisdictions: {', '.join(sorted(request.label.jurisdictions)) or 'none recorded'}\n"
        f"Derived from: {_origins_summary(request.label)}\n\n"
        f"Destination: {request.destination.value} via {request.destination_system} "
        f"(operating in {request.destination_region})\n"
        f"Recipient: {request.recipient}\n"
        f"Purpose: {request.purpose}\n\n"
        f"May this be sent?"
    )


def parse_judge_response(text: str) -> GatewayVerdict:
    """Turn Gemini's answer into a verdict.

    An unparseable answer blocks. A gateway that fails open would be worse than
    no gateway, because it would look like a control while permitting anything
    that confused the judge.
    """
    cleaned = (text or "").strip()
    upper = cleaned.upper()

    if upper.startswith("ALLOW"):
        decision = Decision.ALLOW
    elif upper.startswith("BLOCK"):
        decision = Decision.BLOCK
    else:
        return GatewayVerdict(
            Decision.BLOCK,
            "gemini_judge_unparseable",
            f"The judge's answer did not begin with ALLOW or BLOCK, so the "
            f"disclosure was blocked rather than guessed at. Answer was: "
            f"{cleaned[:300] or '(empty)'}",
            judged_by="gemini",
        )

    _, _, reasoning = cleaned.partition(":")
    return GatewayVerdict(
        decision,
        "gemini_judge",
        reasoning.strip() or cleaned,
        judged_by="gemini",
    )


async def ask_gemini_judge(request: DisclosureRequest, model: Optional[Any] = None) -> GatewayVerdict:
    """Put an ambiguous disclosure to Gemini and record what it says."""
    from google.adk.agents import LlmAgent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    from .config import get_settings

    settings = get_settings()
    judge = LlmAgent(
        name="disclosure_judge",
        model=model or settings.gemini_model,
        description="Decides whether labelled content may be disclosed to a destination.",
        instruction=JUDGE_INSTRUCTION,
    )

    session_service = InMemorySessionService()
    session_id = f"judge:{request.case_id}"
    await session_service.create_session(
        app_name="blackbox-gateway", user_id="gateway", session_id=session_id
    )
    runner = Runner(app_name="blackbox-gateway", agent=judge, session_service=session_service)
    message = types.Content(role="user", parts=[types.Part(text=build_judge_prompt(request))])

    answer = ""
    try:
        async for event in runner.run_async(
            user_id="gateway", session_id=session_id, new_message=message
        ):
            if event.is_final_response() and event.content and event.content.parts:
                answer = "".join(p.text or "" for p in event.content.parts).strip()
    except Exception as exc:
        # The judge being unreachable must not become an allow.
        logger.exception("Disclosure judge failed")
        return GatewayVerdict(
            Decision.BLOCK,
            "gemini_judge_unavailable",
            f"The disclosure judge could not be reached, so the disclosure was "
            f"blocked rather than allowed by default: {exc}",
            judged_by="gemini",
        )

    return parse_judge_response(answer)


# ----------------------------------------------------------------------
# The one entry point
# ----------------------------------------------------------------------


async def check_disclosure(
    request: DisclosureRequest,
    recorder: Any,
    model: Optional[Any] = None,
    use_judge: bool = True,
) -> GatewayVerdict:
    """Decide whether something may leave, and record the decision either way.

    Rules run first. If one blocks, that is the answer and no model call is made.
    If none fires and the content carries anything beyond public data, Gemini
    judges it. Public content is allowed without a model call, because there is
    nothing to judge.

    Args:
        request: What is being sent, where, and what it is derived from.
        recorder: Writes the POLICY_CHECK event.
        model: Override the judge's model. Used by tests.
        use_judge: Skip the model and allow on no rule match. Only for tests
            that are exercising the rules themselves.

    Returns:
        The verdict. Callers must not send when ``allowed`` is false.
    """
    verdict = apply_rules(request)

    if verdict is None:
        if request.label.is_public or not use_judge:
            verdict = GatewayVerdict(
                Decision.ALLOW,
                "no_rule_applies",
                "No sensitivity class on this content restricts it from this "
                "destination.",
                considered=[],
            )
        else:
            verdict = await ask_gemini_judge(request, model=model)

    # Recorded whether allowed or blocked. An allow is a decision too, and a
    # gateway that only logs its refusals cannot answer "was this ever checked".
    payload = verdict.to_payload(request)
    verdict.event_id = recorder.policy_check(
        policy_id=payload["policy_id"],
        check_type=payload["check_type"],
        input_data=payload["input_data"],
        decision=payload["decision"],
        reasoning=payload["reasoning"],
        labels=request.label.to_dict(),
    )

    return verdict
