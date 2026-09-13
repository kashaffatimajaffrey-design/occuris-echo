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

CLAUSE_SPLIT = re.compile(r"(?<![Dd]r)(?<![Mm]r)(?<![Mm]s)(?<![Mm]rs)(?<![Ss]t)\.\s+(?!\d{1,2}(?::\d{2})?\s*(?:am|pm)\b)|,\s*(?:and\s+)?(?!\d{1,2}(?::\d{2})?\s*(?:am|pm)\b)|\s+and then\s+|\s+then\s+|\s+and\s+(?=(?:tell|put|add|email|reply|send|move|remind|text|message|notify|at\s+\d|my\s+(?:sister|brother)|(?:on|for)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow)|tomorrow|next\s+week))", re.I)


def strip_disfluencies(text: str) -> str:
    t = re.sub(r"\be-?mail\b", "email", text, flags=re.I)
    t = re.sub(r"\bdoctor\.?(?=\s+[A-Za-z])", "Dr.", t, flags=re.I)   # "doctor Ravi" / "Doctor. Anita" → "Dr. …"
    t = re.sub(DISFLUENCIES, " ", t, flags=re.I)
    t = re.sub(r"\s*,(\s*,)+", ",", t)          # ", uh," → ","
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
    t = re.sub(r"\b(at|on|around|by|about)\s+(" + "|".join(WORD_NUM) + r")\b(?=\s*(?:am|pm|o'?clock|$|\s|,|\.))", lambda m: m.group(1) + " " + str(WORD_NUM[m.group(2)]), t)
    t = re.sub(r"\b(" + "|".join(WORD_NUM) + r")\s*(am|pm|a\.m\.|p\.m\.)\b", lambda m: str(WORD_NUM[m.group(1)]) + " " + m.group(2), t)
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
        # last resort: dateparser — only when the text has a digit; it must never invent a day from a bare time word
        dp = dateparser.parse(t, settings={"RELATIVE_BASE": today, "PREFER_DATES_FROM": "future"}) if re.search(r"\d", t) else None
        if dp and re.search(r"\d", t):
            return Slot(dp.replace(second=0, microsecond=0).isoformat(timespec="minutes"), Source.deterministic, 0.6, "dateparser"), ambiguous
        return Slot(None, Source.none), ambiguous
    dt = day.replace(hour=hour or 0, minute=minute or 0, second=0, microsecond=0)
    conf = 0.95 if (hour is not None and tm and tm.group(3)) else 0.8
    ev = ("no-day; " if no_day else "") + ("no-hour; " if hour is None else "") + f"weekday/time rule on '{text[:40]}'"
    return Slot(dt.isoformat(timespec="minutes"), Source.deterministic, conf, ev), ambiguous


# ---------------------------------------------------------------- intents
RENAME = re.compile(r"(?:it'?s\s+)?not\s+(?:a\s+|an\s+|the\s+)?([a-z][a-z ]*?),?\s+(?:it'?s\s+)?(?:supposed to be\s+|meant to be\s+|should be\s+)?(?:a\s+|an\s+|the\s+)?([a-z][a-z ]*?)(?:\s*$|[.,])|\b(?:call it|rename it to|name it|change (?:the )?(?:name|title) to)\s+(?:a\s+|an\s+|the\s+)?([a-z][a-z ]*?)\s*$|\brename\s+(?:the\s+)?([a-z][a-z ]*?)\s+to\s+(?:a\s+|an\s+|the\s+)?([a-z][a-z ]*?)\s*$", re.I)


EVENT_NOUNS = r"(lunch|dinner|brunch|breakfast|coffee|meeting|appointment|call|check-?up|dentist|physio|gym|class|interview|flight|train|party|concert|movie|date|visit|session|lesson|standup|review|catch-?up|luncheon|drinks|workout|spa)"


