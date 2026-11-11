"""Host side of the incremental time machine.

The full store becomes one time-ordered event list (kb/events.py) with a cursor. advance(T) hands
out only the events in (previous T, T]; time never moves backwards. The sandbox is fed exclusively
from here, so it physically holds nothing after the cursor."""
from bisect import bisect_right
from .kb.events import events_from_db, COMMIT
from .store import connect


class Timeline:
    def __init__(self, db_path: str):
        db = connect(db_path)
        self._ev = events_from_db(db)
        db.close()
        self._ts = [e["vts"] for e in self._ev]
        self._i = 0
        self.as_of = None
        self.released_commits = set()

    def fork(self) -> "Timeline":
        """Same events, fresh cursor (one per sandbox; loading the store happens once)."""
        t = Timeline.__new__(Timeline)
        t._ev, t._ts, t._i, t.as_of, t.released_commits = self._ev, self._ts, 0, None, set()
        return t

    def __len__(self):
        return len(self._ev)

    @property
    def released(self) -> int:
        return self._i

    def position(self, t: int) -> int:
        """Number of events visible at time t (what a rebuild at t would have to materialize)."""
        return bisect_right(self._ts, t)

    def peek_after(self, kind=COMMIT):
        """First not-yet-released event of a kind (used only by the leak probe)."""
        return next((e for e in self._ev[self._i:] if e["kind"] == kind), None)

    def advance(self, t: int) -> list[dict]:
        if self.as_of is not None and t < self.as_of:
            raise ValueError(f"time only moves forward: cursor is at {self.as_of}, asked for {t}")
        j = bisect_right(self._ts, t)
        out = self._ev[self._i:j]
        self._i, self.as_of = j, t
        self.released_commits.update(e["row"][0] for e in out if e["kind"] == COMMIT)
        return out
