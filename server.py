"""Occuris Echo — local server. One agent, one page, real providers by default.

    python server.py            # real Gmail/Calendar/Slack/Sheets
    ECHO_TWINS=1 python server.py   # in-memory twins (safe demo / no credentials)
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from enum import Enum
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from echo.agent import Agent

ROOT = Path(__file__).parent
app = FastAPI(title="Occuris Echo")

if os.getenv("ECHO_TWINS") == "1":
    from echo.providers.twins import Twins
    providers = Twins()
    agent = Agent(providers, use_model=True, today=providers.today)
    MODE = "twins"
else:
    from echo.providers.real import Real
    providers = Real()
    agent = Agent(providers, use_model=True, today=providers.today)
    MODE = "real"

LOG: list[dict] = []  # action lines shown on the page, newest last


def _enc(o):
    if isinstance(o, Enum):
        return o.value
    return str(o)


class Say(BaseModel):
    text: str


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/state")
def state():
    return {"mode": MODE, "pending": agent.pending(), "log": LOG[-40:], "today": str(agent.today.date())}


@app.post("/say")
def say(body: Say):
    if hasattr(providers, "invalidate"):
        providers.invalidate()
    tr = agent.handle(body.text)
    # one log line per action, in words — never colour alone. Keyed by idempotency key so a
    # "waiting" line becomes "done" in place instead of being repeated.
    for a in tr.actions:
        line = {"key": a.idempotency_key, "app": a.app, "op": a.op, "status": a.status.value,
                "text": a.detail or _pending_text(a, tr), "run": tr.run_id, "link": a.link or ""}
        for i, existing in enumerate(LOG):
            if existing.get("key") == a.idempotency_key:
                if existing["status"] != "done" or a.status.value != "done":
                    LOG[i] = line
                break
        else:
            LOG.append(line)
    if not tr.actions or tr.question:
        LOG.append({"app": "echo", "op": tr.intent.value.lower(), "status": "info", "text": tr.question or tr.readback, "run": tr.run_id})
    out = json.loads(json.dumps(asdict(tr), default=_enc))
    out["pending"] = agent.pending()
    return JSONResponse(out)


def _pending_text(a, tr):
    for s in tr.subintents:
        if s.recipient.value or s.title.value:
            return f"Waiting for your yes — {a.app} {a.op} to {s.recipient.value or s.title.value}"
    return f"Waiting for your yes — {a.app} {a.op}"


@app.get("/trace/{n}")
def trace(n: int):
    if not agent.traces:
        return {}
    return json.loads(agent.traces[max(0, min(n, len(agent.traces) - 1))].to_json())


@app.get("/traces")
def traces():
    return [json.loads(t.to_json()) for t in agent.traces[-10:]]


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

if __name__ == "__main__":
    print(f"Occuris Echo — mode: {MODE} — http://localhost:8000")
    uvicorn.run(app, host=os.getenv("ECHO_HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")), log_level="warning")
