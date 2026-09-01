from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from langgraph.checkpoint.sqlite import SqliteSaver


@contextmanager
def open_sqlite_checkpointer(path: Path | str) -> Iterator[SqliteSaver]:
    """Open a local durable checkpointer; no cloud/Studio dependency."""
    path_text = str(path)
    if path_text != ":memory:":
        db_path = Path(path).resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        path_text = str(db_path)
    with sqlite3.connect(path_text, check_same_thread=False) as connection:
        saver = SqliteSaver(connection)
        saver.setup()
        yield saver
