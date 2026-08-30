"""Invisible Ink: the label schema and the rule for combining labels.

Sensitive data carries a stamp that survives summarizing, merging, and rephrasing.
This module is where the stamp is defined and where two stamps are combined. The
combination rule is the part most worth reading carefully, because getting it
backwards is a silent failure that looks fine in testing: the system keeps
working, keeps allowing things, and only the blocks it should have made go
missing.

## Why sensitivity is a set and not a single value

The obvious design is one sensitivity value per label, ordered from least to most
restrictive, and combination takes the maximum. That design is wrong here, and it
is wrong in a way that quietly under-restricts.

These classes are not points on a line. They are different *kinds* of
restriction:

- ``INTERNAL_ONLY`` means never to a customer. It says nothing about borders.
- ``THIRD_PARTY_PII`` means not to *this* complainant. The person it belongs to
  could receive it.
- ``PII_HIGH`` means it does not leave the bank at all.
- ``SPECIAL_CATEGORY`` means health and similar, and it is what makes a
  cross-border transfer need an adequacy basis.

Ask which of ``INTERNAL_ONLY`` and ``PII_HIGH`` is "higher" and the question has
no answer. Force them onto one axis and one of the two restrictions is lost. So a
label carries a **set** of classes, and combining two labels takes the union.

The set is kept as a maximal antichain: if one class in the set already implies
another, the implied one is dropped. ``{PII, SPECIAL_CATEGORY}`` reduces to
``{SPECIAL_CATEGORY}`` because special category data is already personal data.
That keeps the set small without losing a single restriction, and it makes the
join idempotent, commutative, and associative, which is what makes it a lattice
join rather than merely a merge function.

## The direction that matters

Combination always moves towards *more* restrictive. Union never removes a class.
Jurisdictions union, so data derived from EU and UK sources is subject to both.
Retention takes the shortest window. There is no operation in this module that
makes a label less restrictive than either of its inputs, and a test asserts it.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, Iterable, Optional, Set, Tuple


class Sensitivity(str, Enum):
    """The sensitivity classes in play in this workflow.

    Drawn from the field table in WORKFLOW.md. Each one names a different reason
    a piece of data might not be allowed somewhere.
    """

    #: No restriction. The bank's published fee schedule, for example.
    PUBLIC = "PUBLIC"
    #: Never reaches a customer. Assessment reasoning lives here.
    INTERNAL_ONLY = "INTERNAL_ONLY"
    #: Ordinary personal data: name, address, date of birth.
    PII = "PII"
    #: Account and transaction records.
    FINANCIAL = "FINANCIAL"
    #: Free text nobody has examined yet. A complaint narrative is this until it
    #: has been read, because it may contain anything, including the two classes
    #: below.
    MIXED = "MIXED"
    #: Names of people who are not the complainant, found in transaction records.
    #: The bank has no right to disclose these to the complainant.
    THIRD_PARTY_PII = "THIRD_PARTY_PII"
    #: National identifiers. Never leaves the bank's systems.
    PII_HIGH = "PII_HIGH"
    #: Health, hardship, vulnerability. The strictest class, and the one that
    #: makes a third-country transfer need an adequacy basis.
    SPECIAL_CATEGORY = "SPECIAL_CATEGORY"


# The partial order. Each entry says: this class already implies these others, so
# holding it makes holding them redundant. Read "A: {B}" as "A is at least as
# restrictive as B in every respect".
#
# Deliberately sparse. PII_HIGH and SPECIAL_CATEGORY are not related to each
# other, and neither is related to INTERNAL_ONLY, because they restrict different
# things. Inventing an order between them is exactly the mistake this module
# exists to avoid.
_IMPLIES: Dict[Sensitivity, Set[Sensitivity]] = {
    Sensitivity.PUBLIC: set(),
    Sensitivity.INTERNAL_ONLY: {Sensitivity.PUBLIC},
    Sensitivity.PII: {Sensitivity.PUBLIC},
    Sensitivity.FINANCIAL: {Sensitivity.PUBLIC},
    # Unexamined free text is treated as at least personal data, because it
    # usually is, and because assuming otherwise is the unsafe direction.
    Sensitivity.MIXED: {Sensitivity.PII, Sensitivity.PUBLIC},
    Sensitivity.THIRD_PARTY_PII: {Sensitivity.PII, Sensitivity.PUBLIC},
    Sensitivity.PII_HIGH: {Sensitivity.PII, Sensitivity.PUBLIC},
    Sensitivity.SPECIAL_CATEGORY: {Sensitivity.PII, Sensitivity.PUBLIC},
}


def implies(a: Sensitivity, b: Sensitivity) -> bool:
    """True if holding ``a`` already covers every restriction ``b`` imposes."""
    return a == b or b in _IMPLIES.get(a, set())


def reduce_classes(classes: Iterable[Sensitivity]) -> FrozenSet[Sensitivity]:
    """Drop classes that another class in the set already implies.

    ``{PII, SPECIAL_CATEGORY}`` becomes ``{SPECIAL_CATEGORY}``: nothing is lost,
    because special category data is personal data. ``{INTERNAL_ONLY, PII_HIGH}``
    stays as it is, because neither implies the other.

    An empty set reduces to ``{PUBLIC}`` rather than staying empty, so a label
    always names at least one class and no caller has to handle the empty case.
    """
    candidates = {c for c in classes if c != Sensitivity.PUBLIC}
    if not candidates:
        return frozenset({Sensitivity.PUBLIC})

    keep = set()
    for candidate in candidates:
        # Keep it unless some *other* class in the set already implies it.
        if not any(other != candidate and implies(other, candidate) for other in candidates):
            keep.add(candidate)
    return frozenset(keep)


@dataclass(frozen=True)
class Provenance:
    """Where one piece of a label came from.

    This is what makes the taint path answerable. Without ``event_id``, a blocked
    action could say "this is special category data" but not "and here is the
    sentence in the complaint it came from, four hops back".
    """

    system: str
    field: str
    event_id: Optional[str] = None
    note: str = ""

    def describe(self) -> str:
        where = f"{self.system}.{self.field}"
        return f"{where} ({self.note})" if self.note else where


# Retention windows, shortest first. Combination takes the shortest, because a
# derived fact cannot outlive the strictest rule that applies to its sources.
_RETENTION_DAYS: Dict[str, int] = {
    "delete_on_closure": 0,
    "retain_30_days": 30,
    "retain_1_year": 365,
    "retain_6_years": 2190,
    "retain_indefinitely": 10**6,
}


@dataclass(frozen=True)
class Label:
    """The stamp carried by a piece of data.

    Four parts, as the spec requires: origin, sensitivity, jurisdiction, and
    retention rule. Origin and jurisdiction are sets because a derived fact
    genuinely can come from two systems and be subject to two regimes at once.
    """

    classes: FrozenSet[Sensitivity] = field(default_factory=lambda: frozenset({Sensitivity.PUBLIC}))
    jurisdictions: FrozenSet[str] = field(default_factory=frozenset)
    origins: FrozenSet[Provenance] = field(default_factory=frozenset)
    retention: Optional[str] = None

    @classmethod
    def make(
        cls,
        classes: Optional[Iterable[Sensitivity]] = None,
        jurisdictions: Optional[Iterable[str]] = None,
        origins: Optional[Iterable[Provenance]] = None,
        retention: Optional[str] = None,
    ) -> "Label":
        """Build a label, reducing the class set."""
        return cls(
            classes=reduce_classes(classes or []),
            jurisdictions=frozenset(jurisdictions or []),
            origins=frozenset(origins or []),
            retention=retention,
        )

    @classmethod
    def public(cls) -> "Label":
        """The bottom of the lattice. Combining with it changes nothing."""
        return cls.make()

    # ------------------------------------------------------------------
    # The combination rule
    # ------------------------------------------------------------------

    def join(self, other: "Label") -> "Label":
        """Combine two labels, taking the strictest of each part.

        This is the operation the whole feature rests on. Every part moves
        towards more restrictive and none towards less:

        - classes: union, then reduced. Never drops a restriction.
        - jurisdictions: union. Data from EU and UK sources answers to both.
        - origins: union. The trail back is never shortened.
        - retention: the shorter window. A derived fact cannot outlive the
          strictest rule covering its sources.
        """
        return Label(
            classes=reduce_classes(self.classes | other.classes),
            jurisdictions=self.jurisdictions | other.jurisdictions,
            origins=self.origins | other.origins,
            retention=_stricter_retention(self.retention, other.retention),
        )

    def with_origin(self, provenance: Provenance) -> "Label":
        """Add one provenance record."""
        return Label(
            classes=self.classes,
            jurisdictions=self.jurisdictions,
            origins=self.origins | {provenance},
            retention=self.retention,
        )

    # ------------------------------------------------------------------
    # Reading a label
    # ------------------------------------------------------------------

    def has(self, sensitivity: Sensitivity) -> bool:
        """True if this label carries a class at least as strict as the one given.

        Use this rather than ``sensitivity in label.classes``: a label reduced to
        ``{SPECIAL_CATEGORY}`` still carries the PII restriction, and a check
        written against the raw set would miss it.
        """
        return any(implies(c, sensitivity) for c in self.classes)

    @property
    def peak(self) -> Sensitivity:
        """One class to show a human, chosen for display only.

        Never make a decision on this. It collapses a set into a single value,
        which is precisely what the class set exists to avoid. Gateway rules use
        ``has``.
        """
        order = [
            Sensitivity.SPECIAL_CATEGORY,
            Sensitivity.PII_HIGH,
            Sensitivity.THIRD_PARTY_PII,
            Sensitivity.INTERNAL_ONLY,
            Sensitivity.MIXED,
            Sensitivity.FINANCIAL,
            Sensitivity.PII,
            Sensitivity.PUBLIC,
        ]
        for candidate in order:
            if candidate in self.classes:
                return candidate
        return Sensitivity.PUBLIC

    @property
    def is_public(self) -> bool:
        return self.classes == frozenset({Sensitivity.PUBLIC})

    def dominates(self, other: "Label") -> bool:
        """True if this label is at least as restrictive as ``other`` throughout.

        Used by the tests to assert that combining labels never loosens one.
        """
        every_class_covered = all(
            any(implies(mine, theirs) for mine in self.classes) for theirs in other.classes
        )
        return (
            every_class_covered
            and other.jurisdictions <= self.jurisdictions
            and other.origins <= self.origins
            and _stricter_retention(self.retention, other.retention) == self.retention
        )

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for the ``labels`` field on an event."""
        return {
            "classes": sorted(c.value for c in self.classes),
            "jurisdictions": sorted(self.jurisdictions),
            "origins": [
                {
                    "system": o.system,
                    "field": o.field,
                    "event_id": o.event_id,
                    "note": o.note,
                }
                for o in sorted(self.origins, key=lambda p: (p.system, p.field, p.event_id or ""))
            ],
            "retention": self.retention,
            "peak": self.peak.value,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "Label":
        """Rebuild a label from an event's ``labels`` field."""
        if not data:
            return cls.public()
        return cls(
            classes=reduce_classes(Sensitivity(c) for c in data.get("classes", [])),
            jurisdictions=frozenset(data.get("jurisdictions", [])),
            origins=frozenset(
                Provenance(
                    system=o.get("system", "unknown"),
                    field=o.get("field", "unknown"),
                    event_id=o.get("event_id"),
                    note=o.get("note", ""),
                )
                for o in data.get("origins", [])
            ),
            retention=data.get("retention"),
        )

    def describe(self) -> str:
        """A one-line summary for a human reading a block decision."""
        classes = ", ".join(sorted(c.value for c in self.classes))
        jurisdictions = ", ".join(sorted(self.jurisdictions)) or "none recorded"
        return f"[{classes}] jurisdiction: {jurisdictions}, {len(self.origins)} source(s)"


def _stricter_retention(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """The shorter of two retention windows. None means no rule recorded."""
    if a is None:
        return b
    if b is None:
        return a
    return a if _RETENTION_DAYS.get(a, 10**6) <= _RETENTION_DAYS.get(b, 10**6) else b


def join_all(labels: Iterable[Label]) -> Label:
    """Combine any number of labels. The identity is the public label."""
    result = Label.public()
    for label in labels:
        result = result.join(label)
    return result


def lattice_is_well_formed() -> Tuple[bool, str]:
    """Check the implication relation is a partial order.

    Reflexive, antisymmetric, and transitive. A cycle here would make
    ``reduce_classes`` drop a restriction it should have kept, which is the
    silent under-restriction this whole module is arranged to prevent, so the
    property is checked rather than assumed.
    """
    members = list(Sensitivity)

    for a in members:
        if not implies(a, a):
            return False, f"not reflexive at {a}"

    for a in members:
        for b in members:
            if a != b and implies(a, b) and implies(b, a):
                return False, f"not antisymmetric: {a} and {b} imply each other"

    for a in members:
        for b in members:
            for c in members:
                if implies(a, b) and implies(b, c) and not implies(a, c):
                    return False, f"not transitive: {a} -> {b} -> {c}"

    return True, "well formed"
