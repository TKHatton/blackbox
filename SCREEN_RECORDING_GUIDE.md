# Screen Recording Guide (silent capture, narrate later in ElevenLabs/CapCut)

No talking needed during capture. Just hit these in order, hold where noted, cut the
rest in CapCut. Record silent video first, generate narration separately from
`ELEVENLABS_SCRIPT.md`, then line the two up in your editor. That's the same process
used for OnboardFlow.

**Record against the live app, not a local dev server:**
- App and API (same URL, same service): https://blackbox-rd444zycdq-uc.a.run.app

## Before you hit record

1. Open https://blackbox-rd444zycdq-uc.a.run.app/ in a clean browser window. Close
   every other tab. If it 403s, the public grant was reset by a redeploy; run:
   `gcloud run services add-iam-policy-binding blackbox --region us-central1 --member=allUsers --role=roles/run.invoker`
2. Resize the browser window to something clean, 1920x1080 or 1280x720. Zoom to 100
   percent (Ctrl+0).
3. Confirm the top bar shows `MODEL: gemini-3.5-flash` before you start. That badge
   is your on-screen proof of the required model, worth lingering on for Take 1.
4. Fire the poller a few minutes before you record so cases and reasoning already
   exist. A cold start on camera wastes seconds you don't have:
   ```
   gcloud scheduler jobs run blackbox-intake-poller --location us-central1 --project=blackblack-agentic
   ```
5. Have a terminal window ready off to the side for Take 5 (breaking something live),
   open it now so you're not fumbling for it mid-recording. Get an identity token
   ready in it but don't run the fault command yet:
   ```
   $env:TOKEN=$(gcloud auth print-identity-token)
   ```
6. Have the Cloud Run console open in a second tab, on the `blackbox` service's
   **Logs** tab, filtered to nothing (just the live tail), for Take 2.

## Take 1: Live fleet, the opening hook

Land on the page with the **Live fleet** tab already selected (it's the default).

1. Let the reasoning panel sit on screen for 2-3 seconds before doing anything else.
   Live Gemini text should already be scrolling in from the poller you fired earlier.
2. Point at (hover, don't click) one reasoning card. Hold 2 seconds.
3. Point at the top bar's `MODEL: gemini-3.5-flash` badge. Hold 1-2 seconds.

Total: **about 8-10 seconds.**

## Take 2: Cloud Run logs, the "genuinely on Google Cloud" proof

Switch to the Cloud Run Logs tab you opened earlier.

1. Scroll until a line reading `Sending out request, model: gemini-3.5-flash,
   backend: VERTEX_AI` is visible.
2. Hold 2 seconds on that line.
3. Switch back to the Split Screen tab.

Total: **about 5-8 seconds.**

## Take 3: Invisible Ink, the trace

Click the **Invisible Ink** tab.

1. Click **Trace** on the EU case (`CASE-CMP-2026-0841`).
2. Hold 2 seconds on hop 1, where the customer's complaint text is visible (the
   cancer mention).
3. Scroll or click down to the final hop, the blocked letter. Hold 2-3 seconds. It
   should visibly contain no medical word.
4. Trace down the label column showing it accumulate across hops. Hold 2 seconds
   on the final, red, blocked state.

Total: **about 12-15 seconds.**

## Take 4: The Time Machine replay

Click the **Split screen** tab.

1. Pick `CASE-CMP-2026-0841` from the case dropdown.
2. Set **Gate A threshold** to `100`.
3. Click **Replay**.
4. The response lands in under a second (measured live: ~0.5s), so don't worry
   about dead air here. Hold 2-3 seconds on the two side-by-side results once they
   render, left ("what happened") vs right ("what would have happened").

Total: **about 8-10 seconds.**

## Take 5: Break it on camera (don't skip this one)

Front the terminal window.

1. Run:
   ```
   curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" `
     -d '{"fault_type":"contradiction","system":"corebank","method":"get_account","detail":{"field":"balance","value_a":-412.55,"value_b":-37.00}}' `
     https://blackbox-rd444zycdq-uc.a.run.app/faults/arm
   ```
2. The response returns in under a second (measured live: ~0.4s). Hold 1-2 seconds
   on the JSON response showing `"armed": ...`.
3. **Immediately after recording this take, disarm it** so the fault doesn't sit
   live on the deployed service after you're done:
   ```
   curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Length: 0" `
     https://blackbox-rd444zycdq-uc.a.run.app/faults/disarm
   ```

Total: **about 5-8 seconds**, plus the disarm step after you stop recording (not
part of the footage).

## Take 6: The immune system chart

Click the **Immune system** tab.

1. Let the chart sit on screen for 3-4 seconds. Point at the falling red line (attack
   success rate) and the growing green line (corpus size) separately.

Total: **about 6-8 seconds.**

## Take 7: Close

Switch back to the **Live fleet** tab, reasoning still moving.

1. Hold on the live reasoning panel for 3-4 seconds to close.

Total: **about 4-5 seconds.**

## Recording order

1. Take 1 (8-10s): Live fleet opening, the model badge
2. Take 2 (5-8s): Cloud Run logs, proof it's really on Google Cloud
3. Take 3 (12-15s): Invisible Ink trace, the blocked letter
4. Take 4 (8-10s): Time Machine replay, side by side
5. Take 5 (5-8s): break it live, the fault response, then disarm immediately after
6. Take 6 (6-8s): immune system chart
7. Take 7 (4-5s): close on the live reasoning panel

That's roughly 50-65 seconds of raw footage. Short on its own, which is fine: this
is a feature-dense build, so the narration in `ELEVENLABS_SCRIPT.md` is written to
run longer than the raw clip length in several places (Takes 3 and 5 especially,
where the *idea* being explained takes longer to say than the click itself takes to
show). Hold each shot for the narration to land, not just for the click to register,
and pad holds live rather than trying to stretch footage in the edit.
