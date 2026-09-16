"""A profile-local journal, shared safely by the agent and HTTP processes."""

import difflib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
 session TEXT NOT NULL, run TEXT NOT NULL, started REAL NOT NULL, updated REAL NOT NULL,
 cwd TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '[]', PRIMARY KEY(session, run));
CREATE TABLE IF NOT EXISTS blobs (hash TEXT PRIMARY KEY, data BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS changes (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, run TEXT NOT NULL,
 tool_id TEXT NOT NULL, tool TEXT NOT NULL, source TEXT NOT NULL, path TEXT NOT NULL,
 before TEXT NOT NULL, after TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS changes_session ON changes(session, run, id);
"""
UNKNOWN = {"unreadable", "excluded", "unsupported", "unstable", "unknown"}


def default_root():
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "plugin-data" / "hermes-session-diff"


class Store:
    def __init__(self, root=None):
        self.root = Path(root) if root else default_root()

    @contextmanager
    def connect(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root / "changes.sqlite3"
        db = sqlite3.connect(path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            if os.name != "nt":
                os.chmod(path, 0o600)
            with db:
                yield db
        finally:
            db.close()

    def touch(self, session, run, cwd="", notes=()):
        now = time.time()
        with self.connect() as db:
            previous = db.execute("SELECT notes FROM runs WHERE session=? AND run=?", (session, run)).fetchone()
            merged = list(dict.fromkeys([*(json.loads(previous[0]) if previous else []), *notes]))[:30]
            db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?) ON CONFLICT(session,run) DO UPDATE SET "
                       "updated=excluded.updated,cwd=CASE WHEN excluded.cwd='' THEN runs.cwd ELSE excluded.cwd END,notes=excluded.notes",
                       (session, run, now, now, cwd, json.dumps(merged)))

    def record(self, session, run, tool_id, tool, source, before, after, blobs):
        changed = [(path, before.get(path, {"kind": "absent"}), after.get(path, {"kind": "absent"}))
                   for path in sorted(before.keys() | after.keys())
                   if before.get(path, {"kind": "absent"}) != after.get(path, {"kind": "absent"})]
        if not changed:
            return
        with self.connect() as db:
            # Store only blobs referenced by actual changes, not every scanned file.
            db.execute("BEGIN IMMEDIATE")
            size = db.execute("SELECT COALESCE(SUM(length(data)),0) FROM blobs").fetchone()[0]
            for path, old, new in changed:
                if old["kind"] in UNKNOWN or new["kind"] in UNKNOWN:
                    continue
                for meta in (old, new):
                    digest = meta.get("hash")
                    if meta["kind"] == "text" and digest in blobs:
                        exists = db.execute("SELECT 1 FROM blobs WHERE hash=?", (digest,)).fetchone()
                        if not exists and size + len(blobs[digest]) > 256 * 1024 * 1024:
                            raise RuntimeError("The 256 MiB snapshot budget is full. Reduce retention or remove old plugin data.")
                        cursor = db.execute("INSERT OR IGNORE INTO blobs VALUES (?,?)", (digest, blobs[digest]))
                        if cursor.rowcount:
                            size += len(blobs[digest])
                db.execute("INSERT INTO changes(session,run,tool_id,tool,source,path,before,after,created) VALUES (?,?,?,?,?,?,?,?,?)",
                           (session, run, tool_id, tool, source, path, json.dumps(old), json.dumps(new), time.time()))

    def prune(self, days=30):
        cutoff = time.time() - max(1, min(int(days), 365)) * 86400
        with self.connect() as db:
            db.execute("DELETE FROM changes WHERE created < ?", (cutoff,))
            db.execute("DELETE FROM runs WHERE updated < ?", (cutoff,))
            db.execute("DELETE FROM blobs WHERE hash NOT IN (SELECT json_extract(before,'$.hash') FROM changes "
                       "WHERE json_extract(before,'$.hash') IS NOT NULL UNION SELECT json_extract(after,'$.hash') "
                       "FROM changes WHERE json_extract(after,'$.hash') IS NOT NULL)")

    def sessions(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT session, MAX(updated) AS updated, "
                    "COUNT(*) AS runs FROM runs GROUP BY session ORDER BY updated DESC LIMIT 100")]

    def view(self, session, run="latest"):
        with self.connect() as db:
            runs = [dict(r) for r in db.execute("SELECT * FROM runs WHERE session=? ORDER BY started DESC LIMIT 100", (session,))]
            selected = runs[0]["run"] if run == "latest" and runs else run
            params = [session]
            where = "session=?"
            if selected != "all":
                where += " AND run=?"
                params.append(selected)
            rows = db.execute("SELECT * FROM changes WHERE " + where + " ORDER BY id LIMIT 10001", params).fetchall()
            groups = {}
            for row in rows[:10000]:
                old, new = json.loads(row["before"]), json.loads(row["after"])
                entry = groups.setdefault(row["path"], {"path": row["path"], "first_id": row["id"], "last_id": row["id"],
                    "before": old, "after": old, "edits": 0, "observed": False, "gap": False})
                entry["gap"] |= entry["after"] != old
                entry.update(after=new, last_id=row["id"], edits=entry["edits"] + 1)
                entry["observed"] |= row["source"] == "observed"
            files = []
            for entry in groups.values():
                old, new = entry["before"], entry["after"]
                status = ("restored" if old == new else "added" if old["kind"] == "absent"
                          else "deleted" if new["kind"] == "absent" else "modified")
                files.append(dict(entry, status=status))
            notes = list(dict.fromkeys(note for r in runs if selected == "all" or r["run"] == selected
                                      for note in json.loads(r["notes"])))
            if len(rows) > 10000:
                notes.append("Showing the first 10,000 changes in this scope. Select a single run for a smaller view.")
            return {"session": session, "run": selected, "runs": [{k: v for k, v in r.items() if k != "notes"} for r in runs],
                    "files": files, "notes": notes}

    def diff(self, session, first_id, last_id):
        with self.connect() as db:
            first = db.execute("SELECT * FROM changes WHERE id=? AND session=?", (first_id, session)).fetchone()
            last = db.execute("SELECT * FROM changes WHERE id=? AND session=?", (last_id, session)).fetchone()
            if not first or not last or first["path"] != last["path"] or first_id > last_id:
                raise LookupError("Recorded change not found")
            old, new = json.loads(first["before"]), json.loads(last["after"])
            if any(m["kind"] not in {"text", "absent"} for m in (old, new)):
                return {"patch": "", "note": "Binary or large file. Content diff unavailable; metadata was recorded.", "added": 0, "removed": 0}
            contents = []
            for meta in (old, new):
                row = db.execute("SELECT data FROM blobs WHERE hash=?", (meta.get("hash", ""),)).fetchone()
                if meta["kind"] == "text" and row is None:
                    raise LookupError("Snapshot content is no longer available")
                contents.append(row[0].decode("utf-8") if row else "")
            # Keep SequenceMatcher bounded even for pathological repeated-line files.
            if sum(len(s.splitlines()) for s in contents) > 12000:
                return {"patch": "", "note": "This file exceeds the 12,000-line diff limit.", "added": 0, "removed": 0}
            root = db.execute("SELECT cwd FROM runs WHERE session=? AND run=?", (session, first["run"])).fetchone()
            try:
                label = Path(first["path"]).relative_to(root[0]).as_posix() if root and root[0] else first["path"]
            except ValueError:
                label = first["path"]
            label = label.replace("\n", "\\n").replace("\r", "\\r")
            lines = list(difflib.unified_diff(contents[0].splitlines(keepends=True), contents[1].splitlines(keepends=True),
                        fromfile="/dev/null" if old["kind"] == "absent" else "a/" + label,
                        tofile="/dev/null" if new["kind"] == "absent" else "b/" + label))
            rendered = []
            for line in lines:
                rendered.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
            note = ""
            if not lines:
                if old["kind"] == "absent" and new["kind"] != "absent":
                    note = "An empty file was added. There are no text lines to compare."
                elif new["kind"] == "absent" and old["kind"] != "absent":
                    note = "An empty file was deleted. There are no text lines to compare."
                elif old.get("mode") != new.get("mode"):
                    note = "File mode changed."
            return {"patch": "".join(rendered), "note": note,
                    "added": sum(l.startswith("+") for l in lines[2:]),
                    "removed": sum(l.startswith("-") for l in lines[2:])}

    def history(self, session, first_id, last_id):
        with self.connect() as db:
            row = db.execute("SELECT path FROM changes WHERE session=? AND id=?", (session, first_id)).fetchone()
            if not row:
                raise LookupError("Recorded change not found")
            return [dict(r) for r in db.execute("SELECT id,run,tool,source,created FROM changes WHERE session=? "
                    "AND path=? AND id>=? AND id<=? ORDER BY id DESC LIMIT 100", (session, row[0], first_id, last_id))]
