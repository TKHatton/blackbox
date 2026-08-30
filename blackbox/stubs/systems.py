"""The three source systems, as stubs with deliberate personality.

Nothing external is integrated. Each stub behaves the way its real counterpart
would in the ways that matter to the fleet:

- CoreBank answers immediately, and its transaction records carry third-party
  names the bank may not disclose to the complainant.
- CRM360 answers immediately, and is the origin of vulnerability flags, which are
  special category data.
- CommsVault does not answer. It returns a job id and makes the caller come back
  days later. This is what turns step 5 of the workflow into a genuine
  asynchronous wait rather than a simulated one, and Phase 3 builds suspend and
  resume on top of it.

Every method returns a plain dictionary with an ``origin`` marker so the Flight
Recorder can record where a fact came from, and Phase 4 has a field to label.
"""

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import data


class SourceSystemError(RuntimeError):
    """Raised when a stub cannot answer. Phase 9 injects these deliberately."""


class CoreBank:
    """Accounts, transactions, fees, balances. Sub-second responses."""

    name = "CoreBank"

    def get_account(self, account_id: str) -> Dict[str, Any]:
        """Return the account record, including its domicile."""
        account = data.ACCOUNTS.get(account_id)
        if account is None:
            raise SourceSystemError(f"CoreBank has no account {account_id}")
        return {
            "origin": self.name,
            "sensitivity": "FINANCIAL",
            "record": dict(account),
        }

    def get_transactions(self, account_id: str, fees_only: bool = False) -> Dict[str, Any]:
        """Return transactions for an account.

        Records that name a counterparty carry THIRD_PARTY_PII on that field.
        The name is returned rather than stripped here on purpose: the gateway
        decides what may leave, not the source system, and the trail has to
        start somewhere real for the taint path to be worth showing.
        """
        if account_id not in data.ACCOUNTS:
            raise SourceSystemError(f"CoreBank has no account {account_id}")
        rows = data.TRANSACTIONS.get(account_id, [])
        if fees_only:
            rows = [r for r in rows if r.get("fee")]
        return {
            "origin": self.name,
            "sensitivity": "FINANCIAL",
            "records": [dict(r) for r in rows],
            "contains_third_party_pii": any(r.get("counterparty_name") for r in rows),
        }


class CRM360:
    """Customer profile, contact history, preferences, vulnerability flags."""

    name = "CRM360"

    def get_customer(self, customer_id: str) -> Dict[str, Any]:
        """Return the customer profile.

        The strictest field present sets the sensitivity of the whole record,
        which is the same combination rule Phase 4 formalises for labels.
        """
        customer = data.CUSTOMERS.get(customer_id)
        if customer is None:
            raise SourceSystemError(f"CRM360 has no customer {customer_id}")

        field_sensitivity = customer.get("field_sensitivity", {})
        strictest = "PII"
        for value in field_sensitivity.values():
            if data.SENSITIVITY_ORDER.index(value) > data.SENSITIVITY_ORDER.index(strictest):
                strictest = value

        return {
            "origin": self.name,
            "sensitivity": strictest,
            "record": {k: v for k, v in customer.items() if k != "field_sensitivity"},
            "field_sensitivity": dict(field_sensitivity),
        }


