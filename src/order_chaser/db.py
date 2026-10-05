"""SQLite: one row per chase (its state as JSON) and an append-only event log keyed by cl_ord_id."""
from __future__ import annotations

import fcntl
import logging
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
create table if not exists leg (
  id text primary key, chase_id text not null references chase(id));
create table if not exists setting (
  key text primary key, value text not null);
create table if not exists sim_account (
  id integer primary key check (id = 1), state text not null);
"""


log = logging.getLogger("order_chaser")


def lock_folder(folder: Path):
    """Make the data folder if it is missing, and hold an exclusive lock on it for the life of the process.

    The tool makes a missing folder with mode 0700. It never changes the mode of a folder that is
    there already (it can be a folder such as ~/Documents); it warns when others can read it.
    A second tool on the same folder would end the first tool's chase as "ended" while it still runs.
    Raises BlockingIOError when another process holds the lock.
    """
    try:
        folder.parent.mkdir(parents=True, exist_ok=True)
        folder.mkdir(mode=0o700)
    except FileExistsError:
        mode = folder.stat().st_mode & 0o777
        if mode & 0o077:
            log.warning(f"The data folder {folder} is readable by other users (mode {mode:o}). The tool did not change it.")
    f = open(folder / "lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise
    return f


class Db:
    def __init__(self, folder: Path) -> None:
        """The folder must exist: lock_folder() makes it."""
        self.path = folder / "order-chaser.sqlite3"
        self.cx = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)  # one event loop uses it
        self.cx.row_factory = sqlite3.Row
        self.cx.executescript(SCHEMA)

    def save(self, c: core.Chase, mode: str, now: float) -> None:
        self.cx.execute(
            "insert into chase(id, created, mode, phase, outcome, updated, state) values (?,?,?,?,?,?,?) "
            "on conflict(id) do update set phase=excluded.phase, outcome=excluded.outcome, "
            "updated=excluded.updated, state=excluded.state",
            (c.id, c.started, mode, c.phase, c.outcome, now, core.to_json(c)))
        # Each leg's cl_ord_id leads to its chase (an execution report names the leg, not the chase).
        self.cx.executemany("insert or ignore into leg(id, chase_id) values (?, ?)",
                            [(leg, c.id) for leg in (*c.legs, c.ioc_id)])

    def chase_of(self, leg: str) -> str | None:
        """The chase id of a leg's cl_ord_id. A chase of an earlier build has no leg rows: its id is its only leg."""
        r = self.cx.execute("select chase_id from leg where id = ? union select id from chase where id = ?", (leg, leg)).fetchone()
        return r[0] if r else None

    def load_account(self) -> str | None:
        """The simulated margin account of the dry run, as JSON; None before the first margin chase."""
        r = self.cx.execute("select state from sim_account where id = 1").fetchone()
        return r[0] if r else None

    def save_account(self, state: str) -> None:
        self.cx.execute("insert into sim_account(id, state) values (1, ?) on conflict(id) do update set state=excluded.state", (state,))

    def setting(self, key: str) -> str | None:
        r = self.cx.execute("select value from setting where key = ?", (key,)).fetchone()
        return r[0] if r else None

    def set_setting(self, key: str, value: str | None) -> None:
        """None deletes the setting."""
        if value is None:
            self.cx.execute("delete from setting where key = ?", (key,))
        else:
            self.cx.execute("insert into setting(key, value) values (?, ?) on conflict(key) do update set value=excluded.value",
                            (key, value))

    def any_live(self) -> bool:
        """A live chase was started before (Q6: the first live order is marked one time)."""
        return self.cx.execute("select 1 from chase where mode = 'live' limit 1").fetchone() is not None

    def touch(self, chase_id: str, now: float) -> None:
        """Heartbeat: after a crash, "updated" tells when the tool last ran."""
        self.cx.execute("update chase set updated = ? where id = ?", (now, chase_id))

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