def detect_intent(clause: str) -> Intent | None:
    c = clause.lower()
    if RENAME.search(c): return Intent.RENAME_EVENT
    polite = re.match(r"^\s*(?:so\s+)?(?:can|could|would|will)\s+you\s+(?:please\s+)?|^\s*please\s+", c)
    if polite:
        c = c[polite.end():]
    has_verb = re.search(r"\b(reply|respond|email|mail|send|tell|text|message|notify|ping|put|add|schedule|book|move|reschedule|change|edit|fix|correct|update|rename|delete|remove|remind)\b", c)
    if re.search(r"\b(what'?s on|what is on|what do i (have|need)|do i have|did i|have i|is there|am i free|any slots?|any time|when am i free|what times?|available|tell me if|let me know if|tell me when|look at my (schedule|calendar|week|day)|check my (schedule|calendar)|see if i(?:'m| am) free|when (do|will) i have|when i have (time|a slot)|i'?m free|i am free)\b", c) \
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
    if re.search(r"\b(want to|wanna|i'?d like to|let'?s|i need to|i have to|i'?m going to|i am going to|i'?ve got to)\s+(have|book|do|schedule|go for|go to|grab|get|attend)\b", c) and re.search(r"\b" + EVENT_NOUNS + r"\b", c):
        return Intent.CREATE_EVENT
    if re.search(r"\b(cancel|call off|scrap|strike)\b", c) and not re.search(r"\b(cancel that|never mind)\b", c):
        return Intent.UPDATE_EVENT   # soft-cancel, handled in the agent
    # "Lunch with Dr Patel Friday at 3" — an event noun plus a time, no verb: that is an event
    if re.search(r"\b" + EVENT_NOUNS + r"\b", c) and re.search(r"\d|monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|noon", c) \
            and not re.search(r"\b(reply|email|mail|tell|text|message|send|free|slots?|available|do i have|what)\b", c):
        return Intent.CREATE_EVENT
    return None


RECIP = re.compile(r"\b(?:reply to|respond to|email|mail|tell|text|message|notify|ping|send|let)\s+(?:(?:a|an|the)\s+(?:message|note|email|text|mail|reminder)\s+to\s+|(?:it|this|that|these|the details|the invite)\s+to\s+)?(?:to\s+)?((?:dr\.?\s+|doctor\s+)?[a-z][a-z.]*(?:\s+[a-z][a-z.]*)?)", re.I)
MSG_SAY = re.compile(r"\b(?:that|saying|say|:|about|regarding)\s+(.+)$", re.I)


