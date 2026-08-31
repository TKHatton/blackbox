# Recording Cue Sheet

Keep this open on a second screen while you record narration. Do not chase exact
words, get close and move on.

**S** = static screen, narration carries it, let it breathe.
**A** = something is changing on screen, keep it tight.

---

**0. S | "Right now, companies are handing consequential decisions to AI agents..."**
Screen: title card, or the live page sitting still. Nothing clicked. Longest block.

**1. A | "This is running right now, and what you are watching..."**
Screen: Live fleet, reasoning scrolling. Hover a card, then the model badge.

**2. S | "Here is my favorite detail. One agent is waiting..."**
Screen: the "what the fleet is waiting on" panel. Held still, no clicking.

**3. A | "Same system from the other side..."**
Screen: Cloud Run logs, the Vertex AI line.

**4. S | "If you only watch one part of this, watch this one..."**
Screen: Invisible Ink, Trace clicked, hop one, slow scroll, land on the blocked
letter and hold. The most important shot in the video, move slowly.

**5. A | "Every rule in this system is stored as data..."**
Screen: Split screen tab, threshold set to 100, Replay clicked, both panels land.

**6. A | "Let me break it on purpose while it is running..."**
Screen: terminal, fault command, the response. **Disarm right after this take.**

**7. S | "Gemini writes attacks against this system on its own..."**
Screen: Immune system chart held still, trace red then green, then a glance at the
Eraser tab.

**8. S | "All of this comes from one idea..."**
Screen: back on Live fleet, reasoning moving, held to the end.

---

## The one thing that makes this easier

Record fresh with the audio in your headphones as a pace-setter, video silent. Do
not try to match narration to footage you already shot.

## After Take 6, before anything else

```
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Length: 0" \
  https://blackbox-rd444zycdq-uc.a.run.app/faults/disarm
```
Leaving it armed means the next click on the live demo, yours or a judge's, hits a
fake balance disagreement nobody is expecting.
