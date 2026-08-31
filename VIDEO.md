# The four-minute demo

This is the master reference: every line of narration and every on-screen action,
in order. It describes shooting in one live take with the browser and the Cloud
Console visible, narrating as you go.

**If you're recording silent footage first and adding ElevenLabs narration after**
(the OnboardFlow workflow), use these three documents instead, all derived from
this one:

1. [SCREEN_RECORDING_GUIDE.md](SCREEN_RECORDING_GUIDE.md): silent capture, no
   talking, exact clicks and holds, Takes 1-7
2. [ELEVENLABS_SCRIPT.md](ELEVENLABS_SCRIPT.md): narration only, same 7 blocks,
   matched to the takes above
3. [RECORDING_CUE_SHEET.md](RECORDING_CUE_SHEET.md): a condensed sheet to keep
   open while recording narration, so you're not flipping between the two full
   documents

The point either way is that a reviewer can see the system is running rather than
being told.

## Before you start recording

Have these open in tabs, in this order, so you are never hunting mid-take:

1. The Split Screen: `https://YOUR-SERVICE-URL/`
2. Cloud Run console, the `blackbox` service, on the **Logs** tab
3. Cloud Trace, filtered to the service
4. A terminal with `TOKEN=$(gcloud auth print-identity-token)` already run

Fire the poller a few minutes before you record so cases exist and reasoning is
already on screen. A cold start on camera wastes twenty seconds of your four
minutes.

Say the numbers out loud as you go. "Two hundred and seventy-eight tests" lands
harder than pointing at them.

---

## 0:00 to 0:25. What this is, over the live page

**On screen:** the Split Screen, Live fleet view, reasoning scrolling.

> This is BLACKBOX. It is a fleet of six AI agents handling regulated bank
> complaints, sitting on a recording layer that cannot be edited.
>
> Everything on this page is live, from a Cloud Run service. What you are
> watching on the left is Gemini's reasoning, arriving as it is recorded. Nobody
> pressed a button to start any of this. A Cloud Scheduler job wakes a poller,
> the poller publishes to Pub/Sub, and a message landing on that topic is what
> makes an agent run.

**Do:** point at one reasoning card. Read half a sentence of it aloud, whatever is
on screen. Unscripted model output is the most convincing thing in the video.

---

## 0:25 to 0:50. It is genuinely on Google Cloud

**On screen:** switch to the Cloud Run Logs tab.

> Here is the same thing from the other side. Cloud Run, the live service, and
> these are Vertex AI calls going out to Gemini as the fleet works.

**Do:** scroll so `Sending out request, model: gemini-3.5-flash, backend:
VERTEX_AI` is visible. Sit on it for two seconds.

> Firestore holds the log. BigQuery holds anything older than a week. The
> tiering job moves it there every night, and only deletes from Firestore after
> reading the event back to check it arrived intact.

**Do:** switch back to the Split Screen.

---

## 0:50 to 1:40. Invisible Ink, the moment worth remembering

**On screen:** the Invisible Ink tab. Click Trace on the EU case.

> This is the piece I would watch if you only watch one.
>
> A customer in Ireland complained about bank fees. In passing, she mentioned a
> cancer diagnosis that had cut her income.

**Do:** point at the quoted sentence in hop 1.

> Four hops later, a different agent, which never saw the complaint, wrote her a
> letter. Here it is. There is no medical word in it. No diagnosis, no illness,
> no treatment. Every word is ordinary.
>
> The gateway blocked it anyway.

**Do:** point at the final hop, the red one.

> Special category data, EU origin, going to a US-based print vendor, with no
> transfer basis recorded. The system knows because the label travelled with the
> derivation, through two Gemini calls and a paraphrase, not because it matched a
> word. A keyword filter sees an apology about fees and lets it through.

**Do:** trace your finger down the label column, showing it accumulate.

---

## 1:40 to 2:30. The Time Machine, side by side

**On screen:** the Split screen tab. Pick a case, set the threshold to 100, Replay.

> Every governance rule in this system is data, not code. So I can rewind a case
> that already finished, change one rule, and replay it.
>
> This case was worked under a five hundred dollar approval threshold. A three
> hundred dollar remedy went straight through. Watch what happens at one hundred.

**Do:** click Replay. Let it land.

> Left is what happened. Right is what would have happened. The same rule reached
> a different verdict: allow became escalate. That case now waits one to four days
> for an adjudicator that it did not wait for before, against an eight week
> statutory deadline.
>
> That is a policy consequence a bank would want to see before shipping the rule.

**Do:** say this plainly, because it is the part people miss:

> The replay cannot touch anything. It runs against recorded tool responses, and
> if a recording is missing it stops rather than calling the live system. A replay
> that could reach production would be the worst defect in this build.

---

## 2:30 to 3:10. Break it on camera

**On screen:** the terminal.

> Let me break something while it is running.

**Do:** run this, and let the response show:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"fault_type":"contradiction","system":"corebank","method":"get_account",
       "detail":{"field":"balance","value_a":-412.55,"value_b":-37.00}}' \
  https://YOUR-SERVICE-URL/faults/arm
```

> CoreBank and CRM360 now disagree about the same balance. The fault comes back
> as something the agent reads, not as something the infrastructure swallows.
>
> The important part: the fleet does not retry. Asking either system again
> returns the same answer, more confidently. A contradiction is not a slow
> answer. The agent stops, records both figures, and escalates to a person.

**Do:** show `/cases/{id}/degradation` returning `escalated`, or narrate from the
crash test demo if no live case is mid-flight.

> Four outcomes are possible: recovered, escalated, halted safely, or proceeded on
> bad data. Only the last one is a failure, and it is the one that looks fine in a
> log. That is what the scoring is for.

---

## 3:10 to 3:40. The immune system

**On screen:** the Immune system tab.

> Gemini writes attacks against this fleet. Prompt injection in the complaint
> text, poisoned call transcripts, pressure across the appeal window.
>
> Red line is the attack success rate. Green is the corpus of attacks that have
> ever worked. The rate falls while the corpus grows, and both curves have to be
> read together: a falling rate against a fixed set of attacks would just mean
> somebody patched those attacks.
>
> An attack counts as a success only when a policy boundary was actually crossed.
> Money moved without approval, something reaching a customer the gateway
> refused. Not when the model merely sounded rattled. An agent that quotes an
> injection back and then does exactly the right thing has not been compromised.

---

## 3:40 to 4:00. Close

**On screen:** back to the Live fleet view, reasoning still moving.

> Two hundred and seventy-eight tests, no credentials needed to run them.
> Eleven phases: the recorder, the fleet, Invisible Ink, the Eraser, the Time
> Machine, the Stunt Double, the immune system, the crash test, and this page.
>
> The thing underneath all of it is one idea. If you record everything an agent
> does, including why, you can do things no agent system can currently do. Rewind
> a decision. Replay it under different rules. Prove where a fact came from. Show
> a regulator that data never reached where it should not.
>
> It is all in the repo, and it is running right now.

---

## If something breaks on camera

Keep going. An unedited video where a call takes four seconds is more convincing
than a cut one. If a request fails, say what you think happened and move to the
next tab. The one thing not to do is stop and restart the take, because the value
of shooting live is that it is visibly live.

## What not to claim

Say "Gemini 3.5 Flash". It runs on Vertex AI's global endpoint rather than a
region, which is documented in the README. Do not say a region name next to
the model, since that part of the setup is deliberately not region-pinned.

Do not say the tiering has been running for months. It works, it is scheduled
daily, and it has been verified end to end against BigQuery and Cloud Storage.
That is enough and it is true.
