"""SQLite: one row per chase (its state as JSON) and an append-only event log keyed by cl_ord_id."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from . import core

DEFAULT_DIR = Path.home() / "Library" / "Application Support" / "order-chaser"

SCHEMA = """
create table if not exists chase (
  id text primary key, created real not null, mode text not null,
  phase text not null, outcome text, updated real not null, state text not null);
create table if not exists event (
  seq integer primary key autoincrement, chase_id text not null references chase(id),
  t real not null, at real not null, kind text not null, text text not null);
create index if not exists event_chase on event(chase_id, seq);
"""


class Db:
    def __init__(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        self.path = folder / "order-chaser.sqlite3"
        self.cx = sqlite3.connect(self.path, isolation_level=None)
        self.cx.row_factory = sqlite3.Row
        self.cx.executescript(SCHEMA)

    def save(self, c: core.Chase, mode: str, now: float) -> None:
        self.cx.execute(
            "insert into chase(id, created, mode, phase, outcome, updated, state) values (?,?,?,?,?,?,?) "
            "on conflict(id) do update set phase=excluded.phase, outcome=excluded.outcome, "
            "updated=excluded.updated, state=excluded.state",
            (c.id, c.started, mode, c.phase, c.outcome, now, core.to_json(c)))

    def log(self, chase_id: str, entry: core.Log, at: float) -> None:
        self.cx.execute("insert into event(chase_id, t, at, kind, text) values (?,?,?,?,?)",
                        (chase_id, entry.t, at, entry.kind, entry.text))

    def unfinished(self) -> list[tuple[core.Chase, float]]:
        rows = self.cx.execute("select state, updated from chase where phase != 'done'").fetchall()
        return [(core.from_json(r["state"]), r["updated"]) for r in rows]

    def get(self, chase_id: str) -> tuple[core.Chase, str] | None:
        r = self.cx.execute("select state, mode from chase where id = ?", (chase_id,)).fetchone()
        return (core.from_json(r["state"]), r["mode"]) if r else None

    def events(self, chase_id: str) -> list[dict]:
        rows = self.cx.execute("select t, kind, text from event where chase_id = ? order by seq", (chase_id,))
        return [dict(r) for r in rows]

    def history(self) -> list[tuple[core.Chase, str]]:
        rows = self.cx.execute("select state, mode from chase where phase = 'done' order by created desc")
        return [(core.from_json(r["state"]), r["mode"]) for r in rows]
