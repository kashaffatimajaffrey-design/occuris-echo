"""Read-back text is built ONLY from slots and action statuses — never from the raw transcript.
Dates and times are spoken in full so a screen-reader user hears a sentence, not a timestamp."""
from __future__ import annotations

from datetime import datetime

from .models import Action, Status, SubIntent

_ORD = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth", 7: "seventh", 8: "eighth", 9: "ninth",
        10: "tenth", 11: "eleventh", 12: "twelfth", 13: "thirteenth", 14: "fourteenth", 15: "fifteenth", 16: "sixteenth",
        17: "seventeenth", 18: "eighteenth", 19: "nineteenth", 20: "twentieth", 21: "twenty-first", 22: "twenty-second",
        23: "twenty-third", 24: "twenty-fourth", 25: "twenty-fifth", 26: "twenty-sixth", 27: "twenty-seventh",
        28: "twenty-eighth", 29: "twenty-ninth", 30: "thirtieth", 31: "thirty-first"}


def speak_when(iso: str | None, time_only=False, day_only=False) -> str:
    if not iso:
        return "a time to be confirmed"
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return "an unclear time"
    hour = d.hour % 12 or 12
    ap = "am" if d.hour < 12 else "pm"
    t = f"{hour}{'' if d.minute == 0 else ':%02d' % d.minute} {ap}"
    if time_only:
        return t
    day = f"{d.strftime('%A')} the {_ORD.get(d.day, str(d.day))} of {d.strftime('%B')}"
    if day_only:
        return day
    if d.hour == 0 and d.minute == 0:
        return day
    return f"{day} at {t}"


def _who(s: SubIntent) -> str:
    return (s.recipient.value or "").split(" (")[0] or "them"


def readback_for(actions: list[Action], subs: list[SubIntent], confirm: bool = False) -> str:
    """One sentence per action, in the order they happened. Statuses are stated in words."""
    parts = []
    for a, s in zip(actions, subs):
        if a.status == Status.done:
            parts.append(a.detail or f"{a.app} {a.op} done")
        elif a.status == Status.pending:
            if s.intent.value in ("REPLY_EMAIL", "SEND_EMAIL"):
                msg = s.message.value or (f"{speak_when(s.datetime.value)} works" if s.datetime.value else "")
                ev = f" — {s.recipient.evidence}" if "matches" in (s.recipient.evidence or "") or "near" in (s.recipient.evidence or "") else ""
                verb = "Reply to" if s.intent.value == "REPLY_EMAIL" else "Email"
                parts.append(f"{verb} {s.recipient.value}: “{msg}”{ev}")
            elif s.intent.value == "NOTIFY":
                parts.append(f"Message {_who(s)} on Slack: “{s.message.value or ''}”")
            elif s.intent.value == "UPDATE_EVENT":
                parts.append(f"Move {s.title.value or 'the event'} to {speak_when(s.datetime.value)}")
            elif s.intent.value == "CREATE_EVENT" and s.conflict:
                parts.append(f"Add {s.title.value} {speak_when(s.datetime.value)} — that overlaps with {s.conflict} already on your calendar")
            else:
                parts.append(f"{a.app} {a.op} to {s.recipient.value or s.title.value}")
        elif a.status == Status.skipped_duplicate:
            if a.app == "calendar":
                parts.append(f"That's already how it is — {s.title.value or s.recipient.value or 'that event'}. Nothing changed")
            else:
                parts.append(f"I already sent that to {_who(s)} — not sending it again")
        elif a.status == Status.failed:
            target = _who(s) if s.recipient.value else (s.title.value or "that")
            parts.append(f"{a.app.capitalize()} failed for {target} — {a.detail}")
        elif a.status == Status.not_attempted:
            parts.append(f"I couldn't reach {_who(s)} — {a.detail or 'no channel on file'}. Nothing was sent to them")
    text = ". ".join(p.rstrip(".") for p in parts if p)
    if confirm and any(a.status == Status.pending for a in actions):
        text += ". Send it?"
    elif text:
        text += "."
    return text or "Nothing to do."
