# Screen Recording Guide (silent capture, narrate later in ElevenLabs/CapCut)

No talking during capture. Hit these in order, hold where noted, cut the rest in
CapCut. Record silent video first, generate narration separately from
`ELEVENLABS_SCRIPT.md`, then line the two up.

**Record against the live service:** https://blackbox-rd444zycdq-uc.a.run.app

**Takes 0-8 here match narration blocks 0-8 exactly.** Block 0 has no footage of its
own, so start capture at Take 1 and let block 0 play over a title card or a still
shot of the page.

## The pacing rule that fixes last time's problem

Each take below is marked **STATIC** or **ACTIVE**.

- **STATIC** takes have long narration over a still screen. Record more footage than
  you think you need, 10-15 seconds, and hold. You can always trim.
- **ACTIVE** takes have short narration over something visibly changing. Keep these
  tight. Do not add extra clicking to fill time; the narration is already short so
  you are not left waiting.

Nothing here needs you to stall while words catch up. If a hold feels long while
recording silently, that is correct, the narration fills it.

## Before you hit record

1. Open the URL in a clean browser window. Close every other tab. If it returns 403,
   a redeploy reset the public grant; run:
   `gcloud run services add-iam-policy-binding blackbox --region us-central1 --member=allUsers --role=roles/run.invoker`
2. Zoom to 100 percent (Ctrl+0). Window at 1920x1080 or 1280x720.
3. Confirm the top bar reads `MODEL: gemini-3.5-flash`. That badge is your on-screen
   proof of the required model.
4. Fire the poller a few minutes ahead so cases and reasoning already exist:
   ```
   gcloud scheduler jobs run blackbox-intake-poller --location us-central1 --project=blackblack-agentic
   ```
5. Open the Cloud Run console in a second tab, on the `blackbox` service's **Logs**
   tab, for Take 3.
6. Open a terminal for Take 6 and get a token ready, but do not run the fault yet:
   ```
   $env:TOKEN=$(gcloud auth print-identity-token)
   ```

---

## Take 1: Live fleet (ACTIVE, ~10s)

1. Land on the **Live fleet** tab, reasoning already scrolling.
2. Hover one reasoning card for 2 seconds. Do not click.
3. Move the cursor to the `gemini-3.5-flash` badge in the top bar. Hold 2 seconds.

## Take 2: The suspended case (STATIC, ~12s)

1. Stay on Live fleet. Scroll to the **"what the fleet is waiting on"** panel on the
   right, showing `evidence_agent` waiting on the archived call.
2. Hold on it, still, for the full take. Nothing to click. This is a long narration
   block over a static panel, so give it room.

## Take 3: Cloud Run logs (ACTIVE, ~7s)

1. Switch to the Cloud Run Logs tab.
2. Scroll until a line reading `model: gemini-3.5-flash, backend: VERTEX_AI` is
   visible. Hold 2 seconds on it.
3. Switch back to the Split Screen tab.

## Take 4: Invisible Ink (STATIC, ~25s, the most important take)

This carries the longest narration block. Move slowly and deliberately.

1. Click the **Invisible Ink** tab.
2. Click **Trace** on the EU case (`CASE-CMP-2026-0841`).
3. Hold 3-4 seconds on hop 1, where the customer's complaint text with the cancer
   mention is visible.
4. Scroll slowly down through the hops. Do not rush this, the narration is walking
   through it with you.
5. Land on the final blocked hop and hold 4-5 seconds. The letter text should be
   readable, so a viewer can confirm for themselves there is no medical word in it.
6. Trace the cursor down the label column showing the tag carried across hops. Hold
   2 seconds on the blocked state.

## Take 5: The Time Machine (ACTIVE, ~10s)

1. Click the **Split screen** tab.
2. Pick `CASE-CMP-2026-0841` from the dropdown.
3. Set **Gate A threshold** to `100`.
4. Click **Replay**. It returns in about half a second, so there is no dead air.
5. Hold 3-4 seconds on the two side-by-side panels once they render.

## Take 6: The Crash Test (ACTIVE, ~10s)

1. Front the terminal.
2. Run:
   ```
   curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" `
     -d '{"fault_type":"contradiction","system":"corebank","method":"get_account","detail":{"field":"balance","value_a":-412.55,"value_b":-37.00}}' `
     https://blackbox-rd444zycdq-uc.a.run.app/faults/arm
   ```
3. Response lands in under a second. Hold 2-3 seconds on the JSON showing `"armed"`.
4. **Disarm immediately after this take**, before you forget:
   ```
   curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Length: 0" `
     https://blackbox-rd444zycdq-uc.a.run.app/faults/disarm
   ```

## Take 7: Immune System, then a glance at the Eraser (STATIC, ~18s)

1. Click the **Immune system** tab. Hold 6-8 seconds on the chart, still. It
   currently shows a flat line at zero across three campaign runs (v1, v2, v3),
   nine attacks total, none successful. Point at the flat line, not a moving
   one, the narration explains why that is the point.
2. Click the **The Eraser** tab. Hold 4-5 seconds. No interaction needed, the
   narration is naming it in passing, not demonstrating it.

## Take 8: Close (STATIC, ~12s)

1. Return to the **Live fleet** tab with reasoning still moving.
2. Hold. Let it run to the end of the narration.

---

## Recording order and rough budget

| Take | What | Mode | Footage |
|---|---|---|---|
| (0) | title card or still page | STATIC | 20-25s |
| 1 | Live fleet, model badge | ACTIVE | 10s |
| 2 | the suspended case | STATIC | 12s |
| 3 | Cloud Run logs | ACTIVE | 7s |
| 4 | Invisible Ink trace | STATIC | 25s |
| 5 | Time Machine replay | ACTIVE | 10s |
| 6 | Crash Test fault | ACTIVE | 10s |
| 7 | Immune System, Eraser | STATIC | 18s |
| 8 | close on Live fleet | STATIC | 12s |

Roughly **2:05 of raw footage** against about 4:50 of narration. That gap is
intentional and is what you fix in the edit: hold the STATIC shots longer, or let a
still frame sit while the words finish. It is far easier to stretch a held shot than
to find footage you never captured, so if in doubt, record longer on every STATIC
take.

## Before you call it done

Confirm no fault is left armed on the live service:
```
curl -H "Authorization: Bearer $TOKEN" https://blackbox-rd444zycdq-uc.a.run.app/faults
```
It should print `{"armed":[]}`.
