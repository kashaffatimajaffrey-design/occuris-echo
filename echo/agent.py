"""Stub — replaced in the next step. Exists so the harness runs red rather than erroring."""
from .models import Trace, Intent, Gate


class Agent:
    def __init__(self, providers, use_model=False, ablate=False):
        self.p = providers
        self._pending = []

    def handle(self, transcript: str) -> Trace:
        return Trace(run_id="stub", transcript=transcript, cleaned=transcript, intent=Intent.REFUSE,
                     subintents=[], gate=Gate.BLOCKED, actions=[], readback="not implemented")

    def pending(self):
        return self._pending
