# Recording Cue Sheet

Keep this open on a second screen (or printed) while you record narration. Listen
through headphones, don't chase exact words, just get roughly close and move on.

---

**Clip 1: "This is BLACKBOX. It's a fleet of six AI agents..."**
Screen: Live fleet tab, reasoning already scrolling, then a hover on one card,
then the `gemini-3.5-flash` badge.

**Clip 2: "Here's the same thing from the other side..."**
Screen: Cloud Run Logs tab, scrolled to the Vertex AI request line.

**Clip 3: "This is the piece I'd watch if you only watch one..."**
Screen: Invisible Ink tab, Trace clicked on the EU case, hop 1 then the final
blocked hop.

**Clip 4: "Every governance rule in this system is data, not code..."**
Screen: Split screen tab, case picked, threshold set to 100, Replay clicked,
both panels landing.

**Clip 5: "Let me break something while it's running..."**
Screen: terminal, the fault-arm command run, the JSON response on screen.

**Clip 6: "Gemini writes attacks against this fleet..."**
Screen: Immune system tab, the two trend lines.

**Clip 7: "Two hundred and seventy-eight tests..." through "...running right now."**
Screen: back on Live fleet, reasoning still moving, held to close.

---

## If you only do one thing to make this easier

Don't try to match narration to footage you already recorded. Record fresh, audio
in your headphones as a pace-setter, video silent. That's the whole fix.

## After Take 5 in the recording session

Disarm the fault before you do anything else, live or not:
```
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Length: 0" \
  https://blackbox-rd444zycdq-uc.a.run.app/faults/disarm
```
Leaving it armed means the next click on the live demo, yours or a judge's, hits a
fake balance disagreement nobody's expecting.
