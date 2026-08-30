# WORKFLOW.md
## BLACKBOX Phase 0: the work the fleet performs

**Domain:** Regulated consumer complaint handling and remediation at a mid-size retail bank operating in the US, UK, and EU.

**Why this domain:** complaint handling runs on statutory clocks, which gives agents an unarguable reason to wake up and act without being asked. It is saturated with sensitive data including health disclosures. It crosses three departments and three jurisdictions. It requires human sign-off at defined thresholds. And the customer-facing output is a letter, which creates an outbound path where a data leak would carry consequences. Every BLACKBOX feature has work to do here.

**Note on the regulatory detail below:** the deadlines are simplified approximations of statutory complaint-handling rules, chosen because they create useful timing pressure in the system. Treat them as configuration for a demonstration, not as a compliance reference.

---

## The 60-second version

A complaint arrives. An agent reads it, classifies it, works out which country's rules apply, and starts the clock. A second agent gathers evidence from three internal systems, one of which takes days to answer. A third agent decides whether the bank was at fault and proposes a remedy. Anything over a threshold goes to a human for approval, which takes days. Once approved, a fourth agent executes the remedy and a fifth drafts the customer letter. A sixth agent watches every statutory deadline across every open case and intervenes on its own when one is at risk. After the final letter, the case sleeps for a 30-day appeal window, waking itself only if the customer replies.

---

## The agents

Each has one job. None of them is a supervisor holding a hard-coded sequence; routing is by judgment and recorded as a decision.

### 1. Intake Agent
Reads the raw complaint in whatever form it arrived. Extracts structured facts. Classifies category (billing dispute, service failure, mis-sold product, fraud, data handling) and severity. Determines which jurisdiction's rules apply from the customer's country of residence and account domicile. Opens the case and starts the statutory clock. Decides whether the customer shows vulnerability indicators, which changes handling requirements downstream.

### 2. Evidence Agent
Gathers the record from three internal systems. Decides which evidence is actually needed rather than requesting everything. Handles the fact that one system answers in seconds and another takes days. Self-schedules its own re-checks on pending batch requests.

### 3. Assessment Agent
The judgment step. Decides upheld, partially upheld, or not upheld, and proposes a remedy with reasoning. Decides on its own whether the case looks like part of a systemic pattern rather than an isolated incident, which triggers a different approval path.

### 4. Remediation Agent
Executes the approved remedy against the core banking system: refunds, fee reversals, interest adjustments, account corrections. The only agent with write access to money.

### 5. Correspondence Agent
Drafts and sends everything the customer sees: acknowledgment, holding letter, final response, appeal outcome. This is the primary outbound path and therefore the main stage for Invisible Ink.

### 6. Compliance Officer Agent
Watches every statutory clock across every open case. Decides when a holding letter is required, when to escalate to a human, and when a case must be reported to a regulator. This is the agent that most visibly acts without being asked.

---

## The step sequence

| Step | Agent | Typical timing | Notes |
|---|---|---|---|
| 1. Complaint received | Intake | Day 0 | Arrives on a Pub/Sub topic from a simulated email and web-form poller. Nobody presses a button. |
| 2. Classify, set jurisdiction, start clock | Intake | Day 0 | Writes the case, opens its Wiki page |
| 3. Acknowledgment sent | Correspondence | Within 3 business days | Statutory |
| 4. Evidence requested | Evidence | Day 0 to 1 | Two systems answer fast, one returns a job id |
| 5. **Wait on batch evidence** | Evidence | **2 to 3 days** | Agent suspends, self-schedules re-checks |
| 6. Assessment and proposed remedy | Assessment | Day 4 to 6 | Judgment step, full reasoning recorded |
| 7. **Human approval gate A** | Assessment → human | **1 to 4 days** | Required for any remedy over $500 |
| 8. **Human approval gate B** | Assessment → human | **2 to 5 days** | Required only if flagged as a possible systemic issue |
| 9. Holding letter | Correspondence | At 4 weeks if unresolved | Compliance Officer decides this on its own |
| 10. Remedy executed | Remediation | On approval | Writes to core banking |
| 11. Final response letter | Correspondence | By 8 weeks | The main Invisible Ink checkpoint |
| 12. **Appeal window** | Case sleeps | **30 days** | Wakes only if the customer replies |
| 13. Closure or reopening | Compliance Officer | Day 38+ | Agent decides which |

**Four separate waits**, each of a different kind: a batch job, two human gates of differing length, and a long sleep with a conditional wake. That range is deliberate. It exercises suspend and resume more thoroughly than four copies of the same pause would.

---

## Human approval gates

Only two, both meaningful:

- **Gate A, monetary threshold.** Remedies above $500 need an adjudicator's sign-off. Fires on most cases.
- **Gate B, systemic flag.** If the Assessment Agent concludes a complaint may indicate a pattern affecting other customers, Compliance must sign off before any customer-facing statement is made. Fires rarely, and matters when it does.

Approvals arrive by Pub/Sub. The agent that suspended does not stay resident waiting; it rebuilds context from its Wiki page on resume.

---

## The three source systems (build as stubs)

Do not integrate anything external. These are stub services with deliberate personality.

### CoreBank
Accounts, transactions, fees, balances. Responds in under a second. Occasionally disagrees with CRM360 about a balance, which is the Crash Test contradiction case.

### CRM360
Customer profile, contact history, communication preferences, vulnerability flags, prior complaints. Fast. Source of the special-category data.

