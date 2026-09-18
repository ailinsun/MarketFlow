"""Rotation for active trade JSONL files.

Active writers keep appending to a stable active path. On the first append after
local-date rollover, the stale active file is moved to
<archive_root>/YYYY-MM-DD/. The same path is rotated when it exceeds the
active-size threshold. Gzip is started in the background. The active path is
recreated immediately so live readers and writers keep using the same filename.

Writers that need the full history back (offline analysis over a rotated
substrate) must read through ``iter_jsonl_lines``, which walks the archived
segments in write order before the active file. Reading the active path alone
sees only the current segment.
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator


from marketflow.paths import runtime_dir

RUNTIME_DIR = Path(runtime_dir())
ARCHIVE_ROOT = RUNTIME_DIR / "archive" / "feeds-daily"
LOCK_DIR = RUNTIME_DIR / "feeds" / ".locks"
GZIP_BIN = "/usr/bin/gzip"
DEFAULT_MAX_ACTIVE_BYTES = 256 * 1024 * 1024
# Compressed archive sizes are how much a segment costs on disk, not how much history
# it holds. JSONL of this kind compresses to a few percent of its size (ledgers
# around 4%, price ticks around 3%, alert rows under 1%), so 30x under-counts every
# one of them, and iter_jsonl_tail overshoots rather than returning short.
GZ_EXPANSION_ESTIMATE = 30
_GZIP_PROCESSES = set()


def _local_day(ts: float | None = None) -> str:
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d")


def _lock_path(active_path: Path) -> Path:
    # Stable filesystem label only; not an integrity or authentication check.
    digest = hashlib.sha1(
        str(active_path.resolve()).encode("utf-8"), usedforsecurity=False,
    ).hexdigest()[:12]
    return LOCK_DIR / f"{active_path.name}.{digest}.lock"


def _max_active_bytes() -> int:
    raw = os.environ.get("MARKETFLOW_FEED_MAX_ACTIVE_BYTES")
    if raw is None:
        return DEFAULT_MAX_ACTIVE_BYTES
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_MAX_ACTIVE_BYTES


def _archive_root(archive_root: str | os.PathLike[str] | None) -> Path:
    return ARCHIVE_ROOT if archive_root is None else Path(archive_root)


def _unique_archive_path(
    active_path: Path,
    day: str,
    segment: str | None = None,
    archive_root: str | os.PathLike[str] | None = None,
) -> Path:
    archive_dir = _archive_root(archive_root) / day
    archive_dir.mkdir(parents=True, exist_ok=True)
    stem = active_path.stem
    suffix = active_path.suffix
    if segment is None:
        candidate = archive_dir / active_path.name
    else:
        candidate = archive_dir / f"{stem}.{segment}{suffix}"
    if not candidate.exists() and not Path(str(candidate) + ".gz").exists():
        return candidate
    stamp = datetime.now().astimezone().strftime("%H%M%S")
    for idx in range(1, 1000):
        prefix = segment if segment is not None else stamp
        candidate = archive_dir / f"{stem}.{prefix}.{idx}{suffix}"
        if not candidate.exists() and not Path(str(candidate) + ".gz").exists():
            return candidate
    raise RuntimeError(f"no archive filename available for {active_path}")


def _reap_gzip_processes() -> None:
    done = {proc for proc in _GZIP_PROCESSES if proc.poll() is not None}
    _GZIP_PROCESSES.difference_update(done)


def _gzip_background(path: Path) -> None:
    if not path.exists():
        return
    try:
        _reap_gzip_processes()
        proc = subprocess.Popen(
            [GZIP_BIN, "-9f", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _GZIP_PROCESSES.add(proc)
    except OSError:
        # Keep the uncompressed archive if gzip cannot be started; never lose data.
        return


def _rotate_active_path(
    active_path: Path,
    archive_day: str,
    segment: str | None = None,
    archive_root: str | os.PathLike[str] | None = None,
) -> Path:
    archive_path = _unique_archive_path(
        active_path, archive_day, segment=segment, archive_root=archive_root
    )
    os.replace(active_path, archive_path)
    active_path.touch()
    return archive_path


def _rotate_if_needed(
    active_path: Path,
    archive_root: str | os.PathLike[str] | None = None,
) -> Path | None:
    if not active_path.exists():
        active_path.touch()
        return None
    stat = active_path.stat()
    if stat.st_size <= 0:
        active_path.touch()
        return None
    active_day = _local_day(stat.st_mtime)
    today = _local_day()
    if active_day < today:
        return _rotate_active_path(active_path, active_day, archive_root=archive_root)
    max_active_bytes = _max_active_bytes()
    if max_active_bytes > 0 and stat.st_size >= max_active_bytes:
        segment = datetime.now().astimezone().strftime("segment-%H%M%S")
        return _rotate_active_path(
            active_path, today, segment=segment, archive_root=archive_root
        )
    return None


def append_jsonl_record(
    path: str | os.PathLike[str],
    record: dict[str, Any],
    *,
    default: Any | None = None,
    sort_keys: bool = False,
    ensure_ascii: bool = True,
    archive_root: str | os.PathLike[str] | None = None,
    fsync: bool = False,
) -> None:
    line = json.dumps(
        record,
        default=default,
        sort_keys=sort_keys,
        ensure_ascii=ensure_ascii,
    ) + "\n"
    append_jsonl_line(path, line, archive_root=archive_root, fsync=fsync)


def append_jsonl_line(
    path: str | os.PathLike[str],
    line: str,
    *,
    archive_root: str | os.PathLike[str] | None = None,
    fsync: bool = False,
) -> None:
    append_jsonl_lines(path, (line,), archive_root=archive_root, fsync=fsync)


def append_jsonl_lines(
    path: str | os.PathLike[str],
    lines: Iterable[str],
    *,
    archive_root: str | os.PathLike[str] | None = None,
    fsync: bool = False,
) -> None:
    """Append every line under one rotation check and one open, so a batch cannot
    straddle a rotation boundary."""
    payload = "".join(lines)
    if not payload:
        return
    active_path = Path(path)
    active_path.parent.mkdir(parents=True, exist_ok=True)
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    rotated_path: Path | None = None
    with _lock_path(active_path).open("a") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        rotated_path = _rotate_if_needed(active_path, archive_root=archive_root)
        with active_path.open("a") as out:
            out.write(payload)
            if fsync:
                out.flush()
                os.fsync(out.fileno())
    if rotated_path is not None:
        _gzip_background(rotated_path)


def _segment_matches(name: str, stem: str, suffix: str) -> bool:
    base = name[:-3] if name.endswith(".gz") else name
    if not base.endswith(suffix):
        return False
    core = base[: len(base) - len(suffix)]
    return core == stem or core.startswith(stem + ".")


def archived_segments(
    path: str | os.PathLike[str],
    *,
    archive_root: str | os.PathLike[str] | None = None,
) -> list[Path]:
    """Archived segments for `path`, oldest first.

    A segment mid-gzip exists both compressed and plain, and the .gz is still being
    written — so the PLAIN copy wins whenever both are present. gzip only unlinks the
    original once compression completed, making "plain is absent" the signal that the
    .gz is whole. Preferring .gz here would hand back a truncated tail of history for
    the minutes a large segment takes to compress.

    Ordered by mtime, not by name: the first segment of a day carries no segment
    infix (`x.jsonl`) while later ones do (`x.segment-HHMMSS.jsonl`), so a
    lexicographic sort would file the earliest run last. os.replace and gzip both
    preserve mtime, so it survives archival and compression.
    """
    active_path = Path(path)
    root = _archive_root(archive_root)
    if not root.is_dir():
        return []
    stem, suffix = active_path.stem, active_path.suffix
    segments: list[Path] = []
    for day_dir in sorted(root.iterdir()):
        if not day_dir.is_dir():
            continue
        day_matches = [
            entry
            for entry in day_dir.iterdir()
            if entry.is_file() and _segment_matches(entry.name, stem, suffix)
        ]
        plain_names = {
            entry.name for entry in day_matches if not entry.name.endswith(".gz")
        }
        segments.extend(
            sorted(
                (
                    entry
                    for entry in day_matches
                    if not (
                        entry.name.endswith(".gz") and entry.name[:-3] in plain_names
                    )
                ),
                key=lambda entry: (entry.stat().st_mtime, entry.name),
            )
        )
    return segments


def _iter_segment_lines(segment: Path) -> Iterator[str]:
    opener = gzip.open if segment.name.endswith(".gz") else open
    with opener(segment, "rt", encoding="utf-8", errors="replace") as fh:
        yield from fh


def iter_jsonl_lines(
    path: str | os.PathLike[str],
    *,
    archive_root: str | os.PathLike[str] | None = None,
    include_archive: bool = True,
) -> Iterator[str]:
    """Yield every line written to `path` in write order: archived segments first,
    then the active file. Reading the active path directly sees only the current
    segment, so any full-history consumer of a rotated substrate must use this."""
    active_path = Path(path)
    if include_archive:
        for segment in archived_segments(active_path, archive_root=archive_root):
            yield from _iter_segment_lines(segment)
    if active_path.exists():
        yield from _iter_segment_lines(active_path)


def iter_jsonl_tail(
    path: str | os.PathLike[str],
    *,
    tail_bytes: int,
    archive_root: str | os.PathLike[str] | None = None,
) -> Iterator[str]:
    """Yield lines from the newest segments whose combined uncompressed span covers
    `tail_bytes`, oldest-first. Bounds IO for consumers that only want recent history
    but must not lose it to a rotation that just shrank the active file.

    Segments are read whole rather than seeked into: the caller already filters by
    timestamp, so overshooting is cheap and never drops a boundary row.
    """
    active_path = Path(path)
    chain = archived_segments(active_path, archive_root=archive_root)
    if active_path.exists():
        chain = chain + [active_path]
    picked: list[Path] = []
    budget = tail_bytes
    for segment in reversed(chain):
        picked.append(segment)
        size = segment.stat().st_size
        budget -= size * GZ_EXPANSION_ESTIMATE if segment.name.endswith(".gz") else size
        if budget <= 0:
            break
    for segment in reversed(picked):
        yield from _iter_segment_lines(segment)
