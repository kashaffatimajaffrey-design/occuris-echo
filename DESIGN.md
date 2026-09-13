# Occuris Echo — interface spec

One static HTML file + vanilla JS, talking to a local FastAPI server. No build step, no framework.
Built 1:00–2:00 AM after the agent works. The text box alone is a complete fallback if time runs out.

---

## Who this is for, and what each condition demands

| Condition | What breaks a normal UI | Rule here |
|---|---|---|
| **Tremor / limited fine motor** | small targets, drag, double-click, hover menus | targets ≥ 64px with ≥ 16px gaps; single click only; no drag, no hover-dependent anything; **no destructive control adjacent to a common one** |
| **Low vision** | low contrast, tiny text, colour-only meaning | 18px base, 1.6 line-height; contrast ≥ 7:1 body, ≥ 4.5:1 large; **every state carries an icon or word, never colour alone** |
| **Blind / screen reader** | silent state changes, focus traps | one `aria-live="polite"` region for the echo; `aria-live="assertive"` only for BLOCKED; every control labelled; logical tab order; visible focus ring 3px |
| **Neurodivergent (ADHD, autism, dyslexia)** | animation, noise, walls of text, ambiguity about what happened | **no animation beyond a 120ms fade**; one idea per line; plain words; the current state always visible without scrolling; no surprise changes |
| **Fatigue / pain** | repetition, re-entering lost work | **state never resets** — no timeouts, no auto-clear, partial input always retained |

## Settle, don't snap — the Verlet idea, applied to state

Borrowed from a bead-curtain simulation: in Verlet integration there is **no velocity variable** — velocity is implied by `current − previous`. Constraints are not solved, they are **relaxed** a little per pass. Nothing is ever pulled back to an origin, because no origin is stored. The curtain settles wherever momentum and gravity leave it.

Four transfers:

**1. Pending state is derived, never stored.** No `pending` flag that can desync. Pending = requested actions minus completed actions, read from the ledger.
```python
def pending(run_id):
    return [a for a in ledger.requested(run_id)
            if a.idempotency_key not in ledger.completed_keys(run_id)]
```
A crash mid-run loses nothing. The UI cannot disagree with reality. Fixture B14 (user never confirms) falls out for free.

**2. Clarification is a relaxation pass, not a re-solve.** Answering "which Patel?" fills one slot and re-checks constraints; it never re-parses the utterance. Date, message and channel keep what they already had. One question per pass — that is why the agent never asks two things at once.
```
slots = parse(transcript)            # pass 0
while unsatisfied(slots):
    ask(most_blocking(slots))        # exactly one constraint
    slots = merge(slots, answer)     # merge, never replace
```

**3. No restoring force — partial success is the new state.** Email sent + Slack failed *is* the state. No rollback, no "start over", no claiming success. The next pass retries Slack from where things actually are. Most agents get this wrong in one of two ways: claiming the whole run succeeded, or silently discarding the half that worked.

**4. Damping, not cancellation.** A pending action never expires. It decays into a quieter reminder — "still waiting on the Patel reply — send?" — on the next turn. Timeouts cancel; damping settles.

### What deliberately does not transfer
The motion. Beads swinging is the wrong metaphor for this audience: animation is capped at 120ms here because motion triggers sensory overload for autistic users and breaks tracking for ADHD. The physics is an **architecture** metaphor, not a visual one.

### The resulting UI rules
1. Nothing ever resets under the user. No timeout clears a pending confirmation. Ever.
2. Output appends, never overwrites. Newest at the bottom; previous stays readable.
3. A pending confirmation is a persistent card, not a modal — no focus steal, survives reload.
4. Partial input always survives a clarification.
5. No spinner replaces content. Pending shows as a quiet line under what you were reading.

---

## Palette — evidence-based, two themes

No single palette serves everyone. The British Dyslexia Association asks for **dark text on a light, non-white background**; photophobia and migraine (common with autism and ADHD) ask for **dark**. So both ship, with a toggle, remembered in `localStorage`. Default is the BDA one.

**Removed from the earlier draft after checking sources:**
- **Saturated yellow** — the most-reported sensory-overload colour for autistic users; its high luminance is the cause. My first draft used `#FFD166` as the focus ring. Gone.
- **Saturated red** — alarm response, and half of a red/green pair that 8% of men cannot separate.
- **Stark white `#FFF` and pure black `#000`** — BDA: white "appears too dazzling"; black-on-white causes halation for dyslexic readers.
- **Yellow-on-black, white-on-blue, grey-on-grey** — named by BDA as combinations to avoid.

