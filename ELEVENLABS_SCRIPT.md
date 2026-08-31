# ElevenLabs Voiceover Script

Nine blocks, matching Takes 0-8 in `SCREEN_RECORDING_GUIDE.md`. Generate each block
as its own clip, then line each one up with its scene in CapCut.

**Length:** about 700 words, roughly 4:50 at 145 words per minute. That is
deliberately over the 4:00 target, because you speed clips up in the edit and
because it is easier to cut a sentence than to stretch thin material. Every block
is written so the first two sentences carry the point on their own. If a block runs
long against its footage, cut from the end.

**Pacing note, since this was a problem last time:** each block below is marked
either **STATIC** or **ACTIVE**.

- **STATIC** means nothing much is moving on screen, so the words carry the moment.
  These blocks are longer on purpose. Let them breathe.
- **ACTIVE** means you are clicking, typing, or something is visibly changing.
  These are short on purpose, so you are never waiting in silence for narration to
  catch up.

Do not read these word for word. Get the sense of it, keep your own rhythm.

---

## 0. Opening (STATIC, longest block, nothing clicked yet)

Footage: the live page sitting still, or a title card, before you touch anything.

> Right now, companies are handing consequential decisions to AI agents. Approving refunds.
> Reviewing claims. Screening applicants. And when someone eventually asks why the
> system did what it did, most teams cannot answer. They have logs of what got
> called and what came back, but not the reasoning, no way to go back to the moment
> before the decision, and no way to answer the question a regulator actually asks:
> would it have done the same thing under last month's rules?
>
> BLACKBOX is the missing piece underneath. It is a recording layer that sits under
> any AI agent system and writes down everything the agents do, including why, in a
> record that can never be edited or deleted. It works the same way whether the
> agents are handling medical triage, insurance claims, loan decisions, or hiring:
> anywhere a machine makes a decision somebody may later have to answer for.
>
> To show it, I built the hardest case I could think of: six AI agents handling
> regulated bank complaints, with legal deadlines, health information, and customers
> in three countries with different privacy laws. Over the next few minutes you will
> see this system run itself with nobody pressing anything, catch a data leak in a
> letter that contains no sensitive words at all, rewind a closed case and replay it
> under a different rule, and survive attacks that the AI wrote against itself.

## 1. Live fleet, and why nothing has a start button (ACTIVE, you are hovering)

Footage: Live fleet tab, reasoning already scrolling, hover a card, then the model badge.

> This is running right now, and what you are watching is the actual reasoning from
> Google's Gemini model, appearing as it gets recorded.
>
> Nobody started this. A scheduler wakes the system on a timer, and drops a message
> into a queue called Pub/Sub. A message landing there is what makes an agent run.
> That matters more than it sounds: it means there is no button anywhere, and no
> person in the loop, which is what separates a system that works from a demo that
> needs babysitting.

## 2. Agents that sleep (STATIC, hold on the suspended case)

Footage: the "what the fleet is waiting on" panel.

> Here is my favorite detail. One agent is waiting on a records archive that answers
> in days, not seconds. So instead of sitting there holding memory open, it wrote
> down what would wake it up, and stopped completely. A heartbeat checks later and
> starts it again where it left off. Because that wake-up condition is a recorded
> fact and not something held in memory, a server can restart and no case is lost.

## 3. Proof it is genuinely on Google Cloud (ACTIVE, brief)

Footage: Cloud Run logs, scrolled to the Vertex AI line.

> Same system from the other side. That is a live call going out to Gemini 3.5 Flash
> on Vertex AI, which is Google's enterprise platform for running these models.

## 4. Invisible Ink, the one to remember (STATIC, the star, let it run)

Footage: Invisible Ink tab, Trace clicked, hop one, then the blocked final hop.

> If you only watch one part of this, watch this one.
>
> A customer in Ireland complained about bank fees. In passing, she mentioned a
> cancer diagnosis that had cut her income. Four steps later, a different agent, one
> that never saw her original complaint, wrote her a letter.
>
> Here is that letter. There is no medical word anywhere in it. No diagnosis, no
> illness, no treatment. Every word is ordinary.
>
> The system blocked it anyway. Health information, from a customer in Europe,
> heading to a printing vendor in the United States, with no legal basis on file for
> sending it there.
>
> It caught that because the sensitivity tag travels with the information itself,
> through the AI rewriting it twice in its own words. It is not scanning for
> forbidden words. That is the difference between a filter that looks like it works
> and one that actually does, because rewording something never removes a tag that
> was never attached to the wording.

## 5. The Time Machine (ACTIVE, you click Replay)

Footage: pick the case, set the threshold to 100, click Replay, both panels land.

> Every rule in this system is stored as data, not written into the code. So I can
> take a case that already closed, change one rule, and run it again.
>
> This case was handled under a five hundred dollar approval limit. Watch it at one
> hundred. Left is what happened. Right is what would have happened. The same rule
> now says escalate instead of approve, so that case waits for a human it did not
> wait for before, against a legal deadline. That is a consequence you would want to
> see before shipping a policy change, not after.

## 6. The Crash Test (ACTIVE, you run the command)

Footage: terminal, fault command, the response.

> Let me break it on purpose while it is running. Two of the bank's systems now
> report a different balance for the same account.
>
> The fleet does not retry, and that is the point. Asking a system that disagrees
> with another system a second time just gets you the same answer with more
> confidence. So it stops, records both numbers, and escalates to a person, instead
> of picking one and acting on information the bank already knows is disputed.

## 7. The Immune System, and the two we are skipping (STATIC, chart is still)

Footage: Immune system tab and its chart, then quickly through the Eraser tab.

> Gemini writes attacks against this system on its own. The red line is how often
> those attacks succeed. The green line is the library of attacks that have ever
> worked, which is kept forever and retested against every future version. Red falls
> while green grows, and you need both lines to trust either one.
>
> An attack only counts as successful if a rule was actually broken. Not if the AI
> merely sounded rattled. An agent can quote an attacker back and still do exactly
> the right thing.
>
> Two more I do not have time to show properly. The Eraser: when someone asks to be
> forgotten, deleting the record rebuilds every summary built from it, even three
> steps downstream, and the AI is never shown what it is erasing. And the Stunt
> Double, which tests a new version of an agent against genuine past cases with
> every action faked, then blocks the release if Gemini judges it riskier than what
> is already live.

## 8. Close (STATIC, hold to end)

Footage: back on Live fleet, reasoning still moving.

> All of this comes from one idea. If you record everything an agent does, including
> why, and make that record impossible to change, then you can rewind a decision,
> replay it under different rules, prove where a fact came from, and show that
> private information never reached somewhere it should not have.
>
> Every test here runs with no cloud account and no network, so anyone can check any
> of this in about two minutes.
>
> Agents are already making decisions that people have to answer for. The answers
> are not hard. Nobody was keeping the recording. Now something is.
