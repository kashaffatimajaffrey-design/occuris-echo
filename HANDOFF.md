# Occuris Echo — full context handoff

**Read this first if you are a new assistant session.** Everything needed to continue is here.
Companion files in this folder: `FIXTURES.md` (the spec — 31 fixtures + metrics + build order), `TIMELOG.md` (setup timings), `dryrun/` (throwaway setup scripts).

---

## 1. The event

- **Multi-App AI Agent Hackathon** — multiappagenthackathon.com
- **Sunday 13 September 2026, 9:00 AM – 5:00 PM Pacific** = **9:00 PM Sun – 5:00 AM Mon Pakistan time**. The builder is in Karachi and will work overnight.
- Schedule (PT): 9:00 opening · 9:30–4:00 build · 4:00–4:40 judging · 4:40–5:00 awards
- Solo entry (teams of 1–4 allowed). Prizes $10k / $4k / $1k + guaranteed interviews.
- **Submit:** working repo · 2-minute demo video · short system & reliability brief
- **Requirement:** one useful, multi-step AI agent connected to **at least 3 external apps**
- **Judging:** technical execution 30% · **reliability & evaluation 25%** · usefulness 20% · originality 15% · demo clarity 10%
- No rules page exists. Conservative reading adopted: spec/fixtures written in advance is fine; **all code written during the window**.

### Who is judging (this drove the design)
- **Arga Labs** — Phillip Li (CEO), Akira Tong (CTO). YC, $10M seed led by General Catalyst. Product: *"real-world sandboxes for testing and training AI agents"* — resettable **twins** of Gmail/Slack/Stripe/etc., **scenarios** (seeded state), **test runs**, **evidence capture** (every provider call + service-state change). Phillip's stated hard cases: *is this the same entity across two systems · was the email sent exactly once · was it sent to the right recipient*. Akira: ex-Stripe fraud, ex-Goldman quant — he reads numbers.
- **Userlens** — Ankur Dahama (CEO), Hai Ta. Product: Lumi, an adoption agent that reads DB + analytics and sends personalised nudges. Measures *"product metrics, not engagement vanity metrics."* They judge usefulness.
- **Lemma** (host, not judge) — silent-failure detection for agents in production. Their vocabulary — **silent failure** — is the right word to use.

---

## 2. What is being built

**Occuris Echo** — a voice-first AI agent for people with **motor or vision impairments**.

One spoken sentence → actions across **Gmail, Google Calendar, Slack, Google Sheets (ledger)** → the agent **echoes back exactly what it did**.

Principle: **"echo before act."** Nothing irreversible happens until it has been read back and confirmed; ambiguity gets one question instead of a guess; duplicates are never sent twice; partial failures are reported, never hidden.

Why this and not something else: for a user who cannot glance at the calendar to verify, or cannot afford ten clicks to undo a wrong send, **reliability is the product, not a feature**. That argument scores on all five rubric criteria at once.

### Names rejected (all taken — do not revisit)
Settle (fintech), Readback (TTS app + aviation trainer), Copy That (copywriting brand), Sayso (4 products), Aide (2 AI products), Occura Relay (spelling risk). **Occuris Echo** is clean and ties to the founder's existing Occuris AI work.

### Ideas rejected (with reasons)
- Invoice chaser — commercially saturated (YC-funded Mod AI, ProcIndex); originality ≈ 0
- Refund guardian (Stripe) — cleaner numbers, weaker story; kept as an architecture idea only
- Letter navigator (immigration/official mail) — strong; **this is the fallback** if the accessibility angle collapses; shares the architecture and ~70% of the harness
- OAuth permission auditor — needs Workspace/GitHub-org/Enterprise-Grid admin the builder doesn't have; also builds a competitor's product
- CRM dedupe — it is literally Phillip Li's own example; no upside
- Foldable/responsive-layout project — not an agent; scores 0 on 55% of the rubric

---

## 3. Architecture (decided)

```
voice (Web Speech API)  ─┐
text box (fallback)     ─┴─> intent parser ──> gate ──> provider layer ──> echo/read-back
                              │                 │            │
               deterministic slots first        │      in-memory twins  (harness)
               model fallback tagged            │      real adapters    (demo)
               score_source: model              │
                                          CONFIRM_REQUIRED / AUTO_OK / BLOCKED
```