### Status colours (both themes) — Okabe–Ito, desaturated
The Okabe–Ito set is the de-facto standard for colour-vision-deficiency safety (Nature Methods). Every status still carries an **icon and a word**, so colour is never the only signal.

```css
--ok:    #2E8B72;  /* bluish green, from Okabe-Ito #009E73, darkened   ✓ done    */
--wait:  #B06A00;  /* orange,      from Okabe-Ito #E69F00, darkened    ⏸ waiting */
--stop:  #A8442A;  /* vermillion,  from Okabe-Ito #D55E00, darkened    ⛔ blocked */
--accent:#0072B2;  /* blue, unchanged — safe across all CVD types                */
```
Deliberately not red/green as a pair; the three differ in **lightness** as well as hue, so they survive greyscale.

### Theme A — default, "paper" (BDA guidance)
```css
:root {
  --bg:        #FAF6EF;  /* warm cream, not white */
  --surface:   #F3EDE3;
  --surface-2: #EAE2D6;
  --line:      #D9CFC0;
  --text:      #2B2A28;  /* very dark warm grey, not black — 13.4:1 on --bg */
  --text-dim:  #5A564F;  /*                                   7.2:1 on --bg */
  --accent-ink:#FFFFFF;
  --focus:     #0072B2;  /* blue ring, 3px — appears nowhere else */
}
```

### Theme B — "dusk" (photophobia / migraine / low vision)
```css
:root[data-theme="dusk"] {
  --bg:        #14161B;  /* near-black, never #000 */
  --surface:   #1D2027;
  --surface-2: #262A33;
  --line:      #363B47;
  --text:      #E6E4DF;  /* off-white, never #FFF — 13.1:1 on --bg */
  --text-dim:  #A6A79F;  /*                          7.0:1 on --bg */
  --accent:    #6FA8DC;  /* lightened blue for dark ground */
  --accent-ink:#0E1117;
  --ok:        #6FC2A6;  /* lightened Okabe-Ito greens/oranges for contrast on dark */
  --wait:      #D9A05B;
  --stop:      #E08A76;
  --focus:     #6FA8DC;
}
```

Shared:
```css
:root { --radius: 14px; --tap: 64px; }
```

### The toggle
One button, top-right, labelled in words: **"Paper / Dusk"**. Not an icon-only sun/moon — icon-only controls fail for exactly this audience. Choice persists. Respects `prefers-color-scheme` on first visit only, then the user's choice wins.

## Type

```css
body {
  font: 18px/1.6 "Atkinson Hyperlegible", system-ui, -apple-system, "Segoe UI", sans-serif;
  letter-spacing: 0.01em;
  max-width: 68ch;           /* short lines — dyslexia and tracking */
}
```
Atkinson Hyperlegible (Braille Institute, free) is designed for low vision — letterforms that can't be confused with each other. One Google Fonts link; falls back to system UI if it fails. **No italics** (hard for dyslexic readers) — use weight instead. **No justified text** (rivers of whitespace).

## Layout

```
┌──────────────────────────────────────┐
│  [ 🎤  Hold to speak ]  ← 96px tall  │   one primary action, always in the same place
│  [ or type here…            ]        │   text box always present, same size
├──────────────────────────────────────┤
│  PENDING                             │   persistent card, only when something waits
│  Reply to Dr. Patel, Thu 2pm         │
│  [ ✓ Send ]        [ ✕ Don't send ]  │   destructive on the far right, 32px gap
├──────────────────────────────────────┤
│  WHAT I DID                    ↓ new │   aria-live="polite", appends downward
│  ✓ Sent reply to Dr. Patel           │
│  ✓ Added to calendar, Thu 2pm        │
│  ⛔ Slack failed — retry?            │
└──────────────────────────────────────┘
```

- Single column. No sidebar, no tabs, no navigation — there is nowhere else to go.
- **Every action line states the app, what happened, and the status in words.** "Sent reply to Dr. Patel" not "✓ Gmail".
- The two confirm buttons are **far apart** and differently shaped; the destructive one is never the default focus.
- Space bar toggles the mic; Enter submits text; Escape cancels the pending action (and says so).

## Motion: what earns its place

The bead-curtain reference is **ambient** motion — it moves whether or not you acted. That is the kind that hurts here: looping motion competes for attention (ADHD), unpredictable movement reads as threat (autism), and parallax triggers vestibular nausea. But banning motion outright is also wrong — motion that *shows causality* aids comprehension for everyone.

**Rule: every animation maps 1:1 to a real event. Nothing loops. Nothing autoplays. Nothing moves that the user did not cause.**

