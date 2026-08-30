"""Synthetic data behind the three stub source systems.

None of this is real. It is shaped to exercise the constraints in WORKFLOW.md:
mixed jurisdictions, a customer whose country of residence disagrees with their
account domicile, special category health disclosures, and third-party names
sitting inside transaction records where the bank has no right to disclose them.

Phase 4 reads the ``sensitivity`` markers here as the origin labels for Invisible
Ink. Phase 2 only carries them along, but they are placed now so the labels have
a real source to point back at rather than being invented later.
"""

from typing import Any, Dict, List

# Sensitivity classes, loosest to strictest. Phase 4 turns this into the ordered
# lattice; here it is documentation attached to the data it describes.
SENSITIVITY_ORDER = [
    "PUBLIC",
    "INTERNAL_ONLY",
    "PII",
    "FINANCIAL",
    "THIRD_PARTY_PII",
    "PII_HIGH",
    "SPECIAL_CATEGORY",
]


CUSTOMERS: Dict[str, Dict[str, Any]] = {
    "CUST-4471": {
        "customer_id": "CUST-4471",
        "name": "Aoife Brennan",
        "date_of_birth": "1979-03-14",
        "address": "12 Merrion Row, Dublin 2, Ireland",
        "country_of_residence": "EU_IE",
        "national_identifier": "IE-7741932T",
        "email": "a.brennan@example.ie",
        "preferred_channel": "post",
        "vulnerability_flags": ["financial_hardship"],
        "prior_complaints": 0,
        "field_sensitivity": {
            "name": "PII",
            "date_of_birth": "PII",
            "address": "PII",
            "national_identifier": "PII_HIGH",
            "vulnerability_flags": "SPECIAL_CATEGORY",
        },
    },
    "CUST-1180": {
        "customer_id": "CUST-1180",
        "name": "Marcus Webb",
        "date_of_birth": "1991-11-02",
        "address": "884 Bryant Street, San Francisco, CA 94103, USA",
        "country_of_residence": "US_CA",
        "national_identifier": "SSN-***-**-4417",
        "email": "m.webb@example.com",
        "preferred_channel": "email",
        "vulnerability_flags": [],
        "prior_complaints": 2,
        "field_sensitivity": {
            "name": "PII",
            "date_of_birth": "PII",
            "address": "PII",
            "national_identifier": "PII_HIGH",
        },
    },
    "CUST-9032": {
        "customer_id": "CUST-9032",
        "name": "Priya Raghunathan",
        "date_of_birth": "1965-07-21",
        "address": "41 Colmore Row, Birmingham B3 2BJ, United Kingdom",
        "country_of_residence": "UK",
        "national_identifier": "NI-QQ123456C",
        "email": "p.raghunathan@example.co.uk",
        "preferred_channel": "email",
        "vulnerability_flags": ["bereavement"],
        "prior_complaints": 1,
        "field_sensitivity": {
            "name": "PII",
            "date_of_birth": "PII",
            "address": "PII",
            "national_identifier": "PII_HIGH",
            "vulnerability_flags": "SPECIAL_CATEGORY",
        },
    },
}


ACCOUNTS: Dict[str, Dict[str, Any]] = {
    "ACC-88214": {
        "account_id": "ACC-88214",
        "customer_id": "CUST-4471",
        # Resident in Ireland, account domiciled in the UK. The jurisdiction
        # question has to be decided rather than assumed.
        "domicile": "UK",
        "product": "personal_current_account",
        "opened": "2016-04-02",
        "balance": -412.55,
        "currency": "EUR",
        "arrears_status": "2_payments_missed",
    },
    "ACC-30117": {
        "account_id": "ACC-30117",
        "customer_id": "CUST-1180",
        "domicile": "US",
        "product": "credit_card",
        "opened": "2020-09-18",
        "balance": 2841.09,
        "currency": "USD",
        "arrears_status": "current",
    },
    "ACC-55902": {
        "account_id": "ACC-55902",
        "customer_id": "CUST-9032",
        "domicile": "UK",
        "product": "savings_account",
        "opened": "2011-01-30",
        "balance": 18402.13,
        "currency": "GBP",
        "arrears_status": "current",
    },
}