def extract_recipient(clause: str, contacts: Contacts) -> tuple[Slot, list[dict], str]:
    clause = re.sub(r"\b(reply to|email|tell|send)\s+\1\b", r"\1", clause, flags=re.I)   # "reply to reply to X" (a restart)
    m = RECIP.search(clause)
    if not m:
        return Slot(None, Source.none), [], ""
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
    if re.search(r"\b(about|regarding)\s+" + re.escape(msg) + r"$", clause, re.I):
        msg = "About " + msg
    return Slot(msg, Source.deterministic, 0.9, "after say/that/colon/about")


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
    m = re.search(r"\b(?:put|add|schedule|book)\s+(?:me\s+)?(?:up\s+|on\s+|in\s+)?(?:for\s+)?(?:a\s+|an\s+|the\s+)?([a-z][a-z ]*?)\s+(?:on|at|for|\Z)", c)
    if m and m.group(1).strip() != "me":
        r = _title_or_none(m.group(1), "book X")
        if r.value: return r
    m = re.search(r"\b(?:go for|go to|going to|attend)\s+(?:a\s+|an\s+|the\s+)?(.+?)(?:\s+with\b|\s+on\b|\s+at\b|$)", c)
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
    fwd: list[str] = []
    for frag in joined:
        if fwd and len(fwd[-1].split()) <= 2 and not re.search(r"\d", fwd[-1]) and re.match(r"^(book|put|add|schedule|remind|email|tell)\b", fwd[-1], re.I):
            fwd[-1] = fwd[-1] + " " + frag
        else:
            fwd.append(frag)
    text = ". ".join(fwd)
    parts = [p.strip(" ,.") for p in CLAUSE_SPLIT.split(text) if p and p.strip(" ,.")]
    # ", my sister, about my appointment" — appositives and "about…" belong to the clause before them
    merged: list[str] = []
    for part in parts:
        if merged and (re.match(r"^(about|regarding|re|saying|that|who|which|my (sister|brother|mum|mom|dad))\b", part, re.I)
                       or (len(part.split()) <= 2 and not re.search(r"\d", part) and not detect_intent(part))):
            merged[-1] = merged[-1] + ", " + part
        else:
            merged.append(part)
    parts = merged
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
    dm = re.search(r"\bnot\s+(" + "|".join(WEEKDAYS) + r")\b", lc)
    if dm:
        other = [w for w in WEEKDAYS if w != dm.group(1) and re.search(r"\b" + w + r"\b", lc)]
        if other and not re.search(r"\b(book|put|add|schedule)\b.*\b" + other[0] + r"\b", lc) or (other and re.search(r"\b(told you|i said|it'?s|fix|move|change)\b", lc)):
            si = SubIntent(intent=Intent.UPDATE_EVENT, clause="__last__")
            when, _ = parse_when(other[0], today)
            si.datetime = when
            si.title = Slot(None, Source.none)
            return [si], meta
    # "call it spade. spa day" — a rename where the mic heard a false start: the last segment is the name
    cm = re.match(r"^\s*(?:call it|name it|title it|rename it to|rename it)\s+(.+)$", lc)
    if cm and not RENAME.search(lc):
        last = [x.strip() for x in re.split(r"[.,;]", cm.group(1)) if x.strip()]
        si = SubIntent(intent=Intent.RENAME_EVENT, clause=cleaned)
        si.title = Slot(last[-1], Source.deterministic, 0.85, "rename phrase, last version") if last else Slot(None, Source.none)
        return [si], meta
    # "I don't want Dr. Anita Patel, it's actually Ravi Patel" — a person correction
    NAME = r"((?:dr\.?\s+)?[a-z]+(?:\s+[a-z]+)?)"
    pm2 = re.search(r"\b(?:with|to|for)\s+" + NAME + r"\s*,?\s+(?:and\s+)?not\s+(?:with\s+)?" + NAME, lc)   # "with Ravi Patel, not Anita Patel"
    if pm2:
        pc, _ = contacts.resolve(pm2.group(1)); px, _ = contacts.resolve(pm2.group(2))
        if len(pc) == 1 and len(px) == 1 and pc[0]["name"] != px[0]["name"]:
            si = SubIntent(intent=Intent.SEND_EMAIL, clause="__correct_person__")
            si.recipient = Slot(pc[0]["name"], Source.deterministic, 0.9, "corrected person")
            return [si], meta
    pm3 = re.fullmatch(r"\s*(?:no,?\s+)?it'?s\s+" + NAME + r"\s*\.?", lc)   # "It's Dr. Ravi Patel."
    if pm3:
        pc, _ = contacts.resolve(pm3.group(1))
        if len(pc) == 1:
            si = SubIntent(intent=Intent.SEND_EMAIL, clause="__correct_person__")
            si.recipient = Slot(pc[0]["name"], Source.deterministic, 0.9, "corrected person")
            return [si], meta
    pm = re.search(r"\b(?:it'?s actually\s+|actually it'?s\s+|not\s+(?:with\s+)?(?:dr\.?\s+|doctor\s+)?[a-z]+(?:\s+[a-z]+)?,?\s+(?:it'?s\s+)?(?:actually\s+)?)((?:dr\.?\s+|doctor\s+)?[a-z]+(?:\s+[a-z]+)?)", lc)
    if pm:
        pc, _ = contacts.resolve(pm.group(1))
        if len(pc) == 1:
            si = SubIntent(intent=Intent.SEND_EMAIL, clause="__correct_person__")
            si.recipient = Slot(pc[0]["name"], Source.deterministic, 0.9, "corrected person")
            return [si], meta
    rm = RENAME.search(lc)
    if rm and not re.search(r"\b(reply|email|tell|put|add|schedule|book)\b", lc):
        new_title = (rm.group(2) or rm.group(3) or rm.group(5) or "").strip()
        old_title = (rm.group(1) or rm.group(4) or "").strip()
        if new_title.split()[0] in WEEKDAYS or new_title in ("tomorrow", "today"):
            si = SubIntent(intent=Intent.UPDATE_EVENT, clause="__last__")
            when, _ = parse_when(new_title, today)
            si.datetime = when
            si.title = Slot(None, Source.none)
            return [si], meta
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
        # "I want to do it with Doctor Patel" — a clause that names a person carries them forward
        wm0 = re.search(r"\bwith\s+((?:dr\.?\s+|doctor\s+)?[a-z]+(?:\s+[a-z]+)?)", cl, re.I)
        _frag_day = re.match(r"\s*(?:on\s+)?(?:next\s+)?(?:" + "|".join(WEEKDAYS) + r"|tomorrow|today)\b", cl, re.I)
        _frag_time = re.match(r"\s*(?:at\s+|on\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*$", cl, re.I)
        if wm0 and intent is None and not _frag_day and not _frag_time:
            wc0, _ = contacts.resolve(wm0.group(1))
            if len(wc0) == 1:
                carry_recipient = Slot(wc0[0]["name"], Source.deterministic, 0.8, "mentioned with the event")
                if subs and subs[-1].intent == Intent.CREATE_EVENT and subs[-1].title.value and " with " not in subs[-1].title.value:
                    subs[-1].title = Slot(f"{subs[-1].title.value} with {wc0[0]['name'].split(' (')[0]}", Source.deterministic, 0.8, "event + person")
                continue
        # a fragment that is only a time ("4:00 PM", "at 3") or only a day ("Wednesday") fills the previous event
        only_time = re.fullmatch(r"\s*(?:at\s+|on\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?\s*", cl, re.I)
        only_day = re.fullmatch(r"\s*(?:on\s+)?(?:next\s+)?(?:" + "|".join(WEEKDAYS) + r"|tomorrow|today)\s*(?:with\s+[a-z .]+)?", cl, re.I)
        if intent == Intent.RENAME_EVENT and subs and any(x.intent == Intent.CREATE_EVENT for x in subs):
            m_ci = re.search(r"\b(?:call it|name it|title it)\s+(?:a\s+|an\s+|the\s+)?(.+)$", cl, re.I)
            if m_ci:
                target = next(x for x in reversed(subs) if x.intent == Intent.CREATE_EVENT)
                target.title = Slot(m_ci.group(1).strip(" ."), Source.deterministic, 0.9, "named in the same sentence")
                if target.question and "call" in target.question:
                    target.question = None
                continue
        if intent is None and subs and subs[-1].intent == Intent.CREATE_EVENT and (only_time or only_day):
            prev = subs[-1]
            base = prev.datetime.value
            if only_time:
                when, _ = parse_when(cl, today)
                if when.value:
                    day_part = base[:10] if base else when.value[:10]
                    prev.datetime = Slot(day_part + when.value[10:], Source.deterministic, 0.85, "time from the next fragment")
            else:
                when, amb = parse_when(cl, today)
                if when.value:
                    time_part = base[10:] if (base and "no-hour" not in (prev.datetime.evidence or "")) else "T00:00"
                    ev = "day from the next fragment" + ("" if time_part != "T00:00" else "; no-hour")
                    prev.datetime = Slot(when.value[:10] + time_part, Source.deterministic, 0.85, ev)
                wm = re.search(r"\bwith\s+((?:dr\.?\s+|doctor\s+)?[a-z]+(?:\s+[a-z]+)?)", cl, re.I)
                if wm:
                    wc, _ = contacts.resolve(wm.group(1))
                    if len(wc) == 1:
                        carry_recipient = Slot(wc[0]["name"], Source.deterministic, 0.8, "mentioned with the event")
                        if prev.title.value and " with " not in prev.title.value:
                            prev.title = Slot(f"{prev.title.value} with {wc[0]['name'].split(' (')[0]}", Source.deterministic, 0.8, "event + person")
            if prev.datetime.value and "no-hour" not in (prev.datetime.evidence or "") and prev.question and "time" in prev.question.lower():
                prev.question = None
            if prev.datetime.value and prev.question and "day and time" in prev.question and "no-hour" not in (prev.datetime.evidence or ""):
                prev.question = None
            carry_when = prev.datetime
            continue
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
            new_title = (m.group(2) or m.group(3) or (m.group(5) if m.lastindex and m.lastindex >= 5 else "") or "").strip()
            if new_title and (new_title.split()[0] in WEEKDAYS or new_title in ("tomorrow", "today")):
                # "Not Wednesday, Thursday" — a date correction, never a rename
                si = SubIntent(intent=Intent.UPDATE_EVENT, clause="__last__")
                when, _ = parse_when(new_title, today)
                si.datetime = when
                subs.append(si)
                continue
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
                    if si.title.value and " with " not in si.title.value:
                        si.title = Slot(f"{si.title.value} with {wc[0]['name'].split(' (')[0]}", Source.deterministic, 0.8, "event + person")
                elif len(wc) > 1 and si.title.value:
                    si.question = f"Which one for {si.title.value} — {' or '.join(c['name'] for c in wc)}?"
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
