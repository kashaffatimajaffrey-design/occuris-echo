"""The agent loop: parse → gate → (confirm) → act → read back. Every run emits a Trace.

Pending state is DERIVED from the ledger (requested − completed), never stored as a flag."""
from __future__ import annotations

import os
import re
import time
import uuid
from datetime import datetime

from .contacts import Contacts
from .models import Action, Gate, Intent, Slot, Source, Status, SubIntent, Trace, idempotency_key
from .parse import INJECTION, WEEKDAYS, parse
from .providers.base import ProviderError
from .readback import readback_for, speak_when

YES = re.compile(r"^\s*(yes|yeah|yep|yup|sure|ok(ay)?|go ahead|send it|do it|confirm|correct)\b", re.I)
NO = re.compile(r"^\s*(no|nope|don'?t|cancel|stop|never ?mind)\b", re.I)
RETRIES = {500: 2, 502: 2, 503: 2, 429: 2}


class Agent:
    def __init__(self, providers, use_model: bool = False, ablate: bool = False, today: str | None = None):
        self.p = providers
        self.use_model = use_model
        self.ablate = ablate
        self.contacts = Contacts(providers.contacts())
        self.today = datetime.fromisoformat((today or getattr(providers, "today", None) or datetime.now().date().isoformat()) + "T09:00")
        self.traces: list[Trace] = []
        # clarification context: the sentence that raised a question, and what was asked.
        # A short answer on the next turn is merged back in and the whole sentence re-parsed —
        # one relaxation pass; every other slot survives.
        self._ctx: dict | None = None

    # ------------------------------------------------------------ pending (derived)
    def pending(self) -> list[dict]:
        """Replay the ledger in order; a key's last row decides. Pending = last row says pending."""
        state: dict[str, dict] = {}
        for r in self.p.ledger_rows():
            state[r["idempotency_key"]] = r
        return [r for r in state.values() if r.get("status") == "pending"]

    def _completed_keys(self) -> set[str]:
        return {r["idempotency_key"] for r in self.p.ledger_rows() if r.get("status") == "done"}

    # ------------------------------------------------------------ main entry
    def handle(self, transcript: str) -> Trace:
        run_id = uuid.uuid4().hex[:8]
        pend = self.pending()

        # 1. confirmation turn?
        if pend and YES.match(transcript):
            return self._execute_pending(run_id, transcript, pend)
        if pend and NO.match(transcript) and len(transcript.split()) <= 3 and not re.search(r"\b(cancel|call off)\s+(the|my)\b", transcript, re.I):
            for r in pend:
                self.p.ledger_append({**r, "status": "cancelled", "ts": _now()})
            self._ctx = None
            tr = Trace(run_id, transcript, transcript, Intent.REFUSE, [], Gate.AUTO_OK, [], "Cancelled. Nothing was sent.")
            self.traces.append(tr); return tr
        if not pend and self._ctx and self._ctx.get("kind") in ("when", "who", "title", "message") and (YES.match(transcript) or NO.match(transcript)) and len(transcript.split()) <= 3:
            q = self._ctx["asker"].question
            tr = Trace(run_id, transcript, transcript, Intent.CLARIFY, [], Gate.AUTO_OK, [], f"I need the answer itself, not a yes or no — {q}", question=q)
            self.traces.append(tr); return tr
        if not pend and not self._ctx and (YES.match(transcript) or NO.match(transcript)) and len(transcript.split()) <= 2:
            tr = Trace(run_id, transcript, transcript, Intent.QUERY, [], Gate.AUTO_OK, [],
                       "Nothing is waiting for a yes or no right now." if YES.match(transcript) else "Okay — nothing to cancel.")
            self.traces.append(tr); return tr

        # 2. parse — merging a short answer into the previous question's sentence if there is one
        subs, meta = parse(transcript, self.contacts, self.today, ablate=self.ablate)
        if self._ctx and subs and all(x.intent == Intent.QUERY for x in subs) and len(transcript.split()) > 4                 and not (self._ctx["kind"] == "when" and self._first_clause_is_time(transcript)):
            # user asked something else while a question was open — answer, then remind, keep the question
            ctx = self._ctx
            rb = self._answer_query(subs[0], transcript)
            asker = ctx["asker"]
            what = asker.title.value or asker.recipient.value or "that"
            if ctx.get("reminded"):
                self._ctx = None   # asked once already; the user has moved on
                tr = Trace(run_id, transcript, transcript, Intent.QUERY, subs, Gate.AUTO_OK, [], rb)
                self.traces.append(tr); return tr
            rb = rb.rstrip(".") + f". And I still need an answer on the earlier one — {asker.question}"
            tr = Trace(run_id, transcript, transcript, Intent.QUERY, subs, Gate.AUTO_OK, [], rb, question=asker.question)
            ctx["reminded"] = True
            self.traces.append(tr); self._ctx = ctx
            return tr
        if self._ctx and subs and any(x.clause == "__correct_person__" for x in subs):
            self._ctx = None   # an explicit correction outranks the open question
        if self._ctx and self._ctx["kind"] in ("similar", "when", "title") and self._ctx["asker"].intent == Intent.CREATE_EVENT and subs and len(subs) == 1 and subs[0].intent == Intent.UPDATE_EVENT and subs[0].clause == "__last__" and subs[0].datetime.value:
            # "Not Wednesday, Thursday" while a question about a not-yet-created event is open: move that event's day
            asker = self._ctx["asker"]
            old_dt = asker.datetime.value or "T12:00"
            asker.datetime = Slot(subs[0].datetime.value[:10] + old_dt[10:], Source.deterministic, 0.85, "day corrected before adding")
            asker.question = None; asker.conflict = None
            subs = self._ctx["subs"]; meta = {"cleaned": transcript, "corrections": []}
            self._ctx = None
        elif self._ctx and self._ctx["kind"] == "similar" and re.search(r"\b(same|update it|merge)\b", transcript, re.I) and not re.search(r"\b(different|another|separate|new)\b", transcript, re.I):
            asker = self._ctx["asker"]
            s_upd = SubIntent(intent=Intent.UPDATE_EVENT, clause=asker.conflict)
            s_upd.title = asker.title; s_upd.datetime = asker.datetime
            s_upd.recipient = Slot(asker.conflict, Source.deterministic, 0.9, "existing event id")
            subs = [s_upd]; meta = {"cleaned": transcript, "corrections": []}
        elif self._ctx and self._ctx["kind"] == "similar" and re.search(r"\b(different|another|separate|new one|add it|no)\b", transcript, re.I):
            asker = self._ctx["asker"]; asker.question = None; asker.conflict = None; asker.clause = "__force_create__"
            subs = self._ctx["subs"]; meta = {"cleaned": transcript, "corrections": []}
        elif self._ctx and (not subs or len(transcript.split()) <= 4
                            or (self._ctx["kind"] == "title" and len(subs) == 1 and subs[0].intent == Intent.RENAME_EVENT)
                            or (self._ctx["kind"] == "when" and self._first_clause_is_time(transcript))):
            if self._ctx["kind"] == "title":
                asker = self._ctx["asker"]
                given = next((x.title.value for x in subs if x.intent == Intent.RENAME_EVENT and x.title.value), None)
                asker.title = Slot(_clean_title_answer(given or transcript), Source.deterministic, 0.9, "user named it when asked")
                asker.question = None; self._answered = True
                subs = [x for x in self._ctx["subs"] if x.intent != Intent.QUERY] or self._ctx["subs"]
                meta = {"cleaned": self._ctx["text"] + " — " + transcript, "corrections": []}
            elif self._ctx["kind"] == "when":
                from .parse import parse_when, split_clauses
                first = split_clauses(transcript)[0]
                trailing_q = [c for c in split_clauses(transcript)[1:] if c.rstrip().endswith("?") or re.search(r"\b(do i have|am i free|what'?s on)\b", c, re.I)]
                when, amb = parse_when(re.sub(r"\b(works|is fine|is good|would be good|please)\b", "", first, flags=re.I), self.today)
                self._followup_q = trailing_q[0] if trailing_q else None
                asker = self._ctx["asker"]
                if when.value and not amb:
                    base = asker.datetime.value
                    if base and "no-hour" in (asker.datetime.evidence or "") and "no-hour" not in (when.evidence or "") and not re.search(r"\b(" + "|".join(WEEKDAYS) + r"|tomorrow|today)\b", first, re.I):
                        when = Slot(base[:10] + when.value[10:], Source.deterministic, 0.85, "time answered; day kept")
                    asker.datetime = when
                    asker.question = None; self._answered = True
                    subs = [x for x in self._ctx["subs"] if x.intent != Intent.QUERY] or self._ctx["subs"]
                    meta = {"cleaned": self._ctx["text"] + " — " + transcript, "corrections": []}
                else:
                    merged = self._merge_answer(transcript)
                    if merged:
                        transcript = merged
                        subs, meta = parse(transcript, self.contacts, self.today, ablate=self.ablate)
            elif self._ctx["kind"] != "when":
                cands, _ = self.contacts.resolve(transcript.strip(" ."))
                if len(cands) == 1 and self._ctx["kind"] != "who":
                    needy = next((x for x in self._ctx["subs"] if x.intent in (Intent.REPLY_EMAIL, Intent.SEND_EMAIL, Intent.NOTIFY) and not x.recipient.value), None)
                    if needy:
                        self._ctx["kind"] = "who"; self._ctx["asker"] = needy
                merged = self._merge_answer(transcript)
                if merged:
                    transcript = merged; self._answered = True
                    subs, meta = parse(transcript, self.contacts, self.today, ablate=self.ablate)
        dropped = None
        if self._ctx and not getattr(self, "_answered", False) and subs and any(x.intent != Intent.QUERY for x in subs) and len(transcript.split()) > 4:
            a0 = self._ctx["asker"]
            dropped = a0.title.value or a0.recipient.value or a0.clause[:30]
        self._ctx = None
        self._answered = False
        cleaned = meta.get("cleaned", transcript)
        corrections = meta.get("corrections", [])

        if meta.get("garbage"):
            return self._finish(Trace(run_id, transcript, cleaned, Intent.REFUSE, [], Gate.AUTO_OK, [],
                                      "Sorry, I didn't catch that. Could you say it again?", corrections=corrections), pend)
        if meta.get("retraction"):
            return self._finish(Trace(run_id, transcript, cleaned, Intent.REFUSE, [], Gate.AUTO_OK, [],
                                      "Cancelled — nothing was sent.", corrections=corrections), pend)
        if meta.get("sensitive"):
            return self._finish(Trace(run_id, transcript, cleaned, Intent.REFUSE, subs, Gate.BLOCKED, [],
                                      "I won't send a password or similar secret by email or chat. Nothing was sent.",
                                      blocked_reason="sensitive content", corrections=corrections), pend)
        model_note = None
        if not subs and self.use_model and not self.ablate:
            from .model import model_parse
            subs, model_note = model_parse(transcript, self.contacts, self.today)
        if not subs:
            return self._finish(Trace(run_id, transcript, cleaned, Intent.CLARIFY, [], Gate.AUTO_OK, [],
                                      "I'm not sure what you'd like me to do. Could you say it another way?",
                                      question="I'm not sure what you'd like me to do — could you say it another way?",
                                      corrections=corrections), pend)

        # 3. ablation: throw away deterministic slot evidence for names → forces guessing
        if self.ablate:
            self._ablate(subs)

        # 4. queries — including "I want to book lunch… am I free this week?": answer, keep the lunch open
        def _no_time(x):
            return not x.datetime.value or "no-hour" in (x.datetime.evidence or "")
        if any(s.intent == Intent.QUERY for s in subs) and any(s.intent == Intent.CREATE_EVENT and _no_time(s) for s in subs):
            qsub = next(s for s in subs if s.intent == Intent.QUERY)
            csub = next(s for s in subs if s.intent == Intent.CREATE_EVENT)
            rb = self._answer_query(qsub, transcript)
            what = csub.title.value or "that"
            csub.question = f"When should I put {what} — what day and time?"
            self._remember_question(cleaned, [csub])
            rb = rb.rstrip(".") + f". Pick a day and time and I'll add {what}."
            return self._finish(Trace(run_id, transcript, cleaned, Intent.QUERY, subs, Gate.AUTO_OK, [], rb, question=csub.question, corrections=corrections), pend)
        if all(s.intent == Intent.QUERY for s in subs):
            rb = self._answer_query(subs[0], transcript)
            return self._finish(Trace(run_id, transcript, cleaned, Intent.QUERY, subs, Gate.AUTO_OK, [], rb, corrections=corrections), pend)

        # CREATE_EVENT with no title: ask the model for one (tagged), else ask the user — never guess "Meeting"
        for s in subs:
            if s.intent == Intent.CREATE_EVENT and not s.title.value and not s.question:
                title = None
                if self.use_model and not self.ablate:
                    from .model import model_title
                    title = model_title(s.clause or transcript)
                if title:
                    s.title = Slot(title, Source.model, 0.6, "model named the event from the utterance")
                else:
                    s.question = "What should I call that on your calendar?"
                    self._remember_question(cleaned, subs)
        # RENAME_EVENT: by the old name if given, else the most recent event this session created
        for s in subs:
            if s.intent == Intent.RENAME_EVENT:
                ev = self.p.calendar_find(s.recipient.value) if s.recipient.value else None
                if ev:
                    s.clause, s.datetime = ev["id"], Slot(ev["start"], Source.deterministic, 0.9, "existing event")
                    s.recipient = Slot(ev["title"], Source.deterministic, 0.9, "matched by name")
                    continue
                last = next((r for r in reversed(self.p.ledger_rows()) if r.get("app") == "calendar" and r.get("status") == "done" and r.get("event_id")), None)
                if not last:
                    s.question = "Which event should I rename? Say its name."
                else:
                    s.datetime = Slot(last.get("datetime"), Source.deterministic, 0.9, "the event just created")
                    s.clause = last["event_id"]
                    s.recipient = Slot(last.get("title"), Source.deterministic, 0.9, "old title")
        # person correction after the fact: we can't unsend an email — offer the same message to the right person, fix the event name
        for s in list(subs):
            if s.clause == "__correct_person__":
                last_mail = next((r for r in reversed(self.p.ledger_rows()) if r.get("app") == "gmail" and r.get("status") == "done"), None)
                wrong = last_mail.get("recipient") if last_mail else None
                if not last_mail:
                    # no email went out — just fix the event that names the wrong person
                    new = _short(s.recipient.value)
                    ev = next((e for e in self.p.calendar_upcoming() if re.search(r"\b(dr\.?\s+)?\w+\s+patel\b|\bwith\b", (e.get("title") or "").lower())), None)
                    ev = next((e for e in self.p.calendar_upcoming() if "with" in (e.get("title") or "").lower() and new.lower() not in (e.get("title") or "").lower()), ev)
                    subs.remove(s)
                    if ev:
                        r = SubIntent(intent=Intent.RENAME_EVENT, clause=ev["id"])
                        old_t = ev["title"] or ""
                        new_t = re.sub(r"\bwith\b.*$", f"with {new}", old_t, flags=re.I) if "with" in old_t.lower() else f"{old_t} with {new}"
                        r.title = Slot(new_t, Source.deterministic, 0.85, "person corrected")
                        r.recipient = Slot(old_t, Source.deterministic, 0.9, "old title"); r.datetime = Slot(ev["start"], Source.deterministic, 0.9)
                        subs.append(r)
                    else:
                        self._note = f"I don't see an event with a person in it to change to {new}. "
                    continue
                s.message = Slot(last_mail.get("message") or last_mail.get("readback", "").split("“")[-1].rstrip("”"), Source.deterministic, 0.8, "same message as the one already sent")
                s.clause = ""
                ev = next((e for e in self.p.calendar_upcoming() if wrong and _short(wrong).lower() in (e.get("title") or "").lower()), None)
                if ev:
                    r = SubIntent(intent=Intent.RENAME_EVENT, clause=ev["id"])
                    r.title = Slot((ev["title"] or "").replace(_short(wrong), _short(s.recipient.value)), Source.deterministic, 0.85, "person corrected")
                    r.recipient = Slot(ev["title"], Source.deterministic, 0.9, "old title")
                    r.datetime = Slot(ev["start"], Source.deterministic, 0.9)
                    subs.append(r)
                self._note = f"I already emailed {_short(wrong)} — I can't unsend that. "
        # "Not Sunday, Friday." — move the event just created to the corrected day
        for s in subs:
            if s.intent == Intent.UPDATE_EVENT and s.clause == "__last__":
                last = next((r for r in reversed(self.p.ledger_rows()) if r.get("app") == "calendar" and r.get("status") == "done" and r.get("event_id")), None)
                if not last or not s.datetime.value:
                    s.question = "Which event should I move, and to when?"
                else:
                    old_time = (last.get("datetime") or "T09:00")[10:]
                    s.datetime = Slot(s.datetime.value[:10] + old_time, Source.deterministic, 0.85, "day corrected, time kept")
                    s.title = Slot(None, Source.none)   # only the day changes
                    s.recipient = Slot(last["event_id"], Source.deterministic, 0.9, "existing event id")
        # soft-cancel: mark it cancelled, keep it visible; never delete
        for s in list(subs):
            if s.intent == Intent.UPDATE_EVENT and re.search(r"\b(cancel|call off|scrap|strike)\b", transcript, re.I) and not re.search(r"\b(delete|remove)\b", transcript, re.I):
                ev = self.p.calendar_find(s.title.value or "") if s.title.value else None
                if not ev:
                    last = next((r for r in reversed(self.p.ledger_rows()) if r.get("app") == "calendar" and r.get("status") == "done" and r.get("event_id")), None)
                    ev = next((e for e in self.p.calendar_upcoming() if last and e["id"] == last["event_id"]), None)
                if ev and not (ev.get("title") or "").lower().startswith("cancelled"):
                    r = SubIntent(intent=Intent.RENAME_EVENT, clause=ev["id"])
                    r.title = Slot(f"Cancelled — {ev['title']}", Source.deterministic, 0.9, "soft cancel")
                    r.recipient = Slot(ev["title"], Source.deterministic, 0.9, "old title"); r.datetime = Slot(ev["start"], Source.deterministic, 0.9)
                    subs.remove(s); subs.append(r)
                    self._note = "I don't delete things, so I've marked it cancelled and left it visible — open it in Calendar if you want it gone. "
                elif ev:
                    s.question = f"“{ev['title']}” is already marked cancelled."
                else:
                    s.question = "Which event should I cancel? Say its name."
        # UPDATE_EVENT phrased as delete: we never delete — say so and offer to change it instead
        for s in subs:
            if s.intent == Intent.UPDATE_EVENT and re.search(r"\b(delete|remove|get rid of)\b", transcript, re.I):
                ev = self.p.calendar_find(s.title.value or "")
                s.question = (f"I don't delete things — that's deliberate, so nothing disappears by accident. "
                              f"I can change “{ev['title']}” instead: say a new time or a new name." if ev
                              else "I don't delete things — that's deliberate. Which event did you mean, and what should change?")
        # CREATE_EVENT: is there already something similar? Ask "same or different?" — the user may have forgotten.
        for s in subs:
            if s.intent == Intent.CREATE_EVENT and s.title.value and s.datetime.value and not s.question and s.clause != "__force_create__":
                sim = self._similar_event(s.title.value, s.datetime.value)
                if sim:
                    s.question = (f"You already have “{sim['title']}” {speak_when(sim['start'])}. Is this the same one? "
                                  f"Say “same” and I'll update it with the new details, or “different” to add another.")
                    s.conflict = sim["id"]  # remembered for the answer
                    self._remember_question(cleaned, subs)
        # SEND_EMAIL with no body: compose from the event in the same breath (the user's own words), else ask
        for s in subs:
            if s.intent in (Intent.SEND_EMAIL, Intent.REPLY_EMAIL) and not s.message.value and not s.question:
                ev = next((x for x in subs if x.intent == Intent.CREATE_EVENT and x.datetime.value), None)
                if ev:
                    s.message = Slot(f"{(ev.title.value or 'Meeting').capitalize()} — {speak_when(ev.datetime.value)}. Does that work for you?",
                                     Source.deterministic, 0.75, "composed from the event you asked me to add")
                elif s.datetime.value:
                    s.message = Slot(f"{speak_when(s.datetime.value)} works for me.", Source.deterministic, 0.75, "from the time you said")
                else:
                    s.question = "What should the email say?"
        # share a message across NOTIFY siblings ("tell A and B I'll be late")
        msgs = [s.message.value for s in subs if s.message.value]
        whens = [s.datetime.value for s in subs if s.datetime.value]
        for s in subs:
            if s.intent == Intent.NOTIFY and not s.message.value:
                if msgs:
                    about = next((x.recipient.value for x in subs if x.intent in (Intent.REPLY_EMAIL, Intent.SEND_EMAIL) and x.recipient.value), None)
                    text = f"{msgs[-1]} (re {_short(about)})" if about else msgs[-1]
                    s.message = Slot(text, Source.deterministic, 0.7, "shared with sibling clause")
                elif whens:
                    s.message = Slot(f"{speak_when(whens[-1])} works", Source.deterministic, 0.6, "from shared time")
                elif not s.question:
                    s.question = f"What should I say to {_short(s.recipient.value) if s.recipient.value else 'them'}?"
        # UPDATE_EVENT with a time but no day: keep the event's own date
        for s in subs:
            if s.intent == Intent.UPDATE_EVENT and s.datetime.value and "no-day" in (s.datetime.evidence or ""):
                ev = self.p.calendar_find(s.title.value or "")
                if ev:
                    s.datetime = Slot(ev["start"][:10] + s.datetime.value[10:], Source.deterministic, 0.85, "time from utterance, date from existing event")
                elif s.title.value and not s.question and s.clause != "__last__":
                    s.question = f"I can't find an event called “{s.title.value}” on your calendar — which one do you mean?"
                    s.datetime = Slot(None, Source.none, 0.0, "no event to take the date from")
        # a resolved NOTIFY contact with no channel on file needs a question, not a failed call;
        # one with email but no Slack gets an email instead
        for s in subs:
            if s.intent == Intent.NOTIFY and s.recipient.value and not s.question:
                row = self._contact(s.recipient.value)
                if row is not None and not row.get("slack"):
                    if row.get("email"):
                        s.intent = Intent.SEND_EMAIL
                        s.channel = Slot("email", Source.deterministic, 0.8, "no Slack on file; email instead")
                    else:
                        s.question = f"How should I reach {_short(s.recipient.value)}? I don't have a channel on file."
        for s in subs:
            if s.intent == Intent.SEND_EMAIL and s.recipient.value and not s.question:
                row = self._contact(s.recipient.value)
                if row is not None and not row.get("email") and row.get("slack"):
                    s.intent = Intent.NOTIFY
                    s.channel = Slot("slack", Source.deterministic, 0.8, "no email on file; Slack instead")
        # 5. clarifications — one question, the most blocking
        q = next((s.question for s in subs if s.question), None)
        if q:
            top = Intent.MULTI if len(subs) > 1 else subs[0].intent
            # B12: partial — do the sub-intents that *can* run, ask about the rest
            runnable = [s for s in subs if not s.question and s.intent == Intent.NOTIFY and self._trusted(s) and (self._contact(s.recipient.value) or {}).get("slack")]
            actions = []
            if runnable and len(subs) > 1:
                actions = [self._do(s, run_id) for s in runnable]
                actions += [Action("slack", "post", idempotency_key(s.intent, s.recipient.value, None, s.message.value), Status.not_attempted,
                                   detail=s.question or "") for s in subs if s.question]
                rb = readback_for(actions, subs) + " " + q
                return self._finish(Trace(run_id, transcript, cleaned, Intent.MULTI, subs, Gate.AUTO_OK, actions, rb, question=q, corrections=corrections), pend)
            self._remember_question(cleaned, subs)
            return self._finish(Trace(run_id, transcript, cleaned, Intent.CLARIFY, subs, Gate.AUTO_OK, [], q, question=q, corrections=corrections), pend)

        # 6. resolve threads / conflicts / injection
        injection = False
        for s in subs:
            if s.intent == Intent.REPLY_EMAIL and s.recipient.value:
                row = self._contact(s.recipient.value)
                th = self.p.gmail_find_thread(row["email"]) if row and row.get("email") else None
                if th and INJECTION.search(th.get("body", "")):
                    injection = True
            if s.intent == Intent.CREATE_EVENT and s.datetime.value:
                day = s.datetime.value[:10]
                for e in self.p.calendar_list(day):
                    if _overlaps(e, s.datetime.value):
                        s.conflict = e["title"]

        # 7. gate
        gate = self._gate(subs)
        top = Intent.MULTI if len(subs) > 1 else subs[0].intent

        subs = [s for s in subs if s.intent != Intent.QUERY] or subs
        if gate == Gate.AUTO_OK:
            actions = [self._do(s, run_id) for s in subs]
            rb = readback_for(actions, subs)
            return self._finish(Trace(run_id, transcript, cleaned, top, subs, gate, actions, rb, injection_detected=injection, corrections=corrections), pend, dropped)

        # CONFIRM_REQUIRED: record pending rows, run the AUTO_OK parts now, read back the question
        actions = []
        for s in subs:
            if self._needs_confirm(s):
                key = self._key(s)
                self.p.ledger_append({"ts": _now(), "run_id": run_id, "intent": s.intent.value, "app": _app(s), "op": _op(s),
                                      "idempotency_key": key, "status": "pending", "evidence": s.recipient.evidence,
                                      "recipient": s.recipient.value, "datetime": s.datetime.value, "message": s.message.value,
                                      "title": s.title.value, "conflict": s.conflict})
                actions.append(Action(_app(s), _op(s), key, Status.pending, evidence=s.recipient.evidence or s.datetime.evidence))
            else:
                actions.append(self._do(s, run_id))
        rb = readback_for(actions, subs, confirm=True)
        return self._finish(Trace(run_id, transcript, cleaned, top, subs, gate, actions, rb, injection_detected=injection, corrections=corrections), pend, dropped)

    def _first_clause_is_time(self, transcript: str) -> bool:
        from .parse import detect_intent, parse_when, split_clauses
        first = split_clauses(transcript)[0]
        if detect_intent(first) is not None:
            return False
        when, _ = parse_when(re.sub(r"\b(works|is fine|is good)\b", "", first, flags=re.I), self.today)
        return bool(when.value)

    # ------------------------------------------------------------ clarification context
    def _remember_question(self, cleaned: str, subs):
        asker = next((s for s in subs if s.question), None)
        if not asker:
            return
        q = asker.question.lower()
        kind = "who" if ("which one" in q or "don't have a contact" in q or "who should" in q) else \
               "message" if ("message say" in q or "what should i say to" in q) else \
               "title" if ("call that" in q or "call it" in q) else \
               "similar" if "is this the same one" in q else "when"
        mention = None
        if kind == "who":
            m = re.search(r"called (\w[\w .]*?)\.", asker.question) or None
            mention = m.group(1) if m else None
        self._ctx = {"text": cleaned, "kind": kind, "mention": mention, "asker": asker, "subs": subs}

    def _merge_answer(self, answer: str) -> str | None:
        ctx = self._ctx
        text, kind = ctx["text"], ctx["kind"]
        a = answer.strip(" .")
        if kind == "who":
            # replace the ambiguous mention with the answer; fall back to appending "to <answer>"
            asker = ctx["asker"]
            cands, _ = self.contacts.resolve(a)
            if len(cands) != 1:
                return None
            name = cands[0]["name"]
            for alias in ("Patel", "Jon", "John", "Joan"):
                pass
            # find the mention the parser stumbled on: the word(s) after the verb
            m = re.search(r"\b(reply to|respond to|email|mail|tell|text|message|notify|ping|send (?:it )?to|let)\s+((?:dr\.?\s+)?\w+)", text, re.I)
            if m:
                return text[:m.start(2)] + name + text[m.end(2):]
            return f"{text} to {name}"
        if kind == "when":
            if not re.search(r"\bat\b|\d", a):
                a = "at " + a
            clause = ctx["asker"].clause
            if clause and clause in text:
                return text.replace(clause, f"{clause} {a}", 1)
            return f"{text} {a}"
        if kind == "message":
            return f"{text} saying {a}"
        return None

    # ------------------------------------------------------------ execute after "yes"
    def _execute_pending(self, run_id, transcript, pend) -> Trace:
        actions, subs = [], []
        # include the sibling actions that already ran in the originating turn, so the
        # read-back after "yes" summarises the whole request
        origin_runs = {r.get("run_id") for r in pend}
        for r in self.p.ledger_rows():
            if r.get("run_id") in origin_runs and r.get("status") in ("done", "failed", "not_attempted"):
                s = SubIntent(intent=Intent(r["intent"]))
                s.recipient = Slot(r.get("recipient"), Source.deterministic if r.get("recipient") else Source.none, 0.9)
                s.title = Slot(r.get("title"), Source.deterministic if r.get("title") else Source.none, 0.9)
                subs.append(s)
                actions.append(Action(r["app"], r["op"], r["idempotency_key"], Status(r["status"]), detail=r.get("readback") or r.get("evidence", ""), link=r.get("link") or ""))
        for r in pend:
            s = SubIntent(intent=Intent(r["intent"]))
            s.recipient = Slot(r.get("recipient"), Source.deterministic if r.get("recipient") else Source.none, 0.9, r.get("evidence", ""))
            s.datetime = Slot(r.get("datetime"), Source.deterministic if r.get("datetime") else Source.none, 0.9)
            s.message = Slot(r.get("message"), Source.deterministic if r.get("message") else Source.none, 0.9)
            s.title = Slot(r.get("title"), Source.deterministic if r.get("title") else Source.none, 0.9)
            s.conflict = r.get("conflict")
            subs.append(s)
            actions.append(self._do(s, run_id, key=r["idempotency_key"]))
        order = {"gmail": 0, "calendar": 1, "slack": 2}
        pairs = sorted(zip(actions, subs), key=lambda p: order.get(p[0].app, 9))
        actions, subs = [p[0] for p in pairs], [p[1] for p in pairs]
        rb = readback_for(actions, subs)
        tr = Trace(run_id, transcript, transcript, subs[0].intent if len(subs) == 1 else Intent.MULTI, subs, Gate.CONFIRM_REQUIRED, actions, rb)
        self.traces.append(tr); return tr

    # ------------------------------------------------------------ do one action with idempotency + retry
    def _do(self, s: SubIntent, run_id: str, key: str | None = None) -> Action:
        key = key or self._key(s)
        app, op = _app(s), _op(s)
        if s.intent in (Intent.QUERY, Intent.CLARIFY, Intent.REFUSE):
            return Action(app, op, key, Status.not_attempted, detail="that part was a question, not an action")
        dup = key in self._completed_keys()
        if dup and s.intent == Intent.CREATE_EVENT:
            prior = next((r for r in reversed(self.p.ledger_rows()) if r.get("idempotency_key") == key and r.get("status") == "done"), None)
            try:
                still_there = bool(prior and prior.get("event_id") and any(e["id"] == prior["event_id"] for e in self.p.calendar_upcoming()))
            except Exception:  # noqa: BLE001 — if we can't check, trust the ledger
                still_there = True
            if not still_there:
                dup = False   # it was removed by hand since — creating it again is what was asked
        if dup:
            a = Action(app, op, key, Status.skipped_duplicate, detail="already sent")
            self.p.ledger_append({"ts": _now(), "run_id": run_id, "intent": s.intent.value, "app": app, "op": op, "idempotency_key": key,
                                  "status": "skipped_duplicate", "evidence": "same key already done"})
            return a
        t0 = time.time()
        last_err = None
        for attempt in range(1 + 2):
            try:
                detail = self._call(s)
                link = (self.p.calls[-1].get("link", "") if getattr(self.p, "calls", None) else "")
                a = Action(app, op, key, Status.done, detail=detail, latency_ms=int((time.time() - t0) * 1000),
                           evidence=s.recipient.evidence or s.datetime.evidence, link=link)
                self.p.ledger_append({"ts": _now(), "run_id": run_id, "intent": s.intent.value, "app": app, "op": op, "idempotency_key": key,
                                      "status": "done", "evidence": a.evidence, "readback": detail,
                                      "recipient": s.recipient.value, "title": s.title.value, "datetime": s.datetime.value,
                                      "event_id": s.clause if s.intent == Intent.CREATE_EVENT else None, "link": link})
                return a
            except ProviderError as e:
                last_err = e
                if e.code not in RETRIES or attempt >= RETRIES[e.code]:
                    break
                time.sleep(0.01)
        why = {404: "I couldn't find that", 429: "the service is rate-limiting me", 400: "the request was rejected"}.get(
            last_err.code if last_err else 0, f"{app} returned an error")
        extra = str(last_err).split(" ", 2)[2] if last_err and len(str(last_err).split(" ", 2)) > 2 else ""
        a = Action(app, op, key, Status.failed, detail=f"{extra or why} — nothing was changed", latency_ms=int((time.time() - t0) * 1000))
        self.p.ledger_append({"ts": _now(), "run_id": run_id, "intent": s.intent.value, "app": app, "op": op, "idempotency_key": key,
                              "status": "failed", "evidence": a.detail, "recipient": s.recipient.value, "title": s.title.value})
        return a

    def _call(self, s: SubIntent) -> str:
        row = self._contact(s.recipient.value) if s.recipient.value else None
        if s.intent in (Intent.NOTIFY, Intent.SEND_EMAIL) and not (s.message.value or "").strip():
            raise ProviderError("slack" if s.intent == Intent.NOTIFY else "gmail", 400, "there was no message to send, so I didn't")
        if s.intent == Intent.REPLY_EMAIL:
            th = self.p.gmail_find_thread(row["email"])
            body = s.message.value or (f"{speak_when(s.datetime.value)} works for me." if s.datetime.value else "Confirmed.")
            if th:
                self.p.gmail_reply(th["id"], row["email"], body)
            else:
                self.p.gmail_send(row["email"], "Re: your message", body)
            return f"Sent reply to {row['name']}: “{body}”"
        if s.intent == Intent.SEND_EMAIL:
            self.p.gmail_send(row["email"], (s.message.value or "Message")[:60], s.message.value or "")
            return f"Emailed {row['name']}: “{s.message.value}”"
        if s.intent == Intent.NOTIFY:
            if not row.get("slack"):
                raise ProviderError("slack", 404, "no channel on file")
            user = os.getenv("ECHO_USER_NAME", "Kashaf")
            self.p.slack_post(row["slack"], f"*{user}:* {s.message.value or ''}  _(sent by voice via Occuris Echo)_")
            return f"Told {row['name'].split(' (')[0]} on Slack: “{s.message.value}”"
        if s.intent == Intent.CREATE_EVENT:
            eid = self.p.calendar_create(s.title.value or "Event", s.datetime.value)
            s.clause = eid  # remembered in the ledger row for a later rename
            return f"Added “{s.title.value or 'Event'}” to your calendar, {speak_when(s.datetime.value)}"
        if s.intent == Intent.RENAME_EVENT:
            self.p.calendar_rename(s.clause, s.title.value)
            return f"Renamed “{s.recipient.value}” to “{s.title.value}”, {speak_when(s.datetime.value)}"
        if s.intent == Intent.UPDATE_EVENT:
            if s.recipient.evidence == "existing event id":
                eid = s.recipient.value
                ev = next((e for e in self.p.calendar_upcoming() if e["id"] == eid), None)
                if not ev:
                    raise ProviderError("calendar", 404, "event not found")
                changed = []
                if s.datetime.value and s.datetime.value[:16] != ev["start"][:16]:
                    self.p.calendar_update(eid, s.datetime.value); changed.append(f"time to {speak_when(s.datetime.value)}")
                if s.title.value and s.title.value.lower() != ev["title"].lower() and len(s.title.value) >= len(ev["title"]):
                    self.p.calendar_rename(eid, s.title.value); changed.append(f"name to “{s.title.value}”")
                return f"Updated “{ev['title']}” — " + (", ".join(changed) if changed else "nothing needed changing") + ". No duplicate added"
            ev = self.p.calendar_find(s.title.value or "")
            if not ev:
                raise ProviderError("calendar", 404, f"no event called “{s.title.value or '?'}” on your calendar")
            self.p.calendar_update(ev["id"], s.datetime.value)
            return f"Moved “{ev['title']}” to {speak_when(s.datetime.value)}"
        raise ProviderError(app, 400, "unsupported action")

    # ------------------------------------------------------------ helpers
    def _gate(self, subs) -> Gate:
        return Gate.CONFIRM_REQUIRED if any(self._needs_confirm(s) for s in subs) else Gate.AUTO_OK

    def _needs_confirm(self, s: SubIntent) -> bool:
        if s.intent in (Intent.REPLY_EMAIL, Intent.SEND_EMAIL):
            return not self._trusted(s)
        if s.intent == Intent.NOTIFY:
            return not self._trusted(s)
        if s.intent == Intent.UPDATE_EVENT:
            return s.recipient.evidence != "existing event id"  # "same" answer is the confirmation
        if s.intent == Intent.CREATE_EVENT:
            return bool(s.conflict)
        return False

    def _similar_event(self, title: str, start_iso: str) -> dict | None:
        """An existing event with a similar name within a week either side — the user may have forgotten it."""
        try:
            target = datetime.fromisoformat(start_iso)
        except ValueError:
            return None
        for e in self.p.calendar_upcoming(14):
            try:
                st = datetime.fromisoformat(e["start"])
            except (ValueError, TypeError):
                continue
            if (e.get("title") or "").lower().startswith("cancelled"):
                continue
            if abs((st - target).days) <= 7 and _similar_titles(e.get("title") or "", title):
                if st == target and (e.get("title") or "").lower() == title.lower():
                    continue  # exact duplicate: idempotency handles it
                return e
        return None

    def _trusted(self, s: SubIntent) -> bool:
        row = self._contact(s.recipient.value) if s.recipient.value else None
        return bool(row and str(row.get("trusted", "")).lower() == "yes")

    def _contact(self, name):
        for r in self.p.contacts():
            if r["name"] == name:
                return r
        return None

    def _key(self, s: SubIntent) -> str:
        if s.intent == Intent.RENAME_EVENT:
            return idempotency_key(s.intent, s.clause, s.datetime.value, s.title.value)  # event id + new name
        return idempotency_key(s.intent, s.recipient.value or s.title.value, s.datetime.value, s.message.value)

    def _answer_query(self, s: SubIntent, transcript: str) -> str:
        t = transcript.lower()
        if "did i" in t or "have i" in t:
            done = [r for r in self.p.ledger_rows() if r.get("status") == "done"]
            if not done:
                return "No — I haven't sent anything yet."
            return "Yes — " + "; ".join(r.get("readback", r.get("op", "")) for r in done[-3:])
        if re.search(r"\b(need to do|to ?do|what do i (have|need) to|anything (i )?(should|need)|my inbox|emails?)\b", t):
            return self._week_digest()
        day = (s.datetime.value or self.today.isoformat())[:10]
        day_words = speak_when(day + "T00:00", day_only=True)
        events = []
        for e in self.p.calendar_list(day):
            try:
                st, en = datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"])
                events.append((st, en, e["title"]))
            except Exception:  # noqa: BLE001 — malformed event must not crash the answer
                continue
        events.sort()
        # whole week?
        if re.search(r"\b(this week|whole week|the week|any day|which day|what day)\b", t):
            return self._week_availability(t)
        # free-slot question?
        if re.search(r"\b(free|slots?|available|open|when can i|any time|good time)\b", t):
            return self._free_slots(day, day_words, events, t)
        if not events:
            return f"Nothing on your calendar {day_words} — it's clear."
        listed = "; ".join(f"{title} at {speak_when(st.isoformat(), time_only=True)}" for st, en, title in events)
        return f"On {day_words} you have: {listed}."

    EVENING = re.compile(r"\b(concert|dinner|drinks?|movie|film|show|party|gig|theatre|theater|night|evening|late)\b")
    DAYTIME = re.compile(r"\b(coffee|brunch|breakfast|lunch|walk|gym|run|appointment|meeting|errand|shopping|morning|afternoon|daytime|class)\b")

    def _week_availability(self, t: str) -> str:
        """Busy/free summary for the next 7 days, one short clause per day."""
        from datetime import timedelta
        out = []
        for i in range(7):
            d = (self.today + timedelta(days=i)).date().isoformat()
            evs = []
            for e in self.p.calendar_list(d):
                try:
                    evs.append((datetime.fromisoformat(e["start"]), e["title"]))
                except Exception:  # noqa: BLE001
                    continue
            name = datetime.fromisoformat(d + "T00:00").strftime("%A")
            want_slots = re.search(r"\b(time|times|slots?|when)\b", t)
            if not evs:
                out.append(f"{name} is clear" + (" all day" if want_slots else ""))
            elif want_slots:
                busy = ", ".join(f"{ti} at {speak_when(st.isoformat(), time_only=True)}" for st, ti in sorted(evs))
                out.append(f"{name}: free except {busy}")
            else:
                out.append(f"{name} has " + ", ".join(f"{ti} at {speak_when(st.isoformat(), time_only=True)}" for st, ti in sorted(evs)))
        lead = "This week: " + "; ".join(out) + "."
        if re.search(r"\b(lunch|coffee|brunch|breakfast)\b", t):
            lead += " For lunch, the clear days around midday are the easy ones — say a day and time and I'll add it."
        return lead

    def _free_slots(self, day: str, day_words: str, events, t: str) -> str:
        """Free windows of an hour or more between 8am and 11pm, led by the half of the day that suits the activity."""
        from datetime import timedelta
        d0 = datetime.fromisoformat(day + "T08:00")
        d1 = datetime.fromisoformat(day + "T23:00")
        busy = [(max(st, d0), min(en, d1)) for st, en, _ in events if en > d0 and st < d1]
        free, cursor = [], d0
        for st, en in sorted(busy):
            if st - cursor >= timedelta(hours=1):
                free.append((cursor, st))
            cursor = max(cursor, en)
        if d1 - cursor >= timedelta(hours=1):
            free.append((cursor, d1))
        if not free:
            return f"{day_words} is fully booked between 8 am and 11 pm — nothing an hour long is free."
        split = datetime.fromisoformat(day + "T17:00")
        daytime = [(a, min(b, split)) for a, b in free if a < split and min(b, split) - a >= timedelta(hours=1)]
        evening = [(max(a, split), b) for a, b in free if b > split and b - max(a, split) >= timedelta(hours=1)]
        say = lambda ws: ", ".join(f"{speak_when(a.isoformat(), time_only=True)} to {speak_when(b.isoformat(), time_only=True)}" for a, b in ws)
        activity = re.search(r"\bfor (?:a |an |the )?([a-z]+)", t)
        act = activity.group(1) if activity else None
        if self.EVENING.search(t):
            lead, other, lead_name, other_name = evening, daytime, "in the evening", "during the day"
        elif self.DAYTIME.search(t):
            lead, other, lead_name, other_name = daytime, evening, "during the day", "in the evening"
        else:
            allw = say(free)
            return f"{day_words}, you're free {allw}."
        what = f" for {act}" if act else ""
        if lead:
            out = f"{day_words}{what}, you're free {lead_name}: {say(lead)}."
            if other:
                out += f" If you'd rather go {other_name}, there's also {say(other)}."
            return out
        if other:
            return f"{day_words}, nothing {lead_name} is free{what}, but {other_name} you have {say(other)}."
        return f"{day_words} has no free hour {lead_name} or {other_name}."

    def _week_digest(self) -> str:
        items, flagged = [], []
        for m in self.p.gmail_inbox():
            body, subj, frm = m.get("body", ""), m.get("subject", ""), m.get("from", "")
            text = f"{subj} {body}"
            if re.search(r"(click here|verify|confirm your account|suspended)", text, re.I) and re.search(r"http|bit\.ly", text, re.I):
                flagged.append(f"“{subj}” from {frm.split('<')[0].strip()} looks suspicious — I'd leave it")
                continue
            if re.search(r"newsletter|this week in|top stories|unsubscribe", text, re.I):
                continue
            if re.search(r"resched", text, re.I):
                items.append(f"“{subj}”: your appointment was rescheduled — check the new time before I change the calendar")
                continue
            dm = re.search(r"\b(\d{1,2})/(\d{1,2})\b", text)
            if dm and _date_unclear(int(dm.group(1)), int(dm.group(2))):
                items.append(f"“{subj}”: date unclear in the email — check it yourself")
                continue
            if re.search(r"\b(due|deadline|by \d|appointment|confirmed for)\b", text, re.I):
                when = re.search(r"\b(\d{1,2} \w+|\w+day \d{1,2} \w+)\b", text)
                items.append(f"“{subj}” — {when.group(1) if when else 'has a date'}")
        parts = []
        if items: parts.append("This week: " + "; ".join(items) + ".")
        if flagged: parts.append(" ".join(flagged) + ".")
        if not parts: return "Nothing with a deadline or appointment in your inbox this week."
        return " ".join(parts) + (" Want me to put the dated ones on your calendar?" if items else "")

    def _ablate(self, subs):
        """Ablation: drop the contacts-resolution evidence and guess the first candidate — what a naive agent does."""
        for s in subs:
            if s.question and "Which one" in s.question:
                names = re.findall(r"—\s*(.+?)\?", s.question)
                first = names[0].split(" or ")[0] if names else None
                if first:
                    s.recipient = Slot(first, Source.model, 0.5, "ablation: picked first candidate")
                    s.question = None

    def _finish(self, tr: Trace, prior_pending, dropped: str | None = None) -> Trace:
        fq = getattr(self, "_followup_q", None)
        if fq:
            self._followup_q = None
            try:
                from .models import SubIntent as _SI
                from .parse import parse_when as _pw
                qs = _SI(intent=Intent.QUERY); qs.datetime = _pw(fq, self.today)[0]
                tr.readback = tr.readback.rstrip(".") + ". " + self._answer_query(qs, fq)
            except Exception:  # noqa: BLE001 — never let a follow-up break the main answer
                pass
        note = getattr(self, "_note", None)
        if note:
            tr.readback = note + tr.readback; self._note = None
        if dropped:
            tr.readback = tr.readback.rstrip(".") + f". (I dropped the earlier unfinished request about {dropped}.)"
        if prior_pending and tr.intent != Intent.CLARIFY:
            still = "; ".join(f"the {r['app']} {r['op']} to {r.get('recipient') or r.get('title')}" for r in prior_pending)
            tr.readback = tr.readback.rstrip(".") + f". Also, I'm still waiting on your yes for {still} — send it?"
        self.traces.append(tr)
        return tr


