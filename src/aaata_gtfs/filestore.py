"""The raw blob archive under `<data path>/blobs/`.

```
blobs/rt/<feed>/<YYYY>/<MM>/<DD>/<HH>.pbs.gz       hour (UTC) of the header timestamp
blobs/static/<content_id>.zip
blobs/bustime/getroutebyid/<route_id>/<sha12>.json.gz
```

RT hourly files are appended gzip members, one per stored snapshot. Each record
is an 8-byte big-endian header timestamp, a 4-byte big-endian payload length,
then the raw protobuf payload. Paths returned (and stored in the DB) are
relative to the data path.
"""

import gzip
import os
import struct
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from aaata_gtfs.config import BLOBS_DIR, DATA_PATH

_RECORD_HEADER = struct.Struct(">QI")


def rt_relpath(feed: str, header_ts: int) -> str:
    t = datetime.fromtimestamp(header_ts, timezone.utc)
    return f"{BLOBS_DIR}/rt/{feed}/{t:%Y/%m/%d/%H}.pbs.gz"


def append_rt(feed: str, header_ts: int, payload: bytes, data_path: Path = DATA_PATH) -> str:
    """Append one snapshot to its hourly archive file; returns the file's relpath."""
    relpath = rt_relpath(feed, header_ts)
    path = data_path / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "ab") as f:
        f.write(_RECORD_HEADER.pack(header_ts, len(payload)) + payload)
    return relpath


def iter_rt(path: Path | str) -> Iterator[tuple[int, bytes]]:
    """(header_ts, payload) for each snapshot in an hourly archive file, in order."""
    with gzip.open(path, "rb") as f:
        while True:
            head = f.read(_RECORD_HEADER.size)
            if not head:
                return
            if len(head) < _RECORD_HEADER.size:
                raise ValueError(f"{path}: truncated record header")
            header_ts, length = _RECORD_HEADER.unpack(head)
            payload = f.read(length)
            if len(payload) < length:
                raise ValueError(f"{path}: truncated record at header_ts={header_ts}")
            yield header_ts, payload


def _write_once(path: Path, data: bytes) -> None:
    """Write `data` to `path` atomically, unless the file already exists."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def static_relpath(content_id: str) -> str:
    return f"{BLOBS_DIR}/static/{content_id}.zip"


def put_static(content_id: str, zip_bytes: bytes, data_path: Path = DATA_PATH) -> str:
    relpath = static_relpath(content_id)
    _write_once(data_path / relpath, zip_bytes)
    return relpath


def bustime_relpath(route_id: str, sha256: str) -> str:
    return f"{BLOBS_DIR}/bustime/getroutebyid/{route_id}/{sha256[:12]}.json.gz"


def put_bustime(route_id: str, sha256: str, payload: bytes, data_path: Path = DATA_PATH) -> str:
    """Store a getroutebyid response body (gzipped), named by its canonical hash."""
    relpath = bustime_relpath(route_id, sha256)
    _write_once(data_path / relpath, gzip.compress(payload, mtime=0))
    return relpath


def read_blob(relpath: str, data_path: Path = DATA_PATH) -> bytes:
    """A stored static zip or BusTime payload (decompressed if gzipped)."""
    data = (data_path / relpath).read_bytes()
    return gzip.decompress(data) if relpath.endswith(".gz") else data