TRANSACTIONS: Dict[str, List[Dict[str, Any]]] = {
    "ACC-88214": [
        {
            "transaction_id": "TXN-771043",
            "date": "2026-07-02",
            "description": "ARREARS MANAGEMENT FEE",
            "amount": -35.00,
            "fee": True,
        },
        {
            "transaction_id": "TXN-771088",
            "date": "2026-07-14",
            "description": "ARREARS MANAGEMENT FEE",
            "amount": -35.00,
            "fee": True,
        },
        {
            "transaction_id": "TXN-771120",
            "date": "2026-07-28",
            "description": "ARREARS MANAGEMENT FEE",
            "amount": -35.00,
            "fee": True,
        },
        {
            "transaction_id": "TXN-771156",
            "date": "2026-08-03",
            "description": "UNPAID DIRECT DEBIT FEE",
            "amount": -12.00,
            "fee": True,
        },
    ],
    "ACC-30117": [
        {
            "transaction_id": "TXN-660221",
            "date": "2026-08-11",
            "description": "POS PURCHASE - NORTHGATE AUTOMOTIVE",
            "amount": -1240.00,
            "fee": False,
            # A disputed transaction whose record names another customer. The
            # bank has no right to disclose this name to the complainant, which
            # is the shorter Invisible Ink demonstration in WORKFLOW.md.
            "counterparty_name": "D. Okonkwo",
            "counterparty_sensitivity": "THIRD_PARTY_PII",
        },
        {
            "transaction_id": "TXN-660255",
            "date": "2026-08-12",
            "description": "LATE PAYMENT FEE",
            "amount": -39.00,
            "fee": True,
        },
    ],
    "ACC-55902": [
        {
            "transaction_id": "TXN-410882",
            "date": "2026-06-30",
            "description": "INTEREST CREDIT",
            "amount": 18.44,
            "fee": False,
        }
    ],
}


# CommsVault holds archived email and call transcripts. It answers with a job id
# rather than results, so nothing here is returned synchronously.
ARCHIVED_COMMS: Dict[str, List[Dict[str, Any]]] = {
    "CUST-4471": [
        {
            "record_id": "CV-99120",
            "type": "call_transcript",
            "date": "2026-07-09",
            "summary": (
                "Customer called to explain she is undergoing treatment and has "
                "reduced income. Agent did not record a hardship flag."
            ),
            "sensitivity": "SPECIAL_CATEGORY",
        }
    ],
    "CUST-1180": [
        {
            "record_id": "CV-88410",
            "type": "email",
            "date": "2026-08-13",
            "summary": "Customer reported a card transaction he does not recognise.",
            "sensitivity": "MIXED",
        }
    ],
    "CUST-9032": [],
}


# The inbound queue the poller drains. In production these would arrive from an
# email gateway and a web form. Nobody presses a button to submit them.
INBOUND_COMPLAINTS: List[Dict[str, Any]] = [
    {
        "complaint_ref": "CMP-2026-0841",
        "received_at": "2026-08-28T09:12:00+00:00",
        "channel": "web_form",
        "customer_id": "CUST-4471",
        "account_id": "ACC-88214",
        "narrative": (
            "I am writing because I have been charged an arrears management fee "
            "three months running and nobody told me this would happen. I rang in "
            "July and explained that I was diagnosed with cancer in May and have "
            "been on reduced pay through my treatment, so the payments slipped. "
            "The person I spoke to said it would be noted. It clearly was not, "
            "because the fees kept coming. I want the fees returned and I want "
            "someone to actually read my file this time."
        ),
    },
    {
        "complaint_ref": "CMP-2026-0842",
        "received_at": "2026-08-29T14:47:00+00:00",
        "channel": "email",
        "customer_id": "CUST-1180",
        "account_id": "ACC-30117",
        "narrative": (
            "There is a charge on my card for $1,240 at an auto shop I have never "
            "been to, dated August 11. I reported it two days later and was told "
            "to wait. Since then you have added a late payment fee of $39 for the "
            "disputed amount. I want the charge reversed and the fee removed."
        ),
    },
    {
        "complaint_ref": "CMP-2026-0843",
        "received_at": "2026-08-29T16:05:00+00:00",
        "channel": "email",
        "customer_id": "CUST-9032",
        "account_id": "ACC-55902",
        "narrative": (
            "My interest payment for June was 18.44 pounds when your own rate "
            "card says it should have been closer to 24 pounds. I would like an "
            "explanation and the difference paid if I am right."
        ),
    },
]


def find_complaint(complaint_ref: str) -> Dict[str, Any]:
    """Look up one seeded inbound complaint by reference."""
    for complaint in INBOUND_COMPLAINTS:
        if complaint["complaint_ref"] == complaint_ref:
            return dict(complaint)
    raise KeyError(f"Unknown complaint reference: {complaint_ref}")
