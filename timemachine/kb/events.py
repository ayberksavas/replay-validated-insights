"""Store rows -> time-ordered events. The ONE definition of "when does a row become visible".

Used by the host timeline (full store, released step by step into the sandbox) and by
KnowledgeBase(view) (rows <= T). Both therefore see exactly the same events in the same order.

Visibility time (`vts`):
- commit: committer time; its changed files travel with it.
- issue/PR: created_at.
- issue event / comment: max(own time, issue created_at); rows whose issue is unknown never appear
  (same rule as TimeView: `number IN (SELECT number FROM issues WHERE ts<=T)`).
Ties are ordered issue < commit < issue_event < comment, then by source order, so an issue always
arrives before its own events.
"""

ISSUE, COMMIT, ISSUE_EVENT, COMMENT = "issue", "commit", "issue_event", "comment"


def events_from_db(db) -> list[dict]:
    files = {}
    for r in db.execute("SELECT sha,path,added,deleted,is_test,is_doc FROM commit_files ORDER BY rowid"):
        files.setdefault(r[0], []).append([r[1], r[2], r[3], r[4], r[5]])
    issue_ts, ev = {}, []
    for r in db.execute("SELECT number,is_pr,ts,author,title,body FROM issues"):
        issue_ts[r[0]] = r[2]
        ev.append((r[2], 0, r[0], {"kind": ISSUE, "vts": r[2], "row": list(r)}))
    for r in db.execute("SELECT sha,ts,author_ts,author,message,parents FROM commits"):
        ev.append((r[1], 1, r[0], {"kind": COMMIT, "vts": r[1], "row": list(r), "files": files.get(r[0], [])}))
    for r in db.execute("SELECT rowid,number,ts,type,actor,data FROM issue_events ORDER BY rowid"):
        if r[1] in issue_ts:
            v = max(r[2], issue_ts[r[1]])
            ev.append((v, 2, r[0], {"kind": ISSUE_EVENT, "vts": v, "row": list(r)[1:]}))
    for r in db.execute("SELECT id,number,ts,author,body,kind FROM comments"):
        if r[1] in issue_ts:
            v = max(r[2], issue_ts[r[1]])
            ev.append((v, 3, r[0], {"kind": COMMENT, "vts": v, "row": list(r)}))
    ev.sort(key=lambda e: e[:3])
    return [e[3] for e in ev]