class CommsVault:
    """Archived email, call recordings, and transcripts.

    Deliberately slow. A request returns a job id and a ready-at time two to
    three days out. Polling before then returns PENDING, which is what a
    suspended agent will wake up to re-check in Phase 3.
    """

    name = "CommsVault"
    min_delay_days = 2
    max_delay_days = 3

    def __init__(self) -> None:
        # Jobs live in process for Phase 2. Phase 3 moves the wake condition into
        # a SUSPEND event so a restart does not lose pending work.
        self._jobs: Dict[str, Dict[str, Any]] = {}

    def request_records(self, customer_id: str, reason: str) -> Dict[str, Any]:
        """Open a retrieval job. Returns a job id, never the records."""
        if customer_id not in data.ARCHIVED_COMMS:
            raise SourceSystemError(f"CommsVault has no archive for {customer_id}")

        # Deterministic delay so a replay of the same request produces the same
        # ready-at time. A random delay would make Phase 6 replays diverge for
        # reasons that have nothing to do with the policy under test.
        digest = hashlib.sha256(customer_id.encode()).digest()[0]
        span = self.max_delay_days - self.min_delay_days + 1
        delay_days = self.min_delay_days + (digest % span)

        job_id = f"CV-JOB-{customer_id[-4:]}-{digest:02x}"
        ready_at = datetime.now(timezone.utc) + timedelta(days=delay_days)
        self._jobs[job_id] = {
            "customer_id": customer_id,
            "reason": reason,
            "ready_at": ready_at,
        }

        return {
            "origin": self.name,
            "status": "ACCEPTED",
            "job_id": job_id,
            "ready_at": ready_at.isoformat(),
            "estimated_delay_days": delay_days,
            "note": "CommsVault returns results asynchronously. Poll with the job id.",
        }

    def poll(self, job_id: str, as_of: Optional[datetime] = None) -> Dict[str, Any]:
        """Check a retrieval job. PENDING until its ready-at time has passed."""
        job = self._jobs.get(job_id)
        if job is None:
            raise SourceSystemError(f"CommsVault has no job {job_id}")

        now = as_of or datetime.now(timezone.utc)
        if now < job["ready_at"]:
            return {
                "origin": self.name,
                "status": "PENDING",
                "job_id": job_id,
                "ready_at": job["ready_at"].isoformat(),
            }

        records: List[Dict[str, Any]] = [
            dict(r) for r in data.ARCHIVED_COMMS.get(job["customer_id"], [])
        ]
        strictest = "INTERNAL_ONLY"
        for record in records:
            candidate = record.get("sensitivity", "INTERNAL_ONLY")
            if candidate in data.SENSITIVITY_ORDER and data.SENSITIVITY_ORDER.index(
                candidate
            ) > data.SENSITIVITY_ORDER.index(strictest):
                strictest = candidate

        return {
            "origin": self.name,
            "status": "READY",
            "job_id": job_id,
            "sensitivity": strictest,
            "records": records,
        }


class PrintPost:
    """Letter fulfilment vendor. US-based operations.

    Outbound only. This is the destination that makes cross-border transfer a
    live constraint in Phase 4, which is why its region is recorded explicitly.
    """

    name = "PrintPost"
    region = "US"

    def send_letter(self, recipient: str, body: str) -> Dict[str, Any]:
        """Queue a physical letter. Phase 4 gates this behind the exit check."""
        return {
            "origin": self.name,
            "destination_region": self.region,
            "status": "QUEUED",
            "recipient": recipient,
            "characters": len(body),
        }


class RegPortal:
    """The regulator filing endpoint. Outbound only."""

    name = "RegPortal"

    def file_report(self, jurisdiction: str, summary: str) -> Dict[str, Any]:
        """File a report with the regulator for a jurisdiction."""
        return {
            "origin": self.name,
            "status": "FILED",
            "jurisdiction": jurisdiction,
            "reference": f"REG-{jurisdiction}-{abs(hash(summary)) % 100000:05d}",
        }


class SourceSystems:
    """One handle for the whole stub estate.

    Held for the life of the process so CommsVault job ids survive between
    requests within an instance.
    """

    def __init__(self) -> None:
        self.corebank = CoreBank()
        self.crm360 = CRM360()
        self.commsvault = CommsVault()
        self.printpost = PrintPost()
        self.regportal = RegPortal()


_SYSTEMS: Optional[SourceSystems] = None


def get_source_systems() -> SourceSystems:
    """Process-wide stub estate."""
    global _SYSTEMS
    if _SYSTEMS is None:
        _SYSTEMS = SourceSystems()
    return _SYSTEMS
