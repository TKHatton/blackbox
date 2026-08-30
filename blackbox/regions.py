"""Region pinning: where a Wiki page is allowed to be read.

The failure mode this module exists to avoid is region pinning implemented as a
config label with no enforcement, which is documentation rather than a control.
So the check lives in the read path of the Wiki store. A worker in the wrong
region does not get a warning and the page. It gets a refusal and no page.

The distinction that matters: this is not about which agent is allowed to know
something. It is about which *machine* the bytes are allowed to travel to. An
EU-pinned page can be read by any agent in the fleet, as long as the instance
doing the reading is running in a region that may hold EU personal data. The same
agent running on a US instance is refused, and the refusal is recorded with its
reasoning so the routing decision is auditable rather than invisible.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Which storage region each jurisdiction's data must stay in.
JURISDICTION_REGIONS: Dict[str, str] = {
    "EU_IE": "EU",
    "EU_DE": "EU",
    "UK": "UK",
    "US": "US",
    "US_CA": "US",
}

#: Which regions a worker in a given region may read from. A worker may always
#: read its own region. UK and EU are treated as mutually adequate here, which is
#: a simplification stated rather than hidden. The US is adequate for neither.
REGION_MAY_READ: Dict[str, set] = {
    "EU": {"EU", "UK", "US"},
    "UK": {"UK", "EU", "US"},
    "US": {"US"},
}

DEFAULT_REGION = "US"


class RegionRoutingRefused(RuntimeError):
    """Raised when a read would move data across a border it may not cross.

    An exception rather than a filtered result. A caller that got an empty answer
    would carry on with a gap it could not see, and an agent working from a page
    it silently failed to read is worse than one that stopped.
    """

    def __init__(self, page_id: str, page_region: str, worker_region: str, reasoning: str):
        self.page_id = page_id
        self.page_region = page_region
        self.worker_region = worker_region
        self.reasoning = reasoning
        super().__init__(reasoning)


@dataclass
class RoutingDecision:
    """Whether a worker may read a page, and why."""

    allowed: bool
    page_id: str
    page_region: str
    worker_region: str
    reasoning: str

    def to_policy_check(self) -> Dict[str, Any]:
        return {
            "policy_id": "region_pinning",
            "check_type": "data_transfer",
            "input_data": {
                "page_id": self.page_id,
                "page_region": self.page_region,
                "worker_region": self.worker_region,
            },
            "decision": "allow" if self.allowed else "block",
            "reasoning": self.reasoning,
        }


def region_for_jurisdiction(jurisdiction: Optional[str]) -> str:
    """Which storage region a jurisdiction's data belongs in.

    An unrecognised jurisdiction gets the strictest treatment rather than the
    loosest: it is pinned to the EU, so an unknown value cannot become a way of
    moving data to a US worker.
    """
    if not jurisdiction:
        return "EU"
    return JURISDICTION_REGIONS.get(jurisdiction, "EU")


def may_read(page_region: str, worker_region: str) -> bool:
    """True if a worker in one region may read data pinned to another."""
    return page_region in REGION_MAY_READ.get(worker_region, set())


def evaluate_routing(
    page_id: str, jurisdiction: Optional[str], worker_region: str
) -> RoutingDecision:
    """Decide whether this worker may read this page."""
    page_region = region_for_jurisdiction(jurisdiction)

    if may_read(page_region, worker_region):
        return RoutingDecision(
            True,
            page_id,
            page_region,
            worker_region,
            f"A worker in {worker_region} may hold data pinned to {page_region}.",
        )

    return RoutingDecision(
        False,
        page_id,
        page_region,
        worker_region,
        f"{page_id} holds data pinned to {page_region} because its jurisdiction is "
        f"{jurisdiction or 'unrecorded'}. This worker runs in {worker_region}, which "
        f"is not an adequate destination for {page_region} personal data, so reading "
        f"the page here would move it across a border it may not cross. The work was "
        f"not done. Route it to a {page_region} instance instead.",
    )
