"""Model fallback. Called ONLY when the deterministic parser produces nothing.
The model extracts *mentions*; resolution (which contact, which date) still goes through the
deterministic layer, so a model mistake becomes a clarification, not an action.
Everything it produces is tagged score_source: model."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from .contacts import Contacts
from .models import Intent, Slot, Source, SubIntent
from .parse import parse_when

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

SYSTEM = """You extract structured intent from one spoken sentence for an assistant that acts across Gmail, Google Calendar and Slack.
Return ONLY a JSON array. Each element: {"intent": one of REPLY_EMAIL|SEND_EMAIL|CREATE_EVENT|UPDATE_EVENT|NOTIFY|QUERY|UNKNOWN,
"recipient_mention": string or null (the words the user used for the person, verbatim), "when_text": string or null (the words for date/time, verbatim),
"message": string or null (what to say, verbatim, filler words removed), "title": string or null (event title)}.
Never invent a name, date or time that the user did not say. If unsure, use null. If the sentence is not a request, return []."""


def model_parse(transcript: str, contacts: Contacts, today: datetime) -> tuple[list[SubIntent], str]:
    """Returns (subintents, note). Empty list if the model can't help or no API key."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        return [], "no model key"
    try:
        import anthropic
        client = anthropic.Anthropic()
        r = client.messages.create(model=os.getenv("ECHO_MODEL", "claude-sonnet-5"), max_tokens=400, system=SYSTEM,
                                   messages=[{"role": "user", "content": transcript}])
        text = " ".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()
        m = re.search(r"\[.*\]", text, re.S)
        items = json.loads(m.group(0)) if m else []
    except Exception as e:  # noqa: BLE001 — a model failure must never become an action
        return [], f"model error: {type(e).__name__}"

    subs = []
    for it in items[:4]:
        try:
            intent = Intent(it.get("intent", "UNKNOWN"))
        except ValueError:
            continue
        s = SubIntent(intent=intent)
        if it.get("recipient_mention"):
            cands, ev = contacts.resolve(it["recipient_mention"])
            if len(cands) == 1:
                s.recipient = Slot(cands[0]["name"], Source.model, 0.7, f"model mention '{it['recipient_mention']}' → {ev}")
            elif len(cands) > 1:
                s.question = f"Which one — {' or '.join(c['name'] for c in cands)}?"
            else:
                s.question = f"I don't have a contact called {it['recipient_mention']}. Who do you mean?"
        elif intent in (Intent.REPLY_EMAIL, Intent.SEND_EMAIL, Intent.NOTIFY):
            s.question = "Who should I send that to?"
        if it.get("when_text"):
            when, amb = parse_when(it["when_text"], today)
            if when.value:
                s.datetime = Slot(when.value, Source.model, 0.6, f"model when_text '{it['when_text']}' → rule")
            if amb:
                s.question = s.question or "This week or next? Say the date."
        if it.get("message"):
            s.message = Slot(it["message"], Source.model, 0.7, "model")
        if it.get("title"):
            s.title = Slot(it["title"], Source.model, 0.7, "model")
        if intent == Intent.NOTIFY:
            s.channel = Slot("slack", Source.model, 0.7, "model")
        if intent in (Intent.CREATE_EVENT, Intent.UPDATE_EVENT) and not s.datetime.value and not s.question:
            s.question = "When should I put that — what day and time?"
        subs.append(s)
    return subs, f"model parsed {len(subs)} intent(s)"
