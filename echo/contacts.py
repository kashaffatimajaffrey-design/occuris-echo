"""Contact resolution against the contacts sheet. Deterministic. Ambiguity ⇒ returns all candidates."""
from __future__ import annotations

import re
from difflib import SequenceMatcher


def _norm(s: str) -> str:
    s = re.sub(r"\bdoctor\b", "dr", s.lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", "", s)).strip()


class Contacts:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def _aliases(self, row) -> list[str]:
        names = [row["name"]] + [a for a in (row.get("aliases") or "").split(";") if a]
        return [_norm(n) for n in names]

    def resolve(self, mention: str) -> tuple[list[dict], str]:
        """Return (candidates, evidence). One candidate = resolved. Many = ambiguous. None = unknown."""
        m = _norm(mention)
        if not m:
            return [], "empty mention"
        exact, partial = [], []
        for row in self.rows:
            als = self._aliases(row)
            if m in als:
                exact.append(row)
            elif any(m in a.split() or a in m.split() for a in als):
                partial.append(row)
        if len(exact) == 1:
            return exact, f"exact alias match '{mention}' → {exact[0]['name']}"
        if len(exact) > 1:
            return exact, f"'{mention}' matches {len(exact)} contacts: " + ", ".join(r["name"] for r in exact)
        # score by how many distinct name tokens the mention contains ("dr anita patel" → Anita 3, Ravi 1)
        mtoks = set(m.split())
        scored = []
        for row in self.rows:
            toks = set()
            for a in self._aliases(row):
                toks |= set(a.split())
            hit = len(mtoks & toks)
            if hit:
                scored.append((hit, row))
        if scored:
            best = max(h for h, _ in scored)
            top = [r for h, r in scored if h == best]
            if len(top) == 1 and best >= 2:
                return top, f"name-part match '{mention}' → {top[0]['name']} ({best} parts)"
        if partial:
            return partial, f"partial match '{mention}' → " + ", ".join(r["name"] for r in partial)
        # homophone / near-miss: John vs Joan vs "Jon"
        near = []
        for row in self.rows:
            for a in self._aliases(row):
                for tok in a.split():
                    if len(tok) >= 3 and SequenceMatcher(None, m, tok).ratio() >= 0.75:
                        near.append(row); break
                else:
                    continue
                break
        if near:
            return near, f"near match '{mention}' ~ " + ", ".join(r["name"] for r in near)
        return [], f"no contact matches '{mention}'"
