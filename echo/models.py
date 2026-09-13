"""Core types. Every field the harness scores lives here."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class Intent(str, Enum):
    REPLY_EMAIL = "REPLY_EMAIL"
    SEND_EMAIL = "SEND_EMAIL"
    CREATE_EVENT = "CREATE_EVENT"
    UPDATE_EVENT = "UPDATE_EVENT"
    RENAME_EVENT = "RENAME_EVENT"
    NOTIFY = "NOTIFY"
    MULTI = "MULTI"
    QUERY = "QUERY"
    CLARIFY = "CLARIFY"
    REFUSE = "REFUSE"


class Gate(str, Enum):
    CONFIRM_REQUIRED = "CONFIRM_REQUIRED"
    AUTO_OK = "AUTO_OK"
    BLOCKED = "BLOCKED"


class Status(str, Enum):
    done = "done"
    skipped_duplicate = "skipped_duplicate"
    failed = "failed"
    not_attempted = "not_attempted"
    pending = "pending"


class Source(str, Enum):
    deterministic = "deterministic"
    model = "model"
    none = "none"


@dataclass
class Slot:
    value: Any = None
    score_source: Source = Source.none
    confidence: float = 0.0
    evidence: str = ""

    def __post_init__(self):
        # Hard rule: no source ⇒ no value. Never invent.
        if self.score_source == Source.none:
            self.value = None
            self.confidence = 0.0


@dataclass
class SubIntent:
    """One atomic thing to do. MULTI is a list of these."""
    intent: Intent
    recipient: Slot = field(default_factory=Slot)
    datetime: Slot = field(default_factory=Slot)
    channel: Slot = field(default_factory=Slot)
    message: Slot = field(default_factory=Slot)
    title: Slot = field(default_factory=Slot)
    conflict: Optional[str] = None
    question: Optional[str] = None  # set when this sub-intent needs a CLARIFY
    clause: str = ""                # the words this sub-intent came from (for merging a later answer)


@dataclass
class Action:
    app: str
    op: str
    idempotency_key: str
    status: Status = Status.pending
    detail: str = ""
    latency_ms: int = 0
    evidence: str = ""
    link: str = ""          # where to see it in the real app — the receipt


@dataclass
class Trace:
    run_id: str
    transcript: str
    cleaned: str
    intent: Intent
    subintents: list[SubIntent]
    gate: Gate
    actions: list[Action]
    readback: str
    question: Optional[str] = None
    injection_detected: bool = False
    blocked_reason: Optional[str] = None
    corrections: list[str] = field(default_factory=list)
    started: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_json(self) -> str:
        def enc(o):
            if isinstance(o, Enum):
                return o.value
            if hasattr(o, "__dataclass_fields__"):
                return asdict(o)
            return str(o)
        return json.dumps(asdict(self), default=enc, indent=2)


def idempotency_key(intent: Intent, recipient: str | None, when: str | None, message: str | None) -> str:
    raw = "|".join([intent.value, (recipient or "").lower().strip(), (when or ""), (message or "").lower().strip()])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
