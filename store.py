"""SQLite storage for jobs, milestones and the activity log.

Money is always stored as integer cents. Every job gets three secret links
(homeowner, contractor, trusted helper); each link only unlocks that role's actions.
"""
import json
import os
import secrets
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    description   TEXT NOT NULL,
    homeowner     TEXT NOT NULL,
    contractor    TEXT NOT NULL,
    helper        TEXT NOT NULL,
    total_cents   INTEGER NOT NULL,
    owner_key     TEXT NOT NULL,
    contractor_key TEXT NOT NULL,
    helper_key    TEXT NOT NULL,
    safety_tips   TEXT NOT NULL DEFAULT '[]',
    created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS milestones (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id           INTEGER NOT NULL REFERENCES jobs(id),
    position         INTEGER NOT NULL,
    title            TEXT NOT NULL,
    proof_needed     TEXT NOT NULL,
    amount_cents     INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'planned',
    order_id         TEXT,
    authorization_id TEXT,
    capture_id       TEXT,
    photo_file       TEXT,
    ai_verdict       TEXT,
    ai_note          TEXT,
    updated_at       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL,
    actor      TEXT NOT NULL,
    message    TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""

# Allowed status changes. Anything else is refused, so money can't skip a step.
TRANSITIONS = {
    "planned": {"pending_approval", "held"},
    "pending_approval": {"held", "planned"},
    "held": {"submitted", "voided"},
    "submitted": {"released", "voided", "submitted"},
    "released": set(),
    "voided": set(),
}


class InvalidTransition(Exception):
    pass


class Store:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ---------- jobs ----------
    def create_job(self, title, description, homeowner, contractor, helper,
                   total_cents, milestones, safety_tips):
        now = time.time()
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO jobs (title, description, homeowner, contractor, helper,"
                " total_cents, owner_key, contractor_key, helper_key, safety_tips, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (title, description, homeowner, contractor, helper, total_cents,
                 secrets.token_urlsafe(16), secrets.token_urlsafe(16),
                 secrets.token_urlsafe(16), json.dumps(safety_tips), now),
            )
            job_id = cur.lastrowid
            for i, m in enumerate(milestones, start=1):
                c.execute(
                    "INSERT INTO milestones (job_id, position, title, proof_needed,"
                    " amount_cents, updated_at) VALUES (?,?,?,?,?,?)",
                    (job_id, i, m["title"], m["proof_needed"], m["amount_cents"], now),
                )
        self.log(job_id, "LocalPro Pay", f"Job created with {len(milestones)} steps.")
        return self.get_job(job_id)

    def get_job(self, job_id):
        with self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            job = dict(row)
            job["safety_tips"] = json.loads(job["safety_tips"])
            job["milestones"] = [
                self._milestone_dict(r) for r in c.execute(
                    "SELECT * FROM milestones WHERE job_id=? ORDER BY position", (job_id,))
            ]
            job["events"] = [dict(r) for r in c.execute(
                "SELECT actor, message, created_at FROM events WHERE job_id=? ORDER BY id DESC",
                (job_id,))]
        return job

    # ---------- milestones ----------
    @staticmethod
    def _milestone_dict(row):
        m = dict(row)
        m["ai_verdict"] = json.loads(m["ai_verdict"]) if m["ai_verdict"] else None
        return m

    def get_milestone(self, milestone_id):
        with self._conn() as c:
            row = c.execute("SELECT * FROM milestones WHERE id=?", (milestone_id,)).fetchone()
        return self._milestone_dict(row) if row else None

    def find_by_order(self, order_id):
        with self._conn() as c:
            row = c.execute("SELECT * FROM milestones WHERE order_id=?", (order_id,)).fetchone()
        return self._milestone_dict(row) if row else None

    def update_milestone(self, milestone_id, new_status=None, **fields):
        with self._lock, self._conn() as c:
            row = c.execute("SELECT status FROM milestones WHERE id=?", (milestone_id,)).fetchone()
            if not row:
                raise KeyError(milestone_id)
            if new_status is not None:
                if new_status not in TRANSITIONS[row["status"]]:
                    raise InvalidTransition(f"Can't go from {row['status']} to {new_status}.")
                fields["status"] = new_status
            if "ai_verdict" in fields and fields["ai_verdict"] is not None:
                fields["ai_verdict"] = json.dumps(fields["ai_verdict"])
            fields["updated_at"] = time.time()
            cols = ", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE milestones SET {cols} WHERE id=?", (*fields.values(), milestone_id))
        return self.get_milestone(milestone_id)

    # ---------- activity log ----------
    def log(self, job_id, actor, message):
        with self._conn() as c:
            c.execute("INSERT INTO events (job_id, actor, message, created_at) VALUES (?,?,?,?)",
                      (job_id, actor, message, time.time()))