### CommsVault
Archived email, call recordings, and call transcripts. **Returns a job id, not results.** Results become available 2 to 3 days later. This is the slow system on purpose, and it is what makes Step 5 a true asynchronous wait rather than a simulated one.

### Two outbound stubs

- **PrintPost**, a letter fulfillment vendor with US-based operations. This is the destination that makes cross-border transfer a live constraint.
- **RegPortal**, the regulator filing endpoint.

---

## Sensitive data: fields, classes, jurisdictions

| Field | Sensitivity class | Origin | Notes |
|---|---|---|---|
| Name, address, DOB | PII | CRM360 | |
| National identifier (SSN / NI number) | PII_HIGH | CRM360 | Never leaves the system |
| Account and transaction records | FINANCIAL | CoreBank | |
| Complaint narrative | MIXED | Intake | May contain anything, including the next row |
| Health or hardship disclosure | SPECIAL_CATEGORY | Intake or CommsVault | Highest class. Common in complaints about payment difficulty. |
| Vulnerability flag | SPECIAL_CATEGORY | CRM360 | |
| Third-party names in transaction records | THIRD_PARTY_PII | CoreBank | The bank has no right to disclose these to the complainant |
| Internal assessment reasoning | INTERNAL_ONLY | Assessment Agent | Must never reach the customer |

**Jurisdictions in play:** `US`, `US_CA` (stricter state rules), `UK`, `EU_IE`, `EU_DE`. Cases mix them: an EU-resident customer with a UK-domiciled account is normal and forces the jurisdiction question to be decided rather than assumed.

---

## The standout Invisible Ink moment

Worth building the workflow around, because it is the moment that gets remembered.

An EU-resident customer's complaint narrative mentions a cancer diagnosis affecting their ability to make payments. That is `SPECIAL_CATEGORY`, jurisdiction `EU_IE`.

Four hops later:

1. Intake extracts it into structured facts. Label attached.
2. Evidence Agent correlates it with a CommsVault transcript. Label propagates, combined with the transcript's own labels.
3. Assessment Agent writes reasoning that references the hardship in deciding to uphold the complaint. Label survives the summarization.
4. Correspondence Agent drafts a final response letter that paraphrases it empathetically: something along the lines of acknowledging a difficult personal period. No medical word appears in the letter.

The letter is addressed to PrintPost, US-based.

**The gateway blocks it.** Special category data, EU origin, third-country transfer, no adequacy basis recorded.

The point to make out loud: no keyword filter catches this. The letter contains no medical vocabulary. The system knows only because the label propagated through four transformations including two Gemini summarizations. Then you display the four-hop trail back to the original sentence in the complaint.

**Second, shorter version:** the Correspondence Agent tries to cite a disputed transaction as evidence, and the record names another customer. Blocked as `THIRD_PARTY_PII`. Fast to demonstrate and easy to understand.

---

## What each later phase gets to use

**Phase 3, autonomy.** Complaints arrive unprompted. The Compliance Officer wakes on a heartbeat, scans clocks, and decides to issue holding letters. The Evidence Agent self-schedules CommsVault re-checks. A closed case wakes itself when a customer replies during the appeal window.

**Phase 5, The Eraser.** After closure, the customer invokes erasure. Retract the identity fields and watch the cascade: the case Wiki page, the customer Wiki page, the systemic-pattern analysis that drew partly on this case, and the Assessment Agent's operating-context page all invalidate and regenerate without the retracted content. The Diary still records that a retraction happened. Region pinning shows the EU case refusing to be routed to a US-region worker.

**Phase 6, The Time Machine.** Rewind to Step 6 on an upheld case and tighten the approval threshold from $500 to $100. Replay. Gate A now fires on cases it previously skipped, the timeline shifts, and one case that met its 8-week deadline now misses it. That is a policy consequence a bank would pay to see before shipping the rule.

**Phase 7, The Stunt Double.** A candidate Assessment Agent runs shadow against live cases. The report reads: it would have upheld two complaints the current version rejected, and flagged one as systemic that the current version did not.

**Phase 8, The Immune System.** The complaint narrative is untrusted text the agent must read closely, which makes it an ideal injection surface. Attacks to seed: instructions embedded in the narrative claiming pre-approval for a large refund; a poisoned CommsVault transcript; slow manipulation across a 30-day appeal thread; an attempt to have the final letter disclose internal assessment reasoning.

**Phase 9, The Crash Test.** CoreBank and CRM360 disagree on a balance. CommsVault times out permanently. Gemini declines to assess a complaint containing abusive content. A case is interrupted between remedy execution and letter dispatch, which is the worst possible moment and therefore the one to test.

---

## Open decisions for you

Four things I picked defaults for. Change any of them before Codex starts.

1. **Volume.** I would seed 40 synthetic cases across the jurisdiction mix, at staggered ages so some are already mid-flight on day one and the Compliance Officer has something to do immediately. Adjust if you want a heavier load for the observability views.
2. **Retail bank versus insurer.** Insurance claims give richer health data and a stronger Invisible Ink story. Banking is faster to make legible to a general audience. I chose banking for the audience reason.
3. **Gate A threshold.** $500 makes the gate fire often, which is good for demonstrating the wait but adds latency to every path. $2,000 makes it rarer and more interesting when it fires.
4. **Appeal window length.** 30 days is realistic. If you want the conditional wake to be demonstrable inside a shorter recorded window, make the window configurable and run the demo at compressed time, with the compression visible so nobody thinks it is a shortcut.
