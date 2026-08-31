# ElevenLabs Voiceover Script

Matches the footage from `SCREEN_RECORDING_GUIDE.md`, Takes 1-7, in the order you
recorded them. Generate each numbered block as its own clip in ElevenLabs, then drag
each audio clip onto CapCut lined up with the matching scene.

**Word count check:** roughly 620 words of narration total, which comes out to
about 4:15 of audio at a normal pace (145 words/minute), against a 4-minute target.
Trim sentences rather than whole blocks if a section runs long. Each block is
written so its first sentence or two carries the point even if you drop the rest.

---

## 1. Take 1, Live fleet opening

Footage: the reasoning panel already scrolling, then a hover on one card, then the
`gemini-3.5-flash` model badge in the top bar.

> This is BLACKBOX. It's a fleet of six AI agents handling regulated bank
> complaints, sitting on top of a recording layer that cannot be edited. Nobody
> pressed a button to start any of this: a Cloud Scheduler job wakes a poller, the
> poller publishes to Pub/Sub, and a message landing on that topic is what makes an
> agent run. What you're watching here is Gemini's reasoning, arriving as it's
> recorded, running on Gemini 3.5 Flash, right there in the corner.

## 2. Take 2, Cloud Run logs

Footage: the Cloud Run Logs tab, scrolled to the Vertex AI request line.

> Here's the same thing from the other side. This is the live Cloud Run service,
> and that line is an actual Vertex AI call going out to Gemini as the fleet works.
> Firestore holds the log. BigQuery holds anything older than a week, moved there
> every night by a tiering job that only deletes from Firestore after reading the
> event back to confirm it arrived intact.

## 3. Take 3, Invisible Ink trace

Footage: the Invisible Ink tab, hop 1 with the complaint text, then the final
blocked hop.

> This is the piece I'd watch if you only watch one. A customer in Ireland
> complained about bank fees. In passing, she mentioned a cancer diagnosis that had
> cut her income. Four hops later, a different agent, one that never saw the
> complaint, wrote her a letter. Here it is. There's no medical word in it. No
> diagnosis, no illness, no treatment, every word is ordinary. The gateway blocked
> it anyway: special category data, EU origin, going to a US-based print vendor,
> with no transfer basis recorded. The system knows because the label travelled
> with the derivation, through two Gemini calls and a paraphrase, not because it
> matched a word. A keyword filter would have let this straight through.

## 4. Take 4, Time Machine replay

Footage: the case picker, the threshold set to 100, Replay clicked, both panels
landing.

> Every governance rule in this system is data, not code, so I can rewind a case
> that already finished, change one rule, and replay it. This case was worked
> under a five hundred dollar approval threshold, and a three hundred dollar
> remedy went straight through. Watch what happens at one hundred. Left is what
> happened. Right is what would have happened instead. The same rule reached a
> different verdict: allow became escalate. That case now waits one to four days
> for a human adjudicator, against an eight week statutory deadline. And the
> replay itself can't touch anything live: it runs against recorded tool
> responses only, and a missing recording stops it rather than falling through to
> a live call.

## 5. Take 5, break it on camera

Footage: the terminal, the fault-arm command, the JSON response.

> Let me break something while it's running. CoreBank and CRM360 now disagree
> about the same balance. That fault comes back as something the agent reads,
> not something the infrastructure quietly swallows. The important part: the
> fleet does not retry. Asking either system again returns the same answer, more
> confidently, so a contradiction can't be retried away. The agent stops, records
> both figures, and escalates to a person instead of guessing at which figure is
> correct. Four outcomes are possible here: recovered, escalated, halted safely,
> or proceeded on bad data.
> Only the last one is a failure, and it's the one that looks fine in a log.

## 6. Take 6, the immune system

Footage: the Immune system tab, the two trend lines.

> Gemini writes attacks against this fleet: prompt injection in the complaint
> text, poisoned call transcripts, pressure across the appeal window. Red is the
> attack success rate. Green is the corpus of attacks that have ever worked. The
> rate falls while the corpus grows, and an attack only counts as a success when
> a policy boundary was actually crossed, money moved without approval, something
> reaching a customer the gateway refused, never just because the model sounded
> rattled.

## 7. Take 7, close

Footage: back on the Live fleet view, reasoning still moving.

> Two hundred and seventy-eight tests, no credentials needed to run them. The
> recorder, the fleet, Invisible Ink, the Eraser, the Time Machine, the Stunt
> Double, the immune system, the crash test, and this page. One idea underneath
> all of it: if you record everything an agent does, including why, you can
> rewind a decision, replay it under different rules, and prove where a fact
> came from. It's all in the repo, and it's running right now.