### 1. Mic level meter — the one genuinely live element
A bar that responds to actual input amplitude while the mic is open. This is **feedback, not decoration**: a blind user has no other way to know the mic is hearing them, and a user with unclear speech needs to see that something is arriving. Driven by `AnalyserNode`, so it reflects reality — a fake animated bar would be a lie about system state.
```js
// 5 bars, height from real amplitude, updated on rAF while listening only
const level = analyser.getByteFrequencyData(buf);  // never simulated
```
Stops the instant the mic closes. Never runs otherwise.

### 2. Action lines land one at a time, as each app actually completes
Gmail finishes, its line appears. Calendar finishes, its line appears. **No CSS needed — the timing is real latency.** This is the most "alive" thing on the page and it is entirely honest: the user is watching three apps change, in the order they changed.

Each line enters with 140ms fade + 4px rise. Small, one-shot, downward (matching the reading direction).
```css
@keyframes land { from { opacity:0; transform: translateY(4px) } to { opacity:1; transform:none } }
.action-line { animation: land 140ms ease-out }
```

### 3. Status change crossfades, never jumps
`⏸ waiting` → `✓ done` cross-fades over 120ms so the eye registers that *this specific line* changed, rather than the list appearing to reshuffle.

### 4. Pending card: no pulse, no glow
A looping pulse would be the obvious choice and it is wrong — persistent motion in the periphery is exactly the ADHD/autism failure mode. Instead the card is marked **statically**: 4px left border in `--wait`, the word "Waiting", and the `⏸` icon. Three redundant signals, zero movement.

### 5. Reduced motion is honoured completely
```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation: none !important; transition: none !important; }
}
```
The mic meter stays (it is information, not decoration) but switches to a **numeric level** instead of bars.

### Explicitly not doing
Parallax · confetti · skeleton shimmer · typing-dot indicators · bouncy/elastic easing (implies playfulness this tool should not have) · anything that moves while idle.

## Speech output rules (found in testing)

- **The echo is generated from slots, never from the transcript.** "umm reply to Patel" is spoken back as "Reply to Dr. Patel, Thursday 2pm — send?". A parroting agent would both sound broken and email the filler words.
- **Disfluencies are stripped from message bodies** (`umm, uh, er, like, you know`, repeated words) — but the **raw transcript stays visible on screen**. Never silently rewrite someone's words without showing the change.
- **Use the Natural voices.** Microsoft Zira (Chrome's default on Windows) spells unknown tokens letter-by-letter — "umm" became "U-M-M" in testing. Edge's neural voices (Aria, Jenny) don't.
- **Voice and speed are user-choosable and persisted.** Someone who hears this all day has a strong preference; 0.8×–1.6× covers most needs.
- **Self-corrections are honoured and surfaced.** When the user says "not 7… actually 2", the agent takes 2 — and the echo states the time it took, so a wrong pick is catchable in one sentence rather than discovered at 7pm.
- **Numbers, dates and times are spoken in full**: "Thursday the seventeenth of September at two pm", not "Thu 17/09 14:00".

## Gamification: mostly no, with one exception

Points, streaks and badges are a bad fit here and it is worth saying why in the brief:
- **Streaks punish bad days.** For someone whose motor symptoms fluctuate, a broken streak is a penalty for their disability.
- **Arbitrary reward schedules** are a known ADHD trap and read as confusing noise to many autistic users.
- It **trivialises a tool people depend on.** Nobody wants their assistive tech to congratulate them for sending an email.

What replaces it — **intrinsic progress, not scored progress**:
- **"What I did today"** — a plain running list, readable and countable. The satisfaction is the record itself, not a score.
- **Effort saved, measured honestly** — "3 apps updated, 1 sentence" sits at the end of a multi-app run. It is the interactions-per-task metric from the brief, shown to the user. True, useful, not a game.
- **Nothing celebratory on success.** Success is quiet. Only ambiguity and failure speak up.

## Accessibility checklist to state in the brief (WCAG 2.2)
- 2.5.8 Target Size (Minimum) — 64px, exceeds the 24px requirement
- 2.2.1 Timing Adjustable — no timeouts at all
- 2.4.7 Focus Visible — 3px `--focus` ring, 2px offset
- 2.5.7 Dragging Movements — no drag anywhere
- 1.4.3 / 1.4.6 Contrast — 7:1+ body text
- 2.3.3 Animation from Interactions — 120ms max; `prefers-reduced-motion` removes it
- 1.4.12 Text Spacing — survives user overrides
- 4.1.3 Status Messages — `aria-live` on the echo region

**Say honestly:** not tested with disabled users; built to WCAG 2.2 and to documented guidance, which is not the same thing.
