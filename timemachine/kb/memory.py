"""InsightMemory: dated notes the system writes for its own future steps (per repo).

The KB (kb/base.py) is a pure function of repo events. This is the other half: conclusions the
system itself drew at an earlier T ("src/core.py: 5 of 12 commits were fixes, review changes there")
that later steps can read next to the raw history.

Rules that keep it replay-valid:
- Append-only. A note is a *version*: (id, created_at, ...). Changing a note adds a new version with a
  later created_at; nothing is edited in place, so the notebook at time T is recoverable.
- Reads are filtered by `created_at <= as_of`; a note written at T1 does not exist at T0.
- Every evidence ref (commit sha / issue number) must be visible in the KB at the note's created_at.
  Unknown or newer evidence is rejected, so a note cannot cite the future.
- Notes are written from inside the sandbox from data <= T and leave it only as plain JSON; the host
  stores them (data/<repo>/insights.jsonl). Warm start from a stored notebook is refused if it holds
  a note newer than the run's first T.
No LLM and no network here; LLM-written notes arrive through the same `add`.
"""
import json
from datetime import datetime, timezone
from .bm25 import tokenize

ACTIVE = ("active", "confirmed")          # statuses a reader sees
STATUSES = ("active", "confirmed", "refuted", "retired")
FIELDS = ("id", "created_at", "kind", "scope", "claim", "evidence", "confidence", "source", "status", "version")


class NoteError(ValueError):
    pass


