"""The agent loop: parse → gate → (confirm) → act → read back. Every run emits a Trace.

Pending state is DERIVED from the ledger (requested − completed), never stored as a flag."""
from __future__ import annotations

import re
import time
import uuid
from datetime import datetime

from .contacts import Contacts
from .models import Action, Gate, Intent, Slot, Source, Status, SubIntent, Trace, idempotency_key
from .parse import INJECTION, parse
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
        if pend and NO.match(transcript):
            for r in pend:
                self.p.ledger_append({**r, "status": "cancelled", "ts": _now()})
            tr = Trace(run_id, transcript, transcript, Intent.REFUSE, [], Gate.AUTO_OK, [], "Cancelled. Nothing was sent.")
            self.traces.append(tr); return tr

        # 2. parse
        subs, meta = parse(transcript, self.contacts, self.today, ablate=self.ablate)
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
        if not subs:
            # optional model fallback would go here; without it, ask.
            return self._finish(Trace(run_id, transcript, cleaned, Intent.CLARIFY, [], Gate.AUTO_OK, [],
                                      "I'm not sure what you'd like me to do. Could you say it another way?",
                                      question="I'm not sure what you'd like me to do — could you say it another way?",
                                      corrections=corrections), pend)

        # 3. ablation: throw away deterministic slot evidence for names → forces guessing
        if self.ablate:
            self._ablate(subs)

        # 4. queries
        if all(s.intent == Intent.QUERY for s in subs):
            rb = self._answer_query(subs[0], transcript)
            return self._finish(Trace(run_id, transcript, cleaned, Intent.QUERY, subs, Gate.AUTO_OK, [], rb, corrections=corrections), pend)

        # share a message across NOTIFY siblings ("tell A and B I'll be late")
        msgs = [s.message.value for s in subs if s.message.value]
        whens = [s.datetime.value for s in subs if s.datetime.value]
        for s in subs:
            if s.intent == Intent.NOTIFY and not s.message.value:
                if msgs:
                    s.message = Slot(msgs[-1], Source.deterministic, 0.7, "shared with sibling clause")
                elif whens:
                    s.message = Slot(f"{speak_when(whens[-1])} works", Source.deterministic, 0.6, "from shared time")
        # UPDATE_EVENT with a time but no day: keep the event's own date
        for s in subs:
            if s.intent == Intent.UPDATE_EVENT and s.datetime.value and "no-day" in (s.datetime.evidence or ""):
                ev = self.p.calendar_find(s.title.value or "")
                if ev:
                    s.datetime = Slot(ev["start"][:10] + s.datetime.value[10:], Source.deterministic, 0.85, "time from utterance, date from existing event")
        # a resolved NOTIFY contact with no channel on file needs a question, not a failed call
        for s in subs:
            if s.intent == Intent.NOTIFY and s.recipient.value and not s.question:
                row = self._contact(s.recipient.value)
                if row is not None and not row.get("slack") and not row.get("email"):
                    s.question = f"How should I reach {_short(s.recipient.value)}? I don't have a channel on file."
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

        if gate == Gate.AUTO_OK:
            actions = [self._do(s, run_id) for s in subs]
            rb = readback_for(actions, subs)
            return self._finish(Trace(run_id, transcript, cleaned, top, subs, gate, actions, rb, injection_detected=injection, corrections=corrections), pend)

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
        return self._finish(Trace(run_id, transcript, cleaned, top, subs, gate, actions, rb, injection_detected=injection, corrections=corrections), pend)

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
                actions.append(Action(r["app"], r["op"], r["idempotency_key"], Status(r["status"]), detail=r.get("readback") or r.get("evidence", "")))
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
        if key in self._completed_keys():
            a = Action(app, op, key, Status.skipped_duplicate, detail="already sent")
            self.p.ledger_append({"ts": _now(), "run_id": run_id, "intent": s.intent.value, "app": app, "op": op, "idempotency_key": key,
                                  "status": "skipped_duplicate", "evidence": "same key already done"})
            return a
        t0 = time.time()
        last_err = None
        for attempt in range(1 + 2):
            try:
                detail = self._call(s)
                a = Action(app, op, key, Status.done, detail=detail, latency_ms=int((time.time() - t0) * 1000), evidence=s.recipient.evidence or s.datetime.evidence)
                self.p.ledger_append({"ts": _now(), "run_id": run_id, "intent": s.intent.value, "app": app, "op": op, "idempotency_key": key,
                                      "status": "done", "evidence": a.evidence, "readback": detail,
                                      "recipient": s.recipient.value, "title": s.title.value})
                return a
            except ProviderError as e:
                last_err = e
                if e.code not in RETRIES or attempt >= RETRIES[e.code]:
                    break
                time.sleep(0.01)
        a = Action(app, op, key, Status.failed, detail=f"{app} error {last_err.code if last_err else '?'}", latency_ms=int((time.time() - t0) * 1000))
        self.p.ledger_append({"ts": _now(), "run_id": run_id, "intent": s.intent.value, "app": app, "op": op, "idempotency_key": key,
                              "status": "failed", "evidence": a.detail, "recipient": s.recipient.value, "title": s.title.value})
        return a

    def _call(self, s: SubIntent) -> str:
        row = self._contact(s.recipient.value) if s.recipient.value else None
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
            self.p.slack_post(row["slack"], s.message.value or "")
            return f"Told {row['name'].split(' (')[0]} on Slack: “{s.message.value}”"
        if s.intent == Intent.CREATE_EVENT:
            self.p.calendar_create(s.title.value or "Event", s.datetime.value)
            return f"Added “{s.title.value or 'Event'}” to your calendar, {speak_when(s.datetime.value)}"
        if s.intent == Intent.UPDATE_EVENT:
            ev = self.p.calendar_find(s.title.value or "")
            if not ev:
                raise ProviderError("calendar", 404, "event not found")
            self.p.calendar_update(ev["id"], s.datetime.value)
            return f"Moved “{ev['title']}” to {speak_when(s.datetime.value)}"
        raise ProviderError("agent", 400, "unknown intent")

    # ------------------------------------------------------------ helpers
    def _gate(self, subs) -> Gate:
        return Gate.CONFIRM_REQUIRED if any(self._needs_confirm(s) for s in subs) else Gate.AUTO_OK

    def _needs_confirm(self, s: SubIntent) -> bool:
        if s.intent in (Intent.REPLY_EMAIL, Intent.SEND_EMAIL):
            return not self._trusted(s)
        if s.intent == Intent.NOTIFY:
            return not self._trusted(s)
        if s.intent == Intent.UPDATE_EVENT:
            return True
        if s.intent == Intent.CREATE_EVENT:
            return bool(s.conflict)
        return False

    def _trusted(self, s: SubIntent) -> bool:
        row = self._contact(s.recipient.value) if s.recipient.value else None
        return bool(row and str(row.get("trusted", "")).lower() == "yes")

    def _contact(self, name):
        for r in self.p.contacts():
            if r["name"] == name:
                return r
        return None

    def _key(self, s: SubIntent) -> str:
        return idempotency_key(s.intent, s.recipient.value or s.title.value, s.datetime.value, s.message.value)

    def _answer_query(self, s: SubIntent, transcript: str) -> str:
        t = transcript.lower()
        if "did i" in t or "have i" in t:
            done = [r for r in self.p.ledger_rows() if r.get("status") == "done"]
            if not done:
                return "No — I haven't sent anything yet."
            return "Yes — " + "; ".join(r.get("readback", r.get("op", "")) for r in done[-3:])
        if "this week" in t or "need to do" in t:
            return self._week_digest()
        day = (s.datetime.value or self.today.isoformat())[:10]
        evs = []
        for e in self.p.calendar_list(day):
            try:
                evs.append(f"{e['title']} at {speak_when(e['start'], time_only=True)}")
            except Exception:  # noqa: BLE001 — malformed event must not crash the answer
                continue
        if not evs:
            return f"Nothing on your calendar {speak_when(day + 'T00:00', day_only=True)}."
        return f"On {speak_when(day + 'T00:00', day_only=True)} you have: " + "; ".join(evs) + "."

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

    def _finish(self, tr: Trace, prior_pending) -> Trace:
        if prior_pending and tr.intent != Intent.CLARIFY:
            still = "; ".join(f"the {r['app']} {r['op']} to {r.get('recipient') or r.get('title')}" for r in prior_pending)
            tr.readback = tr.readback.rstrip(".") + f". Also, I'm still waiting on your yes for {still} — send it?"
        self.traces.append(tr)
        return tr


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
            Intent.CREATE_EVENT: "calendar", Intent.UPDATE_EVENT: "calendar"}.get(s.intent, "agent")


def _op(s: SubIntent) -> str:
    return {Intent.REPLY_EMAIL: "reply", Intent.SEND_EMAIL: "send", Intent.NOTIFY: "post",
            Intent.CREATE_EVENT: "create", Intent.UPDATE_EVENT: "update"}.get(s.intent, "noop")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
