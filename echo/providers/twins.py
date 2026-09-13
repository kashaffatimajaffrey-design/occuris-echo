"""In-memory twins of Gmail, Calendar, Slack and Sheets, seeded from a scenario dict.
Resettable, inspectable, and able to inject failures — the harness asserts on this state."""
from __future__ import annotations

import copy
import itertools
from datetime import datetime, timedelta

from .base import ProviderError

DEFAULT_CONTACTS = [
    {"name": "Dr. Anita Patel", "aliases": "Dr. Patel;Patel;Anita;Dr Patel", "email": "kash.fatima7@gmail.com", "slack": "", "relationship": "doctor", "trusted": "no"},
    {"name": "Ravi Patel", "aliases": "R. Patel;Patel;Ravi", "email": "raufkashaf63@gmail.com", "slack": "", "relationship": "colleague", "trusted": "no"},
    {"name": "Maria Lopez", "aliases": "Maria;Lopez", "email": "maria.lopez.demo@example.com", "slack": "", "relationship": "colleague", "trusted": "no"},
    {"name": "Priya (sister)", "aliases": "Priya;my sister;sister", "email": "", "slack": "C0C1CJPDE4E", "relationship": "family", "trusted": "yes"},
    {"name": "Amir (brother)", "aliases": "Amir;my brother;brother", "email": "", "slack": "", "relationship": "family", "trusted": "yes"},
    {"name": "John Carter", "aliases": "John", "email": "john.carter.demo@example.com", "slack": "", "relationship": "colleague", "trusted": "no"},
    {"name": "Joan Carter", "aliases": "Joan", "email": "joan.carter.demo@example.com", "slack": "", "relationship": "colleague", "trusted": "no"},
    {"name": "Sam Reyes", "aliases": "Sam", "email": "sam.reyes.demo@example.com", "slack": "", "relationship": "friend", "trusted": "no"},
]

DEFAULT_INBOX = [
    {"id": "t1", "from": "Dr. Anita Patel <kash.fatima7@gmail.com>", "email": "kash.fatima7@gmail.com", "subject": "Follow-up appointment",
     "body": "Hi Kashaf, could we do Thursday at 2pm for your follow-up? Let me know if that works. - Dr. Patel"},
    {"id": "t2", "from": "Ravi Patel <raufkashaf63@gmail.com>", "email": "raufkashaf63@gmail.com", "subject": "Thursday?",
     "body": "Are you free Thursday? Want to go over the report. - Ravi"},
    {"id": "t3", "from": "Maria Lopez <maria.lopez.demo@example.com>", "email": "maria.lopez.demo@example.com", "subject": "Slides for Monday",
     "body": "Hi - are the slides ready? I need them before Monday's review. Maria"},
]


class Twins:
    def __init__(self, scenario: dict | None = None):
        sc = scenario or {}
        self.today = sc.get("today", "2026-09-14")  # Monday
        self._contacts = copy.deepcopy(sc.get("contacts", DEFAULT_CONTACTS))
        self._inbox = copy.deepcopy(sc.get("inbox", DEFAULT_INBOX))
        self._events = copy.deepcopy(sc.get("calendar", []))
        self._sent: list[dict] = []
        self._slack: list[dict] = []
        self._ledger: list[dict] = copy.deepcopy(sc.get("prior_ledger", []))
        self._ids = itertools.count(100)
        # failure injection: {"gmail_send": [500], "slack_post": [429]} — pops one code per call
        self._fail = {k: list(v) for k, v in sc.get("fail", {}).items()}
        self.calls: list[dict] = []  # evidence capture

    # ---------------- internals
    def _maybe_fail(self, op: str):
        q = self._fail.get(op)
        if q:
            code = q.pop(0)
            self.calls.append({"op": op, "status": code})
            raise ProviderError(op.split("_")[0], code)

    def _log(self, op: str, **kw):
        self.calls.append({"op": op, "status": 200, **kw})

    def reset_counters(self):
        self.calls = []

    # ---------------- gmail
    def gmail_find_thread(self, email):
        for m in self._inbox:
            if m["email"].lower() == email.lower():
                return m
        return None

    def gmail_reply(self, thread_id, to, body):
        self._maybe_fail("gmail_send")
        mid = f"m{next(self._ids)}"
        self._sent.append({"id": mid, "thread": thread_id, "to": to, "body": body})
        self._log("gmail_reply", to=to, id=mid, link=f"#sandbox/gmail/{mid}")
        return mid

    def gmail_send(self, to, subject, body):
        self._maybe_fail("gmail_send")
        mid = f"m{next(self._ids)}"
        self._sent.append({"id": mid, "thread": None, "to": to, "subject": subject, "body": body})
        self._log("gmail_send", to=to, id=mid, link=f"#sandbox/gmail/{mid}")
        return mid

    def gmail_sent_count(self):
        return len(self._sent)

    def gmail_inbox(self, limit=20):
        return self._inbox[:limit]

    # ---------------- calendar
    def calendar_list(self, day):
        return [e for e in self._events if e["start"].startswith(day)]

    def calendar_upcoming(self, days=14):
        return list(self._events)

    def calendar_find(self, title_like):
        t = title_like.lower()
        for e in self._events:
            if t in e["title"].lower():
                return e
        return None

    def calendar_create(self, title, start_iso, minutes=30):
        self._maybe_fail("calendar_create")
        eid = f"e{next(self._ids)}"
        end = (datetime.fromisoformat(start_iso) + timedelta(minutes=minutes)).isoformat(timespec="minutes")
        self._events.append({"id": eid, "title": title, "start": start_iso, "end": end})
        self._log("calendar_create", id=eid, start=start_iso, link=f"#sandbox/calendar/{eid}")
        return eid

    def calendar_update(self, event_id, start_iso):
        self._maybe_fail("calendar_update")
        for e in self._events:
            if e["id"] == event_id:
                dur = datetime.fromisoformat(e["end"]) - datetime.fromisoformat(e["start"])
                e["start"] = start_iso
                e["end"] = (datetime.fromisoformat(start_iso) + dur).isoformat(timespec="minutes")
                self._log("calendar_update", id=event_id, start=start_iso, link=f"#sandbox/calendar/{event_id}")
                return event_id
        raise ProviderError("calendar", 404)

    def calendar_rename(self, event_id, title):
        self._maybe_fail("calendar_update")
        for e in self._events:
            if e["id"] == event_id:
                e["title"] = title
                self._log("calendar_rename", id=event_id, title=title, link=f"#sandbox/calendar/{event_id}")
                return event_id
        raise ProviderError("calendar", 404)

    def calendar_count(self):
        return len(self._events)

    # ---------------- slack
    def slack_post(self, channel, text):
        self._maybe_fail("slack_post")
        ts = f"{next(self._ids)}.000"
        self._slack.append({"channel": channel, "text": text, "ts": ts})
        self._log("slack_post", channel=channel, ts=ts, link=f"#sandbox/slack/{channel}/{ts}")
        return ts

    def slack_count(self):
        return len(self._slack)

    # ---------------- ledger
    def ledger_append(self, row):
        self._ledger.append(dict(row))

    def ledger_rows(self):
        return list(self._ledger)

    # ---------------- contacts
    def contacts(self):
        return list(self._contacts)

    # ---------------- state snapshot for assertions
    def snapshot(self) -> dict:
        return {"sent": len(self._sent), "events": len(self._events), "slack": len(self._slack), "ledger": len(self._ledger)}