def _day(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


class InsightMemory:
    def __init__(self):
        self._versions = {}       # id -> [note version, ...] in creation order
        self._pending = []        # versions added since the last drain() (the host persists these)

    # ---- writing ----
    def add(self, note: dict, as_of: int, kb=None) -> dict:
        """Add a note, or a new version of an existing one (same id). `created_at` defaults to `as_of`."""
        n = {"scope": [], "evidence": [], "confidence": 0.5, "source": "heuristic", "status": "active", **note}
        n["created_at"] = as_of if n.get("created_at") is None else n["created_at"]
        for f in ("id", "kind", "claim"):
            if not n.get(f):
                raise NoteError(f"note needs {f!r}")
        if n["status"] not in STATUSES:
            raise NoteError(f"bad status {n['status']!r}")
        if n["created_at"] > as_of:
            raise NoteError(f"note {n['id']} is dated {n['created_at']}, after T={as_of}")
        prev = self._versions.get(n["id"], [])
        if prev and n["created_at"] < prev[-1]["created_at"]:
            raise NoteError(f"note {n['id']} would go back in time")
        n["scope"], n["evidence"] = list(n["scope"]), [str(e) for e in n["evidence"]]
        if kb is not None:
            self._check_evidence(n, kb)
        n["version"] = len(prev) + 1
        n = {k: n[k] for k in FIELDS}
        self._versions.setdefault(n["id"], []).append(n)
        self._pending.append(n)
        return n

    @staticmethod
    def _check_evidence(n, kb):
        for e in n["evidence"]:
            if e.startswith("#"):
                i = kb.issues.get(int(e[1:])) if e[1:].isdigit() else None
                ts = i["ts"] if i else None
            else:
                c = kb.commit(e)
                ts = c["ts"] if c else None
            if ts is None:
                raise NoteError(f"note {n['id']} cites {e}, which is not visible in the KB")
            if ts > n["created_at"]:
                raise NoteError(f"note {n['id']} cites {e} from after its own date")

    def mark(self, note_id: str, status: str, as_of: int, reason: str = "") -> dict | None:
        """New version with another status (confirmed / refuted / retired)."""
        cur = self.latest(note_id, as_of)
        if cur is None or cur["status"] == status:
            return None
        n = {**cur, "status": status, "created_at": as_of}
        if reason:
            n["claim"] = cur["claim"] if reason in cur["claim"] else f"{cur['claim']} [{status}: {reason}]"
        return self.add(n, as_of)

    # ---- reading ----
    def latest(self, note_id: str, as_of: int) -> dict | None:
        return next((v for v in reversed(self._versions.get(note_id, ())) if v["created_at"] <= as_of), None)

    def all_latest(self, as_of: int) -> list[dict]:
        out = (self.latest(i, as_of) for i in self._versions)
        return sorted((n for n in out if n), key=lambda n: (n["created_at"], n["id"]))

    def active(self, as_of: int, kind=None) -> list[dict]:
        return [n for n in self.all_latest(as_of) if n["status"] in ACTIVE and (kind is None or n["kind"] == kind)]

    def retrieve(self, as_of: int, paths=(), text: str = "", k: int = 5, kinds=None) -> list[dict]:
        """Notes relevant to some files and/or text: path match (exact 3, same directory 1) plus word
        overlap with the claim, scaled by confidence. Only notes that exist at `as_of`."""
        paths, words = list(paths), set(tokenize(text)) if text else set()
        dirs = {p.rsplit("/", 1)[0] for p in paths if "/" in p}
        scored = []
        for n in self.active(as_of):
            if kinds and n["kind"] not in kinds:
                continue
            s = 0.0
            for sc in n["scope"]:
                s += 3.0 if sc in paths else (1.0 if "/" in sc and sc.rsplit("/", 1)[0] in dirs else 0.0)
            if words:
                s += len(words & set(tokenize(n["claim"]))) / (1 + len(words)) ** 0.5
            if s > 0:
                scored.append((s * (0.5 + n["confidence"]), n["created_at"], n["id"], n))
        scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
        return [x[3] for x in scored[:k]]

    def render(self, notes: list[dict]) -> str:
        """Compact text for a prompt: one line per note, with the date it was written."""
        return "\n".join(f"- [{_day(n['created_at'])}] {n['claim']}" for n in notes)

    def summary(self, as_of: int | None = None) -> dict:
        notes = self.all_latest(as_of if as_of is not None else 1 << 62)
        by = {}
        for n in notes:
            by[n["status"]] = by.get(n["status"], 0) + 1
        return {"notes": len(notes), "versions": sum(len(v) for v in self._versions.values()), "by_status": by}

    # ---- persistence (the host stores JSONL; the sandbox only hands notes over as plain dicts) ----
    def drain(self) -> list[dict]:
        out, self._pending = self._pending, []
        return out

    def import_notes(self, notes: list[dict], start: int):
        """Warm start from a stored notebook. Refuses notes newer than the run's first T."""
        late = [n["id"] for n in notes if n["created_at"] > start]
        if late:
            raise NoteError(f"stored notebook has {len(late)} notes after the run's start T={start}: {late[:3]}")
        for n in sorted(notes, key=lambda n: (n["created_at"], n["version"])):
            if not all(f in n for f in FIELDS):
                raise NoteError(f"stored note {n.get('id')!r} is missing fields")
            self._versions.setdefault(n["id"], []).append({k: n[k] for k in FIELDS})

    @staticmethod
    def dumps(notes: list[dict]) -> str:
        return "".join(json.dumps(n, sort_keys=True) + "\n" for n in notes)

    @staticmethod
    def loads(text: str) -> list[dict]:
        return [json.loads(l) for l in text.splitlines() if l.strip()]


# ---- deterministic notes: no LLM, derived from the KB at T ----
def hotspot_notes(kb, mem: InsightMemory, as_of: int, min_fixes=3, min_ratio=0.15, max_evidence=3) -> list[dict]:
    """Files whose history so far is full of bug fixes. One note per file; a new version only when the
    number of fixes changes, and the note is retired if the file stops qualifying."""
    out = []
    for path, s in kb._stats.items():
        nid = f"hotspot:{path}"
        cur = mem.latest(nid, as_of)
        qualifies = s["fixes"] >= min_fixes and s["fixes"] / max(1, s["commits"]) >= min_ratio
        if not qualifies:
            if cur and cur["status"] in ACTIVE:
                out.append(mem.mark(nid, "retired", as_of, "no longer a hotspot"))
            continue
        if cur and cur["status"] in ACTIVE and cur["evidence"] and _fixes_of(cur) == s["fixes"]:
            continue
        ev = sorted(s["fix_shas"], key=lambda x: (-kb.commit(x)["ts"], x))[:max_evidence]
        claim = (f"{path}: {s['fixes']} of {s['commits']} commits so far were bug fixes "
                 f"(last change {_day(s['last_ts'])}); changes here deserve extra review and tests.")
        out.append(mem.add({"id": nid, "kind": "file_hotspot", "scope": [path], "claim": claim, "evidence": ev,
                            "confidence": min(0.9, 0.4 + 0.05 * s["fixes"]), "source": "heuristic"}, as_of, kb=kb))
    return out


def _fixes_of(note) -> int:
    try:
        return int(note["claim"].split(": ", 1)[1].split(" of ", 1)[0])
    except (IndexError, ValueError):
        return -1


DERIVERS = (hotspot_notes,)


def derive_notes(kb, mem: InsightMemory, as_of: int) -> list[dict]:
    out = []
    for d in DERIVERS:
        out.extend(d(kb, mem, as_of))
    return out
