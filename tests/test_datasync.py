import json
import shutil
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlmodel import Session

from aaata_gtfs import db
from aaata_gtfs import models as m
from aaata_gtfs.config import db_path
from aaata_gtfs.datasync import (
    REMOTE_DB,
    REMOTE_MANIFEST,
    SyncError,
    latest_fetched_at,
    pull,
    push,
    read_env_file,
    snapshot_db,
)


class FolderRemote:
    """A `Remote` that is a local directory (what rclone does, without rclone)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def upload(self, local: Path, name: str) -> None:
        shutil.copyfile(local, self.root / name)

    def download(self, name: str, local: Path) -> None:
        shutil.copyfile(self.root / name, local)

    def read_text(self, name: str) -> str | None:
        path = self.root / name
        return path.read_text() if path.exists() else None

    def upload_dir(self, local: Path, name: str, exclude: Sequence[str] = ()) -> None:
        _copy_tree(local, self.root / name, exclude)

    def download_dir(self, name: str, local: Path, exclude: Sequence[str] = ()) -> None:
        if (self.root / name).exists():
            _copy_tree(self.root / name, local, exclude)


def _copy_tree(src: Path, dest: Path, exclude: Sequence[str]) -> None:
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        if path.is_file() and rel.parts[0] not in exclude:
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest / rel)


def close(session: Session) -> None:
    session.close()
    engine = session.get_bind()
    assert isinstance(engine, Engine)
    engine.dispose()


def make_data(data_path: Path, fetched_at: Sequence[float]) -> None:
    """A data directory whose database has feed snapshots fetched at these times, and two blobs."""
    data_path.mkdir(parents=True, exist_ok=True)
    session = db.open_db(db_path(data_path))
    with db.write(session):
        run = m.CollectorRun(started_at=0.0, version="test", args="{}")
        session.add(run)
        session.flush()
        assert run.id is not None
        for i, t in enumerate(fetched_at):
            session.add(m.FeedSnapshot(feed="vehicles", header_ts=int(t) - 3, fetched_at=t, sha256=f"{i}",
                                       n_entities=0, n_bytes=0, run_id=run.id))
    close(session)
    for blob in ("rt/vehicles/a.bin", "static/s.zip"):
        (data_path / "blobs" / blob).parent.mkdir(parents=True, exist_ok=True)
        (data_path / "blobs" / blob).write_bytes(blob.encode())


def test_read_env_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("\n".join([
        "# comment",
        "UID=1000",
        "export AAATA_GTFS_REMOTE=aaata:aaata-gtfs  # the folder",
        """RCLONE_CONFIG_AAATA_TOKEN='{"access_token":"x","expiry":"2026 #1"}'""",
        'QUOTED="a b"',
        "",
        "not a setting",
    ]))
    assert read_env_file(env) == {
        "UID": "1000",
        "AAATA_GTFS_REMOTE": "aaata:aaata-gtfs",
        "RCLONE_CONFIG_AAATA_TOKEN": '{"access_token":"x","expiry":"2026 #1"}',
        "QUOTED": "a b",
    }
    assert read_env_file(tmp_path / "missing") == {}


def test_snapshot_includes_uncheckpointed_writes(tmp_path: Path) -> None:
    make_data(tmp_path / "src", [100.0, 200.0])
    # Keep a writer open with its last write still only in the WAL.
    writer = sqlite3.connect(db_path(tmp_path / "src"))
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    writer.execute("UPDATE feed_snapshot SET fetched_at = 300.0 WHERE fetched_at = 200.0")
    writer.commit()
    assert Path(f"{db_path(tmp_path / 'src')}-wal").stat().st_size > 0
    snapshot_db(db_path(tmp_path / "src"), tmp_path / "copy.sqlite3")
    writer.close()
    copy = sqlite3.connect(tmp_path / "copy.sqlite3")
    assert copy.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    copy.close()
    assert latest_fetched_at(tmp_path / "copy.sqlite3") == 300.0


def test_push_then_pull(tmp_path: Path) -> None:
    pi, campus = tmp_path / "pi", tmp_path / "campus"
    remote = FolderRemote(tmp_path / "remote")
    make_data(pi, [100.0, 200.0])
    manifest = push(pi, remote)
    assert manifest.latest_fetched_at == 200.0
    assert json.loads((tmp_path / "remote" / REMOTE_MANIFEST).read_text())["latest_fetched_at"] == 200.0
    assert (tmp_path / "remote" / REMOTE_DB).exists()
    assert not list(pi.glob(".push*"))

    pulled = pull(campus, remote, rt_blobs=False)
    assert pulled is not None and pulled.latest_fetched_at == 200.0
    assert latest_fetched_at(db_path(campus)) == 200.0
    assert (campus / "blobs" / "static" / "s.zip").exists()
    assert not (campus / "blobs" / "rt").exists()
    assert not list(campus.glob(".pull*"))
    # A migrated read-write open of the pulled copy works.
    close(db.open_db(db_path(campus)))

    # Nothing new: the database isn't downloaded again.
    assert pull(campus, remote) is None
    # New data on the Pi replaces the campus copy, and its stale WAL files.
    make_data(pi, [300.0])
    push(pi, remote)
    Path(f"{db_path(campus)}-wal").write_bytes(b"stale")
    pulled = pull(campus, remote)
    assert pulled is not None and pulled.latest_fetched_at == 300.0
    assert latest_fetched_at(db_path(campus)) == 300.0
    assert not Path(f"{db_path(campus)}-wal").exists()


def test_refuses_to_replace_newer_data(tmp_path: Path) -> None:
    pi, campus = tmp_path / "pi", tmp_path / "campus"
    remote = FolderRemote(tmp_path / "remote")
    make_data(pi, [100.0])
    push(pi, remote)
    pull(campus, remote)
    make_data(pi, [200.0])
    # Pulling on the collector's machine would throw away its newer data.
    with pytest.raises(SyncError, match="newer data"):
        pull(pi, remote)
    assert latest_fetched_at(db_path(pi)) == 200.0
    push(pi, remote)
    # Pushing the older campus copy would replace the Pi's.
    with pytest.raises(SyncError, match="newer data"):
        push(campus, remote)
    assert json.loads((tmp_path / "remote" / REMOTE_MANIFEST).read_text())["latest_fetched_at"] == 200.0
    assert push(campus, remote, force=True).latest_fetched_at == 100.0


def test_pull_with_nothing_pushed(tmp_path: Path) -> None:
    with pytest.raises(SyncError, match="nothing has been pushed"):
        pull(tmp_path / "campus", FolderRemote(tmp_path / "remote"))
