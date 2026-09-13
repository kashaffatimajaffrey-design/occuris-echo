"""Real adapters: Gmail, Google Calendar, Slack, Google Sheets. Same interface as the twins.
Scopes requested are the minimum for these calls; there is no delete anywhere."""
from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .base import ProviderError

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/spreadsheets",
]
TZ = os.getenv("ECHO_TZ", "Asia/Karachi")


class Real:
    def __init__(self):
        creds = Credentials.from_authorized_user_file(str(ROOT / os.getenv("GOOGLE_TOKEN_FILE", "token.json")), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        self.gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self.cal = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self.sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self.slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
        self.sheet_id = os.environ["LEDGER_SHEET_ID"]
        self.me = self.gmail.users().getProfile(userId="me").execute()["emailAddress"]
        self.today = datetime.now().date().isoformat()
        self._contacts_cache: list[dict] | None = None
        self._ledger_cache: list[dict] | None = None
        self._inbox_cache: list[dict] | None = None
        self.calls: list[dict] = []

    def invalidate(self):
        """Call between turns so the next read sees new mail."""
        self._inbox_cache = None

    # ------------------------------------------------------------ helpers
    def _wrap(self, app, fn):
        try:
            return fn()
        except HttpError as e:
            raise ProviderError(app, e.resp.status, str(e)[:120]) from e
        except SlackApiError as e:
            raise ProviderError("slack", e.response.status_code, e.response.get("error", "")) from e

    @staticmethod
    def _headers(msg):
        return {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}

    @staticmethod
    def _body(msg) -> str:
        p = msg["payload"]
        def walk(part):
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", "replace")
            for sub in part.get("parts", []) or []:
                t = walk(sub)
                if t: return t
            return ""
        return walk(p) or msg.get("snippet", "")

    # ------------------------------------------------------------ gmail
    def gmail_inbox(self, limit=20):
        if self._inbox_cache is not None:
            return self._inbox_cache[:limit]
        # format="full" — "metadata" returned empty headers in testing
        ids = self.gmail.users().messages().list(userId="me", labelIds=["INBOX"], maxResults=25).execute().get("messages", [])
        out = []
        for m in ids:
            full = self.gmail.users().messages().get(userId="me", id=m["id"], format="full").execute()
            h = self._headers(full)
            frm = h.get("from", "")
            email = frm.split("<")[-1].rstrip(">").strip() if "<" in frm else frm.strip()
            out.append({"id": full["threadId"], "msg_id": full["id"], "from": frm, "email": email,
                        "subject": h.get("subject", ""), "body": self._body(full), "message_id_header": h.get("message-id", "")})
        self._inbox_cache = out
        return out

    def gmail_find_thread(self, email):
        for m in self.gmail_inbox(25):
            if m["email"].lower() == email.lower():
                return m
        return None

    def gmail_reply(self, thread_id, to, body):
        th = self.gmail_find_thread(to) or {}
        msg = MIMEText(body)
        msg["to"] = to; msg["from"] = self.me
        msg["subject"] = "Re: " + th.get("subject", "your message")
        if th.get("message_id_header"):
            msg["In-Reply-To"] = th["message_id_header"]; msg["References"] = th["message_id_header"]
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        body_req = {"raw": raw}
        if thread_id: body_req["threadId"] = thread_id
        r = self._wrap("gmail", lambda: self.gmail.users().messages().send(userId="me", body=body_req).execute())
        self.calls.append({"op": "gmail_reply", "to": to, "id": r["id"]})
        return r["id"]

    def gmail_send(self, to, subject, body):
        msg = MIMEText(body); msg["to"] = to; msg["from"] = self.me; msg["subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        r = self._wrap("gmail", lambda: self.gmail.users().messages().send(userId="me", body={"raw": raw}).execute())
        self.calls.append({"op": "gmail_send", "to": to, "id": r["id"]})
        return r["id"]

    def gmail_sent_count(self):
        r = self.gmail.users().messages().list(userId="me", labelIds=["SENT"], maxResults=100).execute()
        return len(r.get("messages", []))

    # ------------------------------------------------------------ calendar
    def calendar_list(self, day):
        start, end = f"{day}T00:00:00", f"{day}T23:59:59"
        r = self._wrap("calendar", lambda: self.cal.events().list(calendarId="primary", timeMin=start + "+05:00", timeMax=end + "+05:00",
                                                                   singleEvents=True, orderBy="startTime").execute())
        out = []
        for e in r.get("items", []):
            s = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", "")
            en = e.get("end", {}).get("dateTime") or e.get("end", {}).get("date", "")
            out.append({"id": e["id"], "title": e.get("summary", "(untitled)"), "start": s[:16], "end": en[:16]})
        return out

    def calendar_find(self, title_like):
        t = title_like.lower()
        now = datetime.now()
        r = self.cal.events().list(calendarId="primary", timeMin=(now - timedelta(days=1)).isoformat() + "+05:00",
                                   maxResults=50, singleEvents=True, orderBy="startTime").execute()
        for e in r.get("items", []):
            if t and t in e.get("summary", "").lower():
                s = e.get("start", {}).get("dateTime", "")
                return {"id": e["id"], "title": e.get("summary"), "start": s[:16], "end": e.get("end", {}).get("dateTime", "")[:16]}
        return None

    def calendar_create(self, title, start_iso, minutes=30):
        end = (datetime.fromisoformat(start_iso) + timedelta(minutes=minutes)).isoformat(timespec="minutes")
        body = {"summary": title, "start": {"dateTime": start_iso + ":00", "timeZone": TZ}, "end": {"dateTime": end + ":00", "timeZone": TZ}}
        r = self._wrap("calendar", lambda: self.cal.events().insert(calendarId="primary", body=body).execute())
        self.calls.append({"op": "calendar_create", "id": r["id"]})
        return r["id"]

    def calendar_update(self, event_id, start_iso):
        ev = self.cal.events().get(calendarId="primary", eventId=event_id).execute()
        dur = datetime.fromisoformat(ev["end"]["dateTime"][:16]) - datetime.fromisoformat(ev["start"]["dateTime"][:16])
        end = (datetime.fromisoformat(start_iso) + dur).isoformat(timespec="minutes")
        ev["start"] = {"dateTime": start_iso + ":00", "timeZone": TZ}; ev["end"] = {"dateTime": end + ":00", "timeZone": TZ}
        self._wrap("calendar", lambda: self.cal.events().update(calendarId="primary", eventId=event_id, body=ev).execute())
        self.calls.append({"op": "calendar_update", "id": event_id})
        return event_id

    def calendar_count(self):
        return len(self.calendar_list(self.today))

    # ------------------------------------------------------------ slack
    def slack_post(self, channel, text):
        r = self._wrap("slack", lambda: self.slack.chat_postMessage(channel=channel, text=text))
        self.calls.append({"op": "slack_post", "channel": channel, "ts": r["ts"]})
        return r["ts"]

    def slack_count(self):
        return len([c for c in self.calls if c["op"] == "slack_post"])

    # ------------------------------------------------------------ sheets: ledger + contacts
    LEDGER_COLS = ["ts", "run_id", "intent", "app", "op", "idempotency_key", "status", "evidence", "readback", "recipient", "datetime", "message", "title", "conflict"]

    def ledger_append(self, row):
        values = [[str(row.get(c, "") if row.get(c) is not None else "") for c in self.LEDGER_COLS]]
        self._wrap("sheets", lambda: self.sheets.spreadsheets().values().append(
            spreadsheetId=self.sheet_id, range="ledger!A1", valueInputOption="RAW", body={"values": values}).execute())
        if self._ledger_cache is not None:
            self._ledger_cache.append(dict(row))

    def ledger_rows(self):
        if self._ledger_cache is None:
            r = self.sheets.spreadsheets().values().get(spreadsheetId=self.sheet_id, range="ledger!A2:N").execute()
            rows = []
            for v in r.get("values", []):
                v = v + [""] * (len(self.LEDGER_COLS) - len(v))
                rows.append({c: (v[i] or None) for i, c in enumerate(self.LEDGER_COLS)})
            self._ledger_cache = rows
        return list(self._ledger_cache)

    def contacts(self):
        if self._contacts_cache is None:
            r = self.sheets.spreadsheets().values().get(spreadsheetId=self.sheet_id, range="contacts!A1:F").execute()
            vals = r.get("values", [])
            head = vals[0]
            self._contacts_cache = [{head[i]: (row[i] if i < len(row) else "") for i in range(len(head))} for row in vals[1:]]
        return list(self._contacts_cache)

    def snapshot(self):
        return {"sent": self.gmail_sent_count(), "events": self.calendar_count(), "slack": self.slack_count(), "ledger": len(self.ledger_rows())}
