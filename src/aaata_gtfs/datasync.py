"""Copying the data directory between machines through an rclone remote (a
Google Drive or OneDrive folder, say), for machines that can't reach each other.

`push` uploads a consistent snapshot of the database (SQLite's backup API, so
the collector keeps writing), gzipped, plus a small JSON manifest, and copies
the blobs (write-once, so only new ones transfer). `pull` downloads them and
replaces the local database. Both compare the newest `feed_snapshot.fetched_at`
on each side and refuse to replace newer data with older, so pulling on the
collector's machine, or pushing from a copy, fails instead of losing data.

The remote is configured without an rclone config file, through the
environment (`RCLONE_CONFIG_<NAME>_<KEY>`), usually from the gitignored `.env`
(`read_env_file`); `AAATA_GTFS_REMOTE` names the folder, e.g.
`aaata:aaata-gtfs`.
"""

import gzip
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy import func, select
from sqlmodel import Session

from aaata_gtfs import db
from aaata_gtfs import models as m
from aaata_gtfs.config import BLOBS_DIR, REPO_ROOT, blob_path, db_path

ENV_FILE = REPO_ROOT / ".env"
#: The rclone path of the folder holding the copy, e.g. `aaata:aaata-gtfs`.
REMOTE_ENV = "AAATA_GTFS_REMOTE"
REMOTE_DB = "aaata_gtfs.sqlite3.gz"
REMOTE_MANIFEST = "manifest.json"
#: Raw realtime archives: the bulk of the blobs, and not needed to fit models.
RT_BLOBS = "rt"


class SyncError(Exception):
    """A push or pull that would lose data, or a remote that isn't set up."""


@dataclass(frozen=True)
class Manifest:
    """What a pushed copy holds."""

    #: The newest `feed_snapshot.fetched_at`, or None for an empty database.
    latest_fetched_at: float | None
    pushed_at: float
    host: str
    db_bytes: int


class Remote(Protocol):
    """A folder that files are copied to and from (`Rclone`, or a fake in tests)."""

    def upload(self, local: Path, name: str) -> None: ...
    def download(self, name: str, local: Path) -> None: ...
    def read_text(self, name: str) -> str | None: ...
    def upload_dir(self, local: Path, name: str, exclude: Sequence[str] = ()) -> None: ...
    def download_dir(self, name: str, local: Path, exclude: Sequence[str] = ()) -> None: ...