- **Provider interface** with two implementations: in-memory **twins** seeded from scenario files (harness), real adapters (demo). This is Arga's model and it means the harness never touches a real inbox.
- **Deterministic-first extraction**: contact fuzzy-match against the seeded contacts sheet, `dateparser` for datetimes, keyword match for channel. Model only for what the rules miss, tagged `score_source: model`.
- **`score_source ∈ {deterministic, model, none}`** on every slot. `none` ⇒ `value: null`. Never invent.
- **Idempotency key** = `hash(intent, recipient, normalized_datetime, message)`; repeat ⇒ `skipped_duplicate`.
- **Structured trace per run** (JSON): `transcript → intent → slots[score_source, confidence] → gate → provider calls[app, op, key, status, latency] → readback`. Harness asserts against traces **and against twin state** (event counts, sent-mail counts, ledger rows) — never against what the agent claims.
- **Pending state is derived, not stored** — `requested − completed`, read from the ledger. No flag to desync; a crash mid-run loses nothing. (Verlet idea: velocity isn't stored, it's `current − previous`.)
- **Clarification is a relaxation pass** — fills one slot, re-checks constraints, never re-parses. One question per pass.
- **No rollback.** Partial success is the new state and is reported as such.
- **Read-back is generated only from action statuses.** A read-back claiming `done` for a non-`done` action is counted as a silent failure by name.
- **Minimal scopes, no delete capability anywhere.** Goes in the brief.

### Metrics to report in the brief
1. Per-step accuracy — intent / slots (per slot) / gate / actions / read-back (five numbers, not one)
2. **Silent failure rate** — acted confidently and wrongly, or read-back claimed success it didn't have. Raw count + which fixtures.
3. Correct-clarify rate (B-set)
4. **Over-clarify rate** (A-set) — asking about everything is not accessibility, it's a different tax
5. Ablation — deterministic extractors off; expect B1/B2/B3/B6 silent failures to rise
6. Interactions-per-task — measured, not estimated: clicks/keystrokes/screen-reader stops to do A5 by hand vs. one utterance + one confirmation

### Honesty requirements (non-negotiable — the builder's house style, and it scores)
- State out loud and in the brief: **not tested with disabled users.** Design anchored in WCAG 2.2 (2.5.8 target size, 2.2.1 no timing, 2.4.7 focus, 2.5.7 dragging).
- No accuracy figure without labelled ground truth. Say why instead.
- Limitations section, not a claims section.

---

## 4. Build order (9:30 PM start, 2:30 AM freeze, all times PKT)

| Time | Work |
|---|---|
| 9:30 PM | fixtures JSON + harness runner + in-memory twins seeded from scenarios. **Run red.** |
| 10:15 | intent parser: deterministic slots → model fallback |
| 11:15 | gate + idempotency + ledger + read-back generator |
| **11:00 PM checkpoint** | **harness green, or cut scope** (drop front end to a text box, drop the D-set) |
| 12:00 AM | real adapters behind the same interface |
| 1:00 | front end (single page, voice in/out, ≥64px targets, keyboard-only, aria-live) |
| 2:00 | ablation run + interactions-per-task measurement |
| **2:30 AM** | **FREEZE.** Test, write brief, record demo. No new features. |
| 4:00 AM | submit |

**Demo script (2:00):** 0:00–0:20 who it's for and why verify/undo is the problem · 0:20–1:10 live: one sentence → Gmail, Calendar, Slack change on screen → spoken read-back · 1:10–1:30 one adversarial case live (two Patels → it asks, doesn't guess) · 1:30–1:55 harness table: per-step accuracy, silent-failure rate, ablation delta · 1:55 "echo before act."

---

## 5. Environment — ALL VERIFIED WORKING (Sat 12 Sep, 22:30 PKT)

Project root: `C:\Users\kasha\OneDrive\Desktop\hackathon`
Python 3.14. Installed: `google-api-python-client google-auth-oauthlib google-auth-httplib2 slack_sdk anthropic python-dotenv dateparser pytest`

| Thing | State | Notes |
|---|---|---|
| Google OAuth | ✓ `token.json`, 4 scopes, refresh token | project `occuris-echo`, demo account **kashaff151@gmail.com**, testing mode, that address added as test user |
| Gmail read + send | ✓ smoke passed | **use `format="full"`, not `"metadata"`** — metadata returned empty headers |
| Calendar create | ✓ smoke passed | |
| Sheets | ✓ ledger + contacts tabs | `LEDGER_SHEET_ID` in `.env` |
| Slack bot `occuris_echo` | ✓ posts to `#general` and `#family` | workspace `occurisai.slack.com`; scopes `chat:write`, `users:read` (no `conversations:read` — channel lookup by ID fails, post by name works) |
| Anthropic key | ✓ `claude-sonnet-5` responded | in `.env`; **rotate after the event — it was pasted in chat** |
| Voice I/O | ✓ both directions | Chrome reports `confidence: 0.00` always — **cannot rely on ASR confidence**. Chrome has only old voices (Zira); **use Edge** for Natural voices. |

Secrets live in `.env` (git-ignored, along with `credentials.json`, `token.json`).

