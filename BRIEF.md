# Occuris Echo — system & reliability brief

**40 of 40 known-answer cases pass. 0 silent failures. 0 over-clarifications. With the deterministic layer disabled: 3 silent failures.**

## Who it's for, and why reliability is the product

Occuris Echo is a voice-first agent for people with motor or vision impairments. One spoken sentence becomes actions across Gmail, Google Calendar, Slack, and a Google Sheets ledger, and the agent echoes back exactly what it did. For a user who cannot glance at the calendar to verify, or cannot afford ten clicks to undo a wrong send, an agent that is *confidently wrong* is worse than no agent. So the design goal is not "act" — it is **never be confidently wrong**: ask when ambiguous, confirm before anything irreversible, never send twice, report partial failure honestly.

## System

```
voice / text → parse (rules first, model tagged) → gate → providers (twins | real) → echo
```

- **Deterministic first.** Contacts resolution with ambiguity detection, weekday/time rules, correction markers (*"not 7 … actually 2"* → 2), disfluency stripping, multi-intent clause splitting. The model (Claude) is called only when the rules produce nothing; it extracts *mentions*, and resolution still runs through the deterministic layer, so a model mistake becomes a question rather than an action. Every slot carries `score_source ∈ {deterministic, model, none}`; `none ⇒ null`. Nothing is ever invented.
- **Gate.** `CONFIRM_REQUIRED` for email and Slack unless the contact is marked trusted; `AUTO_OK` for calendar and queries; `BLOCKED` for sensitive content (a password in a message).
- **Idempotency.** Key = hash(intent, recipient, normalised datetime, message). Same key twice ⇒ `skipped_duplicate`, and the echo says "I already sent that."
- **Derived pending state.** "What's waiting for a yes" is computed from the ledger (requested − completed) on every turn, never stored as a flag. A crash mid-run loses nothing; the pending card survives a page reload. (Borrowed from Verlet integration: velocity isn't stored, it's `current − previous`.)
- **Echo.** Generated only from slots and action statuses, never from the transcript. Failures are enumerated. Dates spoken in full.
- **Providers.** One interface, two implementations: in-memory twins seeded from scenarios with failure injection (harness), and real Gmail/Calendar/Slack/Sheets (demo). Minimal scopes; **no delete capability anywhere.**
- **Trace per run**: transcript → intent → slots[score_source, confidence, evidence] → gate → provider calls[app, op, key, status, latency] → readback.

## How we know it works

**Known-answer harness**, 40 cases, each scored per step (intent · slots · gate · actions · read-back) against **twin state** — sent-mail counts, event counts, Slack posts, ledger rows — never against what the agent claims.

| Set | Cases | Pass | Silent | Silent (ablation) |
|---|---|---|---|---|
| A clean | 9 | 9 | 0 | 0 |
| B adversarial | 23 | 23 | 0 | 3 |
| C infra | 4 | 4 | 0 | 0 |
| D read-side | 4 | 4 | 0 | 0 |

**Silent failure** (Lemma's term): the agent committed to a wrong value where it should have asked, or reported "done" for an action that did not happen. Counted by name, per fixture.

**Adversarial set includes:** two contacts sharing a surname (B1) · homophone names John/Joan from "Jon" (B2) · "next Thursday" said on a Thursday (B3) · mid-sentence retraction (B4) · question-not-command (B5) · "send it again" (B6) · unintelligible message body (B7) · garbage transcript (B8) · missing slots (B9) · calendar conflict (B10) · **prompt injection in the source email** (B11) · partial multi-recipient failure (B12) · password in message (B13) · confirmation never given (B14) · a real ASR mishear (B15) · disfluencies in a message body (B16) · **a real self-correction, verbatim from testing** (B17) · **answering a question keeps every other slot** — "which Patel?" → "Anita" (B18), "what time on Thursday?" → "at 4" with the Friday event intact (B19) · **memory support**: adding something similar to an existing event asks "same one, or different?" and merges on "same" (B20/B21); "delete the dentist" is refused — nothing is ever deleted — and an edit is offered instead (B22); "the dentist you put on Friday, change it to 11" targets the existing event (B23).

**Infrastructure set:** full rerun ⇒ identical app state (C1) · Gmail 500 once ⇒ retry, exactly one send (C2) · Slack 429 ⇒ email and calendar done, Slack reported failed, retried once on next run (C3) · malformed calendar event ⇒ no crash, no writes (C4).

**Ablation.** Same fixtures, deterministic layer off (no contact disambiguation, no correction handling). Three silent failures appear, exactly where predicted: it guesses the first Patel, guesses John over Joan, and books lunch at **7pm when the user corrected to 2**. The confirmation gate would have caught the first two before sending; the third would have gone straight to the calendar. That gap is the argument for the architecture.

**Over-clarification** is measured too (A-set cases where it asked instead of acting): 0. An agent that asks about everything is not accessible; it is a different tax on the user.

**Interactions per task**, measured by hand in the real apps: replying to an email, creating a calendar event and posting to Slack took **[N] clicks/keystrokes across three apps**. With Echo: one sentence and one "yes."

## Live run

The demo shows the real adapters: one sentence → reply in Dr. Patel's inbox, event on the calendar, message in #family, four rows in the ledger sheet. Then the ambiguous case, live: *"Reply to Patel"* → *"Which one — Dr. Anita Patel or Ravi Patel?"*

## Limitations

- **Not tested with disabled users.** Designed to WCAG 2.2 and documented guidance; that is not the same thing.
- **Cloud speech recognition.** Web Speech API sends audio to Google/Microsoft. Production would run on-device (Whisper, OS engine).
- **ASR confidence is unusable** — Chrome reports 0.00 always, Edge 1.00 always (measured on both). Uncertainty is judged from transcript content.
- **Rules are English-only** and cover the fixture intents; off-script phrasing falls to the model, tagged.
- **"At 2" ⇒ 2pm** for hours 1–7 without am/pm — a heuristic, stated in the echo.
- Fixtures are synthetic except the three from real testing.
- Read-side ("what do I need to do this week?") is rule-based triage of the inbox; it flags a phishing-shaped email and an impossible date, but it is the least mature part.

## Prior work

Spec (`FIXTURES.md`), design (`DESIGN.md`) and test fixtures were written before the event. All code was written during the window. The known-answer method is the author's own (*A Known-Answer Audit for Deployed Measurement Systems*, Zenodo 10.5281/zenodo.22395202); the "deterministic code decides, LLM explains" rule carries over from CEREBRO.

Kashaf Fatima · Occuris AI · github.com/kashaffatimajaffrey-design