class Rclone:
    """`Remote` over the `rclone` command; `root` is an rclone path such as `aaata:aaata-gtfs`."""

    def __init__(self, root: str, env: Mapping[str, str] | None = None, binary: str = "rclone") -> None:
        if shutil.which(binary) is None:
            raise SyncError(f"{binary} not found; install it (https://rclone.org/install/)")
        self.root = root.rstrip("/")
        self.env = dict(os.environ if env is None else env)
        self.binary = binary

    def _path(self, name: str) -> str:
        return f"{self.root}/{name}"

    def _run(self, *args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run([self.binary, *args], env=self.env, check=True, text=True,
                              capture_output=capture)

    def upload(self, local: Path, name: str) -> None:
        self._run("copyto", str(local), self._path(name))

    def download(self, name: str, local: Path) -> None:
        self._run("copyto", self._path(name), str(local))

    def read_text(self, name: str) -> str | None:
        try:
            out = self._run("cat", self._path(name), capture=True).stdout
        except subprocess.CalledProcessError as e:
            if e.returncode in (3, 4):  # directory / file not found
                return None
            raise SyncError(f"rclone cat {self._path(name)} failed: {e.stderr.strip()}") from e
        return out or None

    def upload_dir(self, local: Path, name: str, exclude: Sequence[str] = ()) -> None:
        # The current hour's realtime archive is appended to while it uploads; the
        # next push copies it whole.
        self._run("copy", "--local-no-check-updated", str(local), self._path(name), *_excludes(exclude))

    def download_dir(self, name: str, local: Path, exclude: Sequence[str] = ()) -> None:
        self._run("copy", self._path(name), str(local), *_excludes(exclude))


def _excludes(dirs: Sequence[str]) -> list[str]:
    return [arg for d in dirs for arg in ("--exclude", f"{d}/**")]


def read_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    """`KEY=value` lines of a dotenv file (as docker compose reads it: `#` comments,
    optional `export`, values optionally in single or double quotes). Missing file → {}."""
    if not path.exists():
        return {}
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        env[key.strip()] = value
    return env


def environment(path: Path = ENV_FILE) -> dict[str, str]:
    """The process environment over the `.env` file (set variables win)."""
    return {**read_env_file(path), **os.environ}


def latest_fetched_at(path: Path) -> float | None:
    """The newest feed snapshot's fetch time in the database at `path` (None if it has none)."""
    engine = db.make_engine(path, read_only=True)
    try:
        with Session(engine) as session:
            value = db.rows(session, select(func.max(m.columns(m.FeedSnapshot).fetched_at))).scalar_one()
    finally:
        engine.dispose()
    return None if value is None else float(value)


def snapshot_db(src: Path, dest: Path) -> None:
    """A consistent copy of `src` at `dest` while it may be written to, as one
    self-contained file (rollback journal rather than WAL; the collector's
    engine turns WAL back on when it opens it)."""
    dest.unlink(missing_ok=True)
    source = sqlite3.connect(f"file:{src.resolve()}?mode=ro", uri=True)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
        target.execute("PRAGMA journal_mode = DELETE")
    finally:
        target.close()
        source.close()


def _is_newer(a: float | None, b: float | None) -> bool:
    return a is not None and (b is None or a > b)


def remote_manifest(remote: Remote) -> Manifest | None:
    text = remote.read_text(REMOTE_MANIFEST)
    return None if text is None else Manifest(**json.loads(text))


def push(data_path: Path, remote: Remote, blobs: bool = True, rt_blobs: bool = True,
         force: bool = False) -> Manifest:
    """Upload a snapshot of the database (and the blobs) to `remote`.

    Refuses (`SyncError`) if the remote copy has newer data than this one,
    unless `force`.
    """
    src = db_path(data_path)
    if not src.exists():
        raise SyncError(f"no database at {src}")
    latest = latest_fetched_at(src)
    current = remote_manifest(remote)
    if current is not None and _is_newer(current.latest_fetched_at, latest) and not force:
        raise SyncError(f"the remote copy (pushed from {current.host}) has newer data than {src}; "
                        "pass --force to replace it anyway")
    snapshot = data_path / ".push-snapshot.sqlite3"
    packed = data_path / ".push-snapshot.sqlite3.gz"
    try:
        snapshot_db(src, snapshot)
        with snapshot.open("rb") as f_in, gzip.open(packed, "wb", compresslevel=6) as f_out:
            shutil.copyfileobj(f_in, f_out)
        manifest = Manifest(latest, time.time(), socket.gethostname(), snapshot.stat().st_size)
        remote.upload(packed, REMOTE_DB)
        manifest_file = data_path / ".push-manifest.json"
        manifest_file.write_text(json.dumps(asdict(manifest), indent=2) + "\n")
        try:
            remote.upload(manifest_file, REMOTE_MANIFEST)
        finally:
            manifest_file.unlink(missing_ok=True)
    finally:
        snapshot.unlink(missing_ok=True)
        packed.unlink(missing_ok=True)
    if blobs and blob_path(data_path).exists():
        remote.upload_dir(blob_path(data_path), BLOBS_DIR, exclude=() if rt_blobs else (RT_BLOBS,))
    return manifest


def install_db(new: Path, dest: Path) -> None:
    """Replace the database at `dest` with the file `new` (same filesystem),
    removing `dest`'s WAL and shared-memory files so they aren't applied to it."""
    for suffix in ("-wal", "-shm"):
        Path(f"{dest}{suffix}").unlink(missing_ok=True)
    os.replace(new, dest)


def pull(data_path: Path, remote: Remote, blobs: bool = True, rt_blobs: bool = True,
         force: bool = False) -> Manifest | None:
    """Replace the local database with the remote copy (and copy new blobs).

    Refuses (`SyncError`) if the local database has newer data than the remote
    copy (e.g. this is the collector's machine), unless `force`. Returns the
    remote manifest, or None if the local database was already that copy.
    Don't pull while something has the local database open.
    """
    manifest = remote_manifest(remote)
    if manifest is None:
        raise SyncError("nothing has been pushed to the remote yet")
    dest = db_path(data_path)
    data_path.mkdir(parents=True, exist_ok=True)
    local = latest_fetched_at(dest) if dest.exists() else None
    if _is_newer(local, manifest.latest_fetched_at) and not force:
        raise SyncError(f"{dest} has newer data than the remote copy (pushed from {manifest.host}); "
                        "pass --force to replace it anyway")
    result: Manifest | None = manifest
    if dest.exists() and local == manifest.latest_fetched_at and not force:
        result = None
    else:
        packed = data_path / ".pull.sqlite3.gz"
        unpacked = data_path / ".pull.sqlite3"
        try:
            remote.download(REMOTE_DB, packed)
            with gzip.open(packed, "rb") as f_in, unpacked.open("wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            install_db(unpacked, dest)
        finally:
            packed.unlink(missing_ok=True)
            unpacked.unlink(missing_ok=True)
    if blobs:
        remote.download_dir(BLOBS_DIR, blob_path(data_path), exclude=() if rt_blobs else (RT_BLOBS,))
    return result