def _similar_titles(a: str, b: str) -> bool:
    from difflib import SequenceMatcher
    a, b = a.lower().strip(), b.lower().strip()
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.72


def _clean_title_answer(text: str) -> str:
    """'Call it spade. Spa day' → 'spa day'. Last version wins; leading 'call it' etc. stripped."""
    parts = [p.strip() for p in re.split(r"[.,;]|\b(?:actually|i mean|sorry|no wait|no)\b", text, flags=re.I) if p and p.strip()]
    t = parts[-1] if parts else text
    t = re.sub(r"^(?:just\s+)?(?:call it|call at|call the|name it|title it|make it|it'?s|its|put|just)\s+", "", t, flags=re.I).strip(" .")
    t = re.sub(r"^(?:a|an|the)\s+", "", t, flags=re.I).strip(" .")
    return t or text.strip(" .")


def _short(name: str) -> str:
    return (name or "").split(" (")[0]


def _date_unclear(a: int, b: int) -> bool:
    """dd/mm that cannot be a real date, or is ambiguous between dd/mm and mm/dd."""
    if a > 31 or b > 31 or (a > 12 and b > 12):
        return True
    if b == 2 and a > 29:
        return True
    if b in (4, 6, 9, 11) and a == 31:
        return True
    return a <= 12 and b <= 12 and a != b


def _overlaps(e, start_iso) -> bool:
    try:
        s = datetime.fromisoformat(start_iso)
        a, b = datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"])
        return a <= s < b
    except Exception:  # noqa: BLE001 — malformed event
        return False


def _app(s: SubIntent) -> str:
    return {Intent.REPLY_EMAIL: "gmail", Intent.SEND_EMAIL: "gmail", Intent.NOTIFY: "slack",
            Intent.CREATE_EVENT: "calendar", Intent.UPDATE_EVENT: "calendar", Intent.RENAME_EVENT: "calendar"}.get(s.intent, "agent")


def _op(s: SubIntent) -> str:
    return {Intent.REPLY_EMAIL: "reply", Intent.SEND_EMAIL: "send", Intent.NOTIFY: "post",
            Intent.CREATE_EVENT: "create", Intent.UPDATE_EVENT: "update", Intent.RENAME_EVENT: "rename"}.get(s.intent, "noop")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
