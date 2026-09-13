"""Deterministic parser. Rules first; the model is only consulted for what rules can't settle.

Pipeline: clean (disfluencies, corrections) → detect refusal/garbage → split into clauses →
per clause: intent, recipient, datetime, message/title → return SubIntents (+ clarify questions)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

import dateparser

from .contacts import Contacts
from .models import Intent, Slot, Source, SubIntent

DISFLUENCIES = r"\b(umm+|uh+|um+|er+|erm+|hmm+|like|you know|basically|just|kind of|sort of)\b"
CORRECTION = re.compile(r"\b(not\s+\S+\s*)?(actually|i mean|sorry|no wait|scratch that|correction)\b", re.I)
RETRACTION = re.compile(r"\b(never\s*mind|nevermind|forget it|cancel that|scratch that|don'?t bother)\b", re.I)
GARBAGE = re.compile(r"\[(static|unintelligible|inaudible|noise)\]", re.I)
SENSITIVE = re.compile(r"\b(password|passcode|pin|otp|cvv|card number|social security|ssn)\b", re.I)
INJECTION = re.compile(r"(ignore (all |any )?(prior|previous) instructions|forward (this )?(thread|email) to all|ai assistant:|system:)", re.I)

CLAUSE_SPLIT = re.compile(r"(?<![Dd]r)(?<![Mm]r)(?<![Mm]s)(?<![Mm]rs)(?<![Ss]t)\.\s+|,\s*(?:and\s+)?|\s+and then\s+|\s+then\s+|\s+and\s+(?=(?:tell|put|add|email|reply|send|move|remind|text|message|notify|at\s+\d|my\s+(?:sister|brother)|(?:on|for)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow)|tomorrow|next\s+week))", re.I)


def strip_disfluencies(text: str) -> str:
    t = re.sub(DISFLUENCIES, " ", text, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" ,.")
    return t


def apply_corrections(text: str) -> tuple[str, list[str]]:
    """'at 7 not 7 actually at 2' → 'at 2'. Last value wins after a correction marker."""
    notes = []
    # pattern: <X> not <X> actually <Y>  |  <X> actually <Y>  |  <X> I mean <Y>
    pat = re.compile(r"(?:at\s+)?(?:around\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+(?:not\s+\S+\s+)?(?:actually|i mean|sorry|no wait)\s+(?:at\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", re.I)
    def repl(m):
        notes.append(f"corrected '{m.group(1).strip()}' → '{m.group(2).strip()}'")
        return f"at {m.group(2)}"
    t = pat.sub(repl, text)
    return t, notes


def has_retraction(text: str) -> bool:
    return bool(RETRACTION.search(text))


def is_garbage(text: str) -> bool:
    words = [w for w in re.sub(GARBAGE, " ", text).split() if w.isalpha()]
    return bool(GARBAGE.search(text)) and len(words) < 4


# ---------------------------------------------------------------- datetime
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}


def parse_when(text: str, today: datetime) -> tuple[Slot, bool]:
    """Returns (slot, ambiguous_next_weekday). 'next Thursday' said on a Thursday is ambiguous."""
    t = text.lower()
    # word numbers after "at": "at ten", "at two o'clock"
    t = re.sub(r"\bat\s+(" + "|".join(WORD_NUM) + r")\b", lambda m: "at " + str(WORD_NUM[m.group(1)]), t)
    t = re.sub(r"\b(" + "|".join(WORD_NUM) + r")\s+o'?clock\b", lambda m: str(WORD_NUM[m.group(1)]), t)
    t = re.sub(r"\bo'?clock\b", "", t)
    if re.search(r"\bmorning\b", t) and not re.search(r"\b(am|pm)\b", t): t += " am"
    if re.search(r"\b(afternoon|evening|tonight)\b", t) and not re.search(r"\b(am|pm)\b", t): t += " pm"
    ambiguous = False
    m = re.search(r"\bnext\s+(" + "|".join(WEEKDAYS) + r")\b", t)
    if m and WEEKDAYS[today.weekday()] == m.group(1):
        ambiguous = True
    # time
    tm = re.search(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b(?!\s*(?:minutes|mins|min))", t)
    hour = minute = None
    if tm:
        hour = int(tm.group(1)); minute = int(tm.group(2) or 0)
        ap = (tm.group(3) or "").replace(".", "")
        if not ap and t.rstrip().endswith((" am", " pm")):
            ap = t.rstrip()[-2:]
        if ap == "pm" and hour < 12: hour += 12
        if ap == "am" and hour == 12: hour = 0
        if not ap and 1 <= hour <= 7: hour += 12  # "at 2" → 14:00 (working-hours heuristic, tagged below)
    if "noon" in t: hour, minute = 12, 0
    # day
    day = None
    if "tomorrow" in t: day = today + timedelta(days=1)
    elif "today" in t or "tonight" in t: day = today
    else:
        wd = re.search(r"\b(" + "|".join(WEEKDAYS) + r")\b", t)
        if wd:
            target = WEEKDAYS.index(wd.group(1))
            delta = (target - today.weekday()) % 7
            if delta == 0 and not m: delta = 0
            if m: delta = 7 if delta == 0 else delta
            day = today + timedelta(days=delta)
    no_day = False
    if day is None and hour is not None:
        day = today; no_day = True  # time with no day ⇒ today, flagged
    if day is None:
        # last resort: dateparser
        dp = dateparser.parse(t, settings={"RELATIVE_BASE": today, "PREFER_DATES_FROM": "future"})
        if dp and re.search(r"\d|monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow", t):
            return Slot(dp.replace(second=0, microsecond=0).isoformat(timespec="minutes"), Source.deterministic, 0.6, "dateparser"), ambiguous
        return Slot(None, Source.none), ambiguous
    dt = day.replace(hour=hour or 0, minute=minute or 0, second=0, microsecond=0)
    conf = 0.95 if (hour is not None and tm and tm.group(3)) else 0.8
    ev = ("no-day; " if no_day else "") + ("no-hour; " if hour is None else "") + f"weekday/time rule on '{text[:40]}'"
    return Slot(dt.isoformat(timespec="minutes"), Source.deterministic, conf, ev), ambiguous


# ---------------------------------------------------------------- intents
RENAME = re.compile(r"(?:it'?s\s+)?not\s+(?:a\s+|an\s+|the\s+)?([a-z][a-z ]*?),?\s+(?:it'?s\s+)?(?:supposed to be\s+|meant to be\s+|should be\s+)?(?:a\s+|an\s+|the\s+)?([a-z][a-z ]*?)(?:\s*$|[.,])|\b(?:call it|rename it to|name it|change (?:the )?(?:name|title) to)\s+(?:a\s+|an\s+|the\s+)?([a-z][a-z ]*?)\s*$|\brename\s+(?:the\s+)?([a-z][a-z ]*?)\s+to\s+(?:a\s+|an\s+|the\s+)?([a-z][a-z ]*?)\s*$", re.I)


EVENT_NOUNS = r"(lunch|dinner|brunch|breakfast|coffee|meeting|appointment|call|check-?up|dentist|doctor|physio|gym|class|interview|flight|train|party|concert|movie|date|visit|session|lesson|standup|review|catch-?up|luncheon|drinks|workout)"


def detect_intent(clause: str) -> Intent | None:
    c = clause.lower()
    if RENAME.search(c): return Intent.RENAME_EVENT
    polite = re.match(r"^\s*(?:so\s+)?(?:can|could|would|will)\s+you\s+(?:please\s+)?|^\s*please\s+", c)
    if polite:
        c = c[polite.end():]
    has_verb = re.search(r"\b(reply|respond|email|mail|send|tell|text|message|notify|ping|put|add|schedule|book|move|reschedule|change|rename|delete|remove|remind)\b", c)
    if re.search(r"\b(what'?s on|what is on|what do i (have|need)|do i have|did i|have i|is there|am i free|any slots?|any time|when am i free|what times?|available|tell me if|let me know if)\b", c) \
            or (c.rstrip().endswith("?") and not has_verb):
        return Intent.QUERY
    if re.search(r"\b(reply|respond|write back)\b", c): return Intent.REPLY_EMAIL
    if re.search(r"\b(email|mail)\b", c): return Intent.SEND_EMAIL
    if re.search(r"\bsend\b", c): return Intent.SEND_EMAIL
    if re.search(r"\b(delete|remove|cancel the|get rid of)\b", c): return Intent.UPDATE_EVENT  # handled as "no delete" in the agent
    if re.search(r"\b(move|reschedule|push|shift|change|edit|update)\b", c) and not re.search(r"\b(reply|email|tell)\b", c): return Intent.UPDATE_EVENT
    if re.search(r"\b(put|add|schedule|book|calendar|stick)\b", c) or re.search(r"\bi(?:'ve| have)? ?(?:got|have) (a|an|the)\b", c) or re.search(r"\bi need to go\b", c):
        return Intent.CREATE_EVENT
    if re.search(r"\b(tell|text|message|notify|let .* know|ping)\b", c): return Intent.NOTIFY
    if re.search(r"\b(remind)\b", c): return Intent.CREATE_EVENT
    if re.search(r"\b(want to|wanna|i'?d like to|let'?s|i need to)\s+(have|book|do|schedule|go for|grab|get)\b", c) and re.search(r"\b" + EVENT_NOUNS + r"\b", c):
        return Intent.CREATE_EVENT
    # "Lunch with Dr Patel Friday at 3" — an event noun plus a time, no verb: that is an event
    if re.search(r"\b" + EVENT_NOUNS + r"\b", c) and re.search(r"\d|monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|noon", c) \
            and not re.search(r"\b(reply|email|mail|tell|text|message|send|free|slots?|available|do i have|what)\b", c):
        return Intent.CREATE_EVENT
    return None


RECIP = re.compile(r"\b(?:reply to|respond to|email|mail|tell|text|message|notify|ping|send|let)\s+(?:(?:it|this|that|these|the details|the invite)\s+to\s+)?(?:to\s+)?((?:dr\.?\s+|doctor\s+)?[a-z][a-z.]*(?:\s+[a-z][a-z.]*)?)", re.I)
MSG_SAY = re.compile(r"\b(?:that|saying|say|:)\s+(.+)$", re.I)


def extract_recipient(clause: str, contacts: Contacts) -> tuple[Slot, list[dict], str]:
    m = RECIP.search(clause)
    if not m:
        return Slot(None, Source.none), [], "no recipient phrase"
    mention = re.sub(r"\s+(as well|as|too|also|please|about it|about this)\s*$", "", m.group(1), flags=re.I)
    rel = re.match(r"my\s+(sister|brother|mum|mom|dad|wife|husband|partner)\b", mention, re.I)
    if rel:
        mention = "my " + rel.group(1).lower()
    else:
        # trim trailing verbs/that-clauses: "Patel that Thursday" → "Patel"
        mention = re.split(r"\b(that|saying|say|about|i'?ll|i am|i'm|my|to)\b", mention, maxsplit=1)[0].strip(" .:")
    # try the full mention, then progressively drop trailing words ("Dr Patel know" → "Dr Patel")
    words = mention.split()
    tried = []
    for n in range(len(words), 0, -1):
        m_try = " ".join(words[:n])
        cands, ev = contacts.resolve(m_try)
        tried.append((m_try, cands, ev))
        if len(cands) == 1:
            return Slot(cands[0]["name"], Source.deterministic, 0.95, ev), cands, m_try
    for m_try, cands, ev in tried:
        if cands:
            return Slot(None, Source.none), cands, m_try
    return Slot(None, Source.none), [], mention


def extract_message(clause: str) -> Slot:
    m = MSG_SAY.search(clause)
    if not m:
        return Slot(None, Source.none)
    msg = m.group(1).strip(" .")
    msg = re.sub(r"^(that|saying|say)\s+", "", msg, flags=re.I)
    return Slot(msg, Source.deterministic, 0.9, "after say/that/colon")


VAGUE = {"something", "thing", "a thing", "an event", "event", "it", "that", "this", "them", "stuff", "an appointment", "appointment", "a meeting"}


def _title_or_none(t: str, ev: str) -> Slot:
    t = t.strip()
    if not t or t in VAGUE:
        return Slot(None, Source.none)
    return Slot(t, Source.deterministic, 0.85, ev)


def extract_title(clause: str) -> Slot:
    c = clause.lower()
    m = re.search(r"\b(?:put|add|schedule|book)\s+(?:the\s+|a\s+|an\s+)?(.+?)\s+(?:on|to|in)\s+(?:my\s+)?calendar", c)
    if m:
        r = _title_or_none(m.group(1), "put X on calendar")
        if r.value: return r
    m = re.search(r"\bi(?:'ve| have)? ?(?:got|have) (?:a|an|the)\s+([a-z ]+?)\s+(?:at|on|with|friday|monday|tuesday|wednesday|thursday|saturday|sunday|tomorrow)\b", c) or re.search(r"\bi(?:'ve| have)? ?(?:got|have) (?:a|an|the)\s+([a-z]+)", c)
    if m:
        r = _title_or_none(m.group(1), "I have a X")
        if r.value: return r
    m = re.search(r"\b(?:put|add|schedule|book)\s+(?:me\s+)?(?:up\s+)?(?:for\s+)?(?:a\s+|an\s+|the\s+)?([a-z][a-z ]*?)\s+(?:on|at|for|\Z)", c)
    if m and m.group(1).strip() != "me":
        r = _title_or_none(m.group(1), "book X")
        if r.value: return r
    m = re.search(r"\b(?:go for|go to)\s+(.+?)(?:\s+with\b|$)", c)
    if m: return Slot(m.group(1).strip(), Source.deterministic, 0.8, "go for X")
    m = re.search(r"\b(?:the\s+)([a-z]+(?:\s+[a-z]+)?)\s+(?:thing\s+|one\s+|appointment\s+|event\s+)?(?:you|that you|i)\s+(?:put|added|registered|booked|scheduled)\b", c)
    if m: return Slot(m.group(1).strip(), Source.deterministic, 0.85, "the X you put")
    m = re.search(r"\b(?:move|reschedule|change|edit|update|delete|remove|cancel)\s+(?:the\s+)?([a-z ]+?)\s+(?:to|on|from|for)\b", c)
    if m and m.group(1).strip() not in ("it", "that", "this", "the time", "time"):
        return Slot(m.group(1).strip(), Source.deterministic, 0.85, "verb X to")
    m = re.match(r"^\s*(?:also\s+)?(?:a\s+|an\s+|the\s+)?([a-z]+(?:\s+with\s+[a-z ]+?)?)\s+(?:at|on|from)\s+\d", c)
    if m: return Slot(m.group(1).strip(), Source.deterministic, 0.75, "noun before time")
    m = re.search(r"\b" + EVENT_NOUNS + r"(?:\s+with\s+(?:dr\.?\s+|doctor\s+)?[a-z]+(?:\s+[a-z]+)?)?", c)
    if m:
        t = re.sub(r"\b(?:at|on)\s.*$", "", m.group(0)).strip()
        return Slot(t, Source.deterministic, 0.75, "event noun")
    return Slot(None, Source.none)


def split_clauses(text: str) -> list[str]:
    m = re.search(r"\b(tell|text|message|notify)\s+((?:my\s+)?\w+)\s+and\s+((?:my\s+)?\w+)\s+(.+)$", text, re.I)
    if m:
        verb, a, b, msg = m.groups()
        text = text[:m.start()] + f"{verb} {a} {msg}, {verb} {b} {msg}"
    # ASR writes a period at every pause: "…with a doctor. Doctor. Patel. And, umm. I wanna…"
    raw = [p.strip(" ,.") for p in re.split(r"(?<=[a-z0-9])(?<![Dd]r)(?<![Mm]r)(?<![Mm]s)(?<![Mm]rs)(?<![Ss]t)\.\s+(?=[A-Za-z])", text) if p.strip(" ,.")]
    joined: list[str] = []
    for frag in raw:
        words = frag.split()
        tiny = len(words) <= 2 and not re.search(r"\d", frag) and not detect_intent(frag)
        if joined and (tiny or re.match(r"^(and|so|also|then)\b", frag, re.I)):
            joined[-1] = joined[-1] + " " + re.sub(r"^(and|so|also|then)\s+", "", frag, flags=re.I)
        else:
            joined.append(frag)
    text = ". ".join(joined)
    parts = [p.strip(" ,.") for p in CLAUSE_SPLIT.split(text) if p and p.strip(" ,.")]
    # "at 7:00 PM I need to go for dinner" — clause starting with a time still belongs to a new event
    return parts or [text]


def parse(transcript: str, contacts: Contacts, today: datetime, ablate: bool = False) -> tuple[list[SubIntent], dict]:
    """Returns subintents and a meta dict: cleaned, corrections, retraction, garbage, sensitive.
    ablate=True switches off the deterministic safety rules (corrections) to measure what they buy."""
    meta = {"raw": transcript}
    if is_garbage(transcript):
        meta.update(cleaned=transcript, garbage=True); return [], meta
    stripped = strip_disfluencies(transcript)
    cleaned, notes = (stripped, []) if ablate else apply_corrections(stripped)
    meta.update(cleaned=cleaned, corrections=notes, retraction=has_retraction(transcript),
                sensitive=bool(SENSITIVE.search(transcript)))
    if meta["retraction"]:
        return [], meta

    subs: list[SubIntent] = []
    # a correction of the last thing done ("it's not a meeting, it's a lunch") is one intent, not two clauses
    lc = cleaned.lower()
    # "call it spade. spa day" — a rename where the mic heard a false start: the last segment is the name
    cm = re.match(r"^\s*(?:call it|name it|title it|rename it to|rename it)\s+(.+)$", lc)
    if cm and not RENAME.search(lc):
        last = [x.strip() for x in re.split(r"[.,;]", cm.group(1)) if x.strip()]
        si = SubIntent(intent=Intent.RENAME_EVENT, clause=cleaned)
        si.title = Slot(last[-1], Source.deterministic, 0.85, "rename phrase, last version") if last else Slot(None, Source.none)
        return [si], meta
    rm = RENAME.search(lc)
    if rm and not re.search(r"\b(reply|email|tell|put|add|schedule|book)\b", lc):
        new_title = (rm.group(2) or rm.group(3) or rm.group(5) or "").strip()
        old_title = (rm.group(1) or rm.group(4) or "").strip()
        # "call it spade. spa day" — the words after a sentence break are the correction; last version wins
        tail = cleaned.lower()[rm.end():].strip(" .,")
        if tail and not old_title:
            new_title = re.split(r"[.,;]", tail)[-1].strip() or new_title
        si = SubIntent(intent=Intent.RENAME_EVENT, clause=cleaned)
        si.title = Slot(new_title, Source.deterministic, 0.9, "rename phrase") if new_title else Slot(None, Source.none)
        if old_title:
            si.recipient = Slot(old_title, Source.deterministic, 0.8, "old name in utterance")
        if not new_title:
            si.question = "What should I call it?"
        return [si], meta
    # "the dentist you put on Friday, change it to 11" — a reference to an existing event, one intent
    ref = re.search(r"\bthe\s+([a-z]+(?:\s+[a-z]+)?)\s+(?:thing\s+|one\s+|appointment\s+|event\s+)?(?:you|that you|i|we)\s+(?:put|added|registered|booked|scheduled|have)\b", cleaned.lower())
    if ref and re.search(r"\b(change|move|update|edit|make it|reschedule|push|shift|rename|delete|remove)\b", cleaned.lower()):
        si = SubIntent(intent=Intent.UPDATE_EVENT, clause=cleaned)
        si.title = Slot(ref.group(1).strip(), Source.deterministic, 0.85, "the X you put")
        tail = cleaned.lower()[ref.end():]
        when, _ = parse_when(tail, today)
        si.datetime = when
        if not when.value and not re.search(r"\b(delete|remove)\b", cleaned.lower()):
            si.question = f"What should I change about {si.title.value} — the time, or the name?"
        return [si], meta
    clauses = split_clauses(cleaned)
    carry_recipient: Slot | None = None
    carry_when: Slot | None = None
    for cl in clauses:
        intent = detect_intent(cl)
        if intent is None and subs and subs[-1].intent == Intent.CREATE_EVENT and not subs[-1].datetime.value \
                and re.search(r"\b(do it|make it|have it|it|that)\b", cl.lower()) and re.search(r"\d|monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|noon", cl.lower()):
            when, amb = parse_when(cl, today)
            if when.value:
                subs[-1].datetime = when
                subs[-1].question = None if not amb else subs[-1].question
                carry_when = when
            continue
        if intent is None:
            # a bare "at 7pm dinner with friends" style clause after a CREATE_EVENT
            if subs and subs[-1].intent == Intent.CREATE_EVENT and re.search(r"\d", cl):
                intent = Intent.CREATE_EVENT
            elif subs and re.search(r"\b(it|that)\b", cl) and re.search(r"calendar", cl):
                intent = Intent.CREATE_EVENT
            else:
                continue
        if intent == Intent.CREATE_EVENT and subs and subs[-1].intent == Intent.CREATE_EVENT \
                and re.search(r"\b(it|that|this)\b", cl.lower()) and not re.search(r"\d", cl):
            continue  # "…, stick it in the calendar" — same event, no new information
        si = SubIntent(intent=intent, clause=cl)
        if intent in (Intent.REPLY_EMAIL, Intent.SEND_EMAIL, Intent.NOTIFY):
            slot, cands, mention = extract_recipient(cl, contacts)
            if (not slot.value) and mention and mention.lower().split()[0] in ("him", "her", "them") and carry_recipient and carry_recipient.value:
                slot = Slot(carry_recipient.value, Source.deterministic, 0.85, f"'{mention}' → the person just mentioned")
                cands = [{"name": carry_recipient.value}]
            si.recipient = slot
            if not slot.value:
                if len(cands) > 1:
                    si.question = f"Which one — {' or '.join(c['name'] for c in cands)}?"
                elif mention and mention.lower() in ("him", "her", "them"):
                    si.question = f"Who do you mean by “{mention}”? Say their name."
                elif mention:
                    si.question = f"I don't have a contact called {mention}. Who do you mean?"
                else:
                    si.question = "Who should I send that to?"
            si.message = extract_message(cl)
            if intent == Intent.NOTIFY:
                si.channel = Slot("slack", Source.deterministic, 0.9, "notify ⇒ slack")
                if not si.message.value:
                    # "tell my sister I'll be there at 2" — message is everything after the recipient phrase
                    rest = re.sub(r"^\s*(?:tell|text|message|notify|ping)\s+" + re.escape(mention) + r"\s*", "", cl, flags=re.I).strip(" ,.")
                    if rest and rest.lower() != cl.lower():
                        si.message = Slot(rest, Source.deterministic, 0.8, "after recipient")
            if "[unintelligible]" in transcript.lower() and si.message.value and "unintelligible" in si.message.value.lower():
                si.message = Slot(None, Source.none)
                si.question = si.question or "What should the message say?"
            when, amb = parse_when(cl, today)
            si.datetime = when
            carry_recipient = si.recipient
        elif intent == Intent.RENAME_EVENT:
            m = RENAME.search(cl.lower())
            new_title = (m.group(2) or m.group(3) or "").strip()
            si.title = Slot(new_title, Source.deterministic, 0.9, "rename phrase") if new_title else Slot(None, Source.none)
            if not new_title:
                si.question = "What should I call it?"
        elif intent in (Intent.CREATE_EVENT, Intent.UPDATE_EVENT):
            si.title = extract_title(cl)
            wm = re.search(r"\bwith\s+((?:dr\.?\s+|doctor\s+)?[a-z]+(?:\s+[a-z]+)?)", cl, re.I)
            if wm:
                wc, _ = contacts.resolve(wm.group(1))
                if len(wc) == 1:
                    carry_recipient = Slot(wc[0]["name"], Source.deterministic, 0.8, "mentioned with the event")
            when, amb = parse_when(cl, today)
            si.datetime = when
            if amb:
                si.question = "This Thursday or next? Say the date — the 17th or the 24th."
            elif not when.value:
                # "add it to my calendar" — inherit datetime from an earlier clause
                if carry_when and carry_when.value:
                    si.datetime = Slot(carry_when.value, Source.deterministic, 0.8, "inherited from earlier clause")
                else:
                    si.question = "When should I put that — what day and time?"
            elif intent == Intent.CREATE_EVENT and when.value.endswith("T00:00") and "no-hour" in (when.evidence or ""):
                si.question = f"What time on {datetime.fromisoformat(when.value).strftime('%A')}?"
            if not si.title.value:
                who = carry_recipient.value if (carry_recipient and carry_recipient.value) else None
                if who:
                    si.title = Slot(f"Follow-up — {who}", Source.deterministic, 0.8, "title from the reply's recipient")
                else:
                    si.title = Slot(None, Source.none, 0.0, "no title in utterance")  # agent will ask the model, then the user
        elif intent == Intent.QUERY:
            when, _ = parse_when(cl, today)
            si.datetime = when
        if si.datetime.value:
            carry_when = si.datetime
        subs.append(si)
    if not subs:
        meta["unparsed"] = True
    return subs, meta
