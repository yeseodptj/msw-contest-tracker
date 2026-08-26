from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

DB_PATH = Path(__file__).with_name("tracker.db")

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS worlds (
    world_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    creator TEXT,
    genre TEXT,
    release_date TEXT,
    updated_date TEXT,
    url TEXT NOT NULL,
    thumbnail_url TEXT,
    max_players INTEGER,
    platforms TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    is_contest INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    world_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    total_players INTEGER,
    favorites INTEGER,
    likes_count INTEGER,
    likes_rate REAL,
    comments INTEGER,
    FOREIGN KEY(world_id) REFERENCES worlds(world_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_world_time
ON snapshots(world_id, observed_at);
"""


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def init_db(db_path: str | Path | None = None) -> None:
    with connect(db_path) as con:
        con.executescript(SCHEMA)


def upsert_world(con: sqlite3.Connection, world: dict) -> None:
    con.execute(
        """
        INSERT INTO worlds (
            world_id, name, creator, genre, release_date, updated_date, url,
            thumbnail_url, max_players, platforms, first_seen, last_seen, is_contest
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(world_id) DO UPDATE SET
            name=excluded.name,
            creator=COALESCE(excluded.creator, worlds.creator),
            genre=COALESCE(excluded.genre, worlds.genre),
            release_date=COALESCE(excluded.release_date, worlds.release_date),
            updated_date=COALESCE(excluded.updated_date, worlds.updated_date),
            url=excluded.url,
            thumbnail_url=COALESCE(excluded.thumbnail_url, worlds.thumbnail_url),
            max_players=COALESCE(excluded.max_players, worlds.max_players),
            platforms=COALESCE(excluded.platforms, worlds.platforms),
            last_seen=excluded.last_seen,
            is_contest=1
        """,
        (
            world["world_id"], world["name"], world.get("creator"), world.get("genre"),
            world.get("release_date"), world.get("updated_date"), world["url"],
            world.get("thumbnail_url"), world.get("max_players"), world.get("platforms"),
            world["observed_at"], world["observed_at"],
        ),
    )


def insert_snapshot(con: sqlite3.Connection, world: dict) -> None:
    con.execute(
        """
        INSERT INTO snapshots (
            world_id, observed_at, total_players, favorites, likes_count, likes_rate, comments
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            world["world_id"], world["observed_at"], world.get("total_players"),
            world.get("favorites"), world.get("likes_count"), world.get("likes_rate"),
            world.get("comments"),
        ),
    )


def save_batch(worlds: Iterable[dict], db_path: str | Path | None = None) -> int:
    init_db(db_path)
    count = 0
    with connect(db_path) as con:
        for world in worlds:
            upsert_world(con, world)
            insert_snapshot(con, world)
            count += 1
        con.commit()
    return count