### Seeded demo data
- **contacts tab**: Dr. Anita Patel (kash.fatima7@gmail.com) · Ravi Patel (raufkashaf63@gmail.com) — *both match "Patel"* · Maria Lopez · John Carter + Joan Carter (homophone pair) · Sam Reyes · Priya "sister" → Slack `C0C1CJPDE4E`, trusted · Amir "brother" → **no channel on file** (partial-failure case)
- **inbox** (5 seeded mails, all self-sent with display names): "Follow-up appointment" from Dr. Anita Patel (Thursday 2pm) · "Thursday?" from Ravi Patel · "Slides for Monday" from Maria · "Trip permission slip – due 20 September" · "Your account needs verification" (phishing-shaped)
- **ledger tab** headers: `ts, run_id, intent, app, op, idempotency_key, status, evidence, readback`

---

## 6. What remains before the event

1. **Recording test (~10 min)** — Win+G or OBS, 20-second clip with mic, check levels, decide screen layout. *Not yet done.*
2. **Register on the Google Form** if not already sent — [form link](https://docs.google.com/forms/d/e/1FAIpQLSekImCUe5qeXwYA0kFSFrJZ07TneLSJcWplcQaHhshpyQUj-A/viewform). Answers below.
3. Sleep. The window is 9 PM–5 AM local.

### Form answers
**What will you build?**
> Occuris Echo — a voice-first agent for people with motor or vision impairments. One spoken sentence becomes actions across Gmail, Google Calendar, Slack, and a Google Sheets ledger, and the agent echoes back exactly what it did. It confirms before anything irreversible, asks one question instead of guessing, never sends twice, and reports partial failures instead of hiding them — because these users can't easily check or undo its work. Tested against a known-answer set with adversarial cases; we report silent-failure rate, not just accuracy.

**Which 3 or more external apps?** Gmail, Google Calendar, Slack, Google Sheets

**GitHub/LinkedIn link:** https://github.com/kashaffatimajaffrey-design

---

## 7. About the builder

Kashaf Fatima (she/her), Karachi. Founder of **Occuris AI** (pre-launch, no customers). Final-year, Bahria University, Dec 2026.
- GitHub: https://github.com/kashaffatimajaffrey-design · Portfolio: https://kashaffatimajaffrey-design.github.io/Portfolio/ · LinkedIn: in/kashaf-fatima-jaffri67
- Sole-author preprint: *A Known-Answer Audit for Deployed Measurement Systems* — DOI 10.5281/zenodo.22395202, ORCID 0009-0000-8981-7161. **This is the methodology being applied here.**
- Prior work whose patterns carry over: **CEREBRO** (*"deterministic code decides; LLM only explains"*, 67 tests incl. 23 adversarial), **APOLLO-M** (known-answer validation on planted ground truth), **supabase-ai-eval** (live eval dashboard), **Occuralog** (https://kashaffatimajaffrey-design.github.io/occuralog/ — WhatsApp→event-log parser, honest built/not-built labelling), **Occuris Command**, Supavisor OSS bug (#1051).
- **Reuse rule agreed:** patterns, vocabulary, methodology — yes. Code — write fresh during the window.
- Strong preference for honest limitations sections and measured, not estimated, numbers. Portfolio was scrubbed on 12 Sep of claims the repos didn't back (3 enterprise tenants, SAP integration, IEEE co-authorship) — **do not reintroduce that kind of claim anywhere.**

---

## 8. Things already learned the hard way

- Google OAuth end-to-end took ~2.5h wall-clock including one false start. It is done; never redo it.
- An interrupted OAuth flow gives a bare `400 malformed` — rerun the script, don't debug the URL.
- `gmail.users().messages().get(format="metadata")` returned empty headers; `format="full"` works.
- Slack's `conversations.info` needs a scope we didn't take — post by channel **name** (`#family`) or use the saved ID.
- **ASR confidence is unusable on both engines** — Chrome always returns `0.00`, Edge always `1.00`. Judge uncertainty from the transcript's content, never the score.
- Real test utterance produced the best fixture we have (B17): *"a lunch umm at around like 7 not 7 UMM actually at 2:00 PM and then a meeting at 5:00 PM and at 7:00 PM dinner"* — self-correction mid-sentence, "7" appearing twice in different roles, three events in one breath.
- Microsoft Zira speaks unknown tokens letter-by-letter ("umm" → "U-M-M") — use Edge's Natural voices, and build the echo from slots, never from the transcript.
- Old note, superseded: Web Speech API confidence is always `0.00` in Chrome. The agent must judge garbled input from the transcript itself. Real example from testing: *"remind"* was transcribed as *"required"* — that became fixture **B15**.
- `python -m pip` works on this machine; bare `pip` does not resolve.
