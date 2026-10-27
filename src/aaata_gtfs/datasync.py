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
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy import func, select
from sqlmodel import Session

from aaata_gtfs import db
from aaata_gtfs import models as m
from aaata_gtfs.config import BLOBS_DIR, REPO_ROOT, blob_path, db_path

log = logging.getLogger(__name__)

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
    """`Remote` over the `rclone` command; `root` is an rclone path such as `aaata:aaata-gtfs`.

    Transfers log rclone's one-line progress every `stats_s` seconds; `verbose`
    also logs each file copied.
    """

    def __init__(self, root: str, env: Mapping[str, str] | None = None, binary: str = "rclone",
                 verbose: bool = False, stats_s: int = 15) -> None:
        if shutil.which(binary) is None:
            raise SyncError(f"{binary} not found; install it (https://rclone.org/install/)")
        self.root = root.rstrip("/")
        self.env = dict(os.environ if env is None else env)
        self.binary = binary
        self.global_args: list[str] = []
        # A remote defined in the environment needs no config file: say so, or rclone
        # prints a "config file not found" notice on every call.
        name = self.root.split(":", 1)[0].upper()
        if f"RCLONE_CONFIG_{name}_TYPE" in self.env and "RCLONE_CONFIG" not in self.env:
            self.global_args += ["--config", ""]
        self.transfer_args = ["--stats", f"{stats_s}s", "--stats-one-line", "--stats-log-level", "NOTICE"]
        if verbose:
            self.transfer_args.append("-v")

    def _path(self, name: str) -> str:
        return f"{self.root}/{name}"

    def _run(self, *args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
        command = [self.binary, *self.global_args, *args]
        log.debug("running %s", " ".join(command))
        return subprocess.run(command, env=self.env, check=True, text=True, capture_output=capture)

    def upload(self, local: Path, name: str) -> None:
        self._run("copyto", *self.transfer_args, str(local), self._path(name))

    def download(self, name: str, local: Path) -> None:
        self._run("copyto", *self.transfer_args, self._path(name), str(local))

    def read_text(self, name: str) -> str | None:
        try:
            out = self._run("cat", self._path(name), capture=True).stdout
        except subprocess.CalledProcessError as e:
            if e.returncode in (3, 4):  # directory / file not found
                return None
            raise SyncError(f"rclone cat {self._path(name)} failed: {e.stderr.strip()}") from e
        return out or None

    # --fast-list lists the remote tree in a few requests rather than one per
    # directory (the realtime archive has one per feed and day).
    def upload_dir(self, local: Path, name: str, exclude: Sequence[str] = ()) -> None:
        # The current hour's realtime archive is appended to while it uploads; the
        # next push copies it whole.
        self._run("copy", *self.transfer_args, "--fast-list", "--local-no-check-updated",
                  str(local), self._path(name), *_excludes(exclude))

    def download_dir(self, name: str, local: Path, exclude: Sequence[str] = ()) -> None:
        self._run("copy", *self.transfer_args, "--fast-list", self._path(name), str(local),
                  *_excludes(exclude))


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


def _mb(path: Path) -> str:
    return f"{path.stat().st_size / 1e6:.1f} MB"


@contextmanager
def _step(what: str) -> Iterator[None]:
    """Log `what` as it starts, and how long it took."""
    log.info("%s...", what)
    t0 = time.monotonic()
    yield
    log.info("%s: done in %.1f s", what, time.monotonic() - t0)


def _blob_summary(root: Path, exclude: Sequence[str]) -> str:
    files = [p for p in root.rglob("*") if p.is_file() and p.relative_to(root).parts[0] not in exclude]
    return f"{len(files)} files, {sum(p.stat().st_size for p in files) / 1e6:.0f} MB"


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
    with _step("reading the remote manifest"):
        current = remote_manifest(remote)
    if current is not None and _is_newer(current.latest_fetched_at, latest) and not force:
        raise SyncError(f"the remote copy (pushed from {current.host}) has newer data than {src}; "
                        "pass --force to replace it anyway")
    snapshot = data_path / ".push-snapshot.sqlite3"
    packed = data_path / ".push-snapshot.sqlite3.gz"
    try:
        with _step(f"snapshotting {src} ({_mb(src)})"):
            snapshot_db(src, snapshot)
        with _step(f"compressing the snapshot ({_mb(snapshot)})"):
            with snapshot.open("rb") as f_in, gzip.open(packed, "wb", compresslevel=6) as f_out:
                shutil.copyfileobj(f_in, f_out)
        manifest = Manifest(latest, time.time(), socket.gethostname(), snapshot.stat().st_size)
        with _step(f"uploading the database ({_mb(packed)} compressed)"):
            remote.upload(packed, REMOTE_DB)
        manifest_file = data_path / ".push-manifest.json"
        manifest_file.write_text(json.dumps(asdict(manifest), indent=2) + "\n")
        try:
            with _step("uploading the manifest"):
                remote.upload(manifest_file, REMOTE_MANIFEST)
        finally:
            manifest_file.unlink(missing_ok=True)
    finally:
        snapshot.unlink(missing_ok=True)
        packed.unlink(missing_ok=True)
    if blobs and blob_path(data_path).exists():
        exclude = () if rt_blobs else (RT_BLOBS,)
        with _step(f"copying new blobs (of {_blob_summary(blob_path(data_path), exclude)} here)"):
            remote.upload_dir(blob_path(data_path), BLOBS_DIR, exclude=exclude)
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
    with _step("reading the remote manifest"):
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
        log.info("the local database is already the remote copy; not downloading it")
        result = None
    else:
        packed = data_path / ".pull.sqlite3.gz"
        unpacked = data_path / ".pull.sqlite3"
        try:
            with _step("downloading the database"):
                remote.download(REMOTE_DB, packed)
            with _step(f"decompressing it ({_mb(packed)})"):
                with gzip.open(packed, "rb") as f_in, unpacked.open("wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
            install_db(unpacked, dest)
            log.info("replaced %s (%s)", dest, _mb(dest))
        finally:
            packed.unlink(missing_ok=True)
            unpacked.unlink(missing_ok=True)
    if blobs:
        with _step("copying new blobs" + ("" if rt_blobs else f" (not {RT_BLOBS}/)")):
            remote.download_dir(BLOBS_DIR, blob_path(data_path), exclude=() if rt_blobs else (RT_BLOBS,))
    return result
