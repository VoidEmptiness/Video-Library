
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from __future__ import annotations

import datetime as dt
import queue
import threading
import zipfile
from pathlib import Path
from typing import Iterable, Iterator

_READ_CHUNK = 1024 * 1024
_QUEUE_SIZE = 8

_ZIP64_LIMIT = (1 << 31) - 1
_MAX_ENTRIES = 0xFFFF
_LOCAL_HEADER_SIZE = 30
_LOCAL_ZIP64_EXTRA_SIZE = 20
_DATA_DESCRIPTOR_SIZE = 24
_CENTRAL_HEADER_SIZE = 46
_END_RECORD_SIZE = 22
_ZIP64_END_RECORD_SIZE = 56
_ZIP64_LOCATOR_SIZE = 20


class _ClientGone(Exception):
    pass


class _QueueWriter:
    def __init__(self, sink: "queue.Queue[object]", cancel: threading.Event) -> None:
        self._sink = sink
        self._cancel = cancel

    def write(self, data) -> int:
        if data and not self._cancel.is_set():
            payload = bytes(data)
            while not self._cancel.is_set():
                try:
                    self._sink.put(payload, timeout=0.2)
                    break
                except queue.Full:
                    continue
            else:
                raise _ClientGone
        return len(data)

    def flush(self) -> None:
        pass


def _zip_datetime(path: Path) -> tuple[int, int, int, int, int, int]:
    try:
        timestamp = path.stat().st_mtime
    except OSError:
        timestamp = dt.datetime.now().timestamp()
    value = dt.datetime.fromtimestamp(timestamp)
    if value.year < 1980:
        value = value.replace(year=1980, month=1, day=1, hour=0, minute=0, second=0)
    return (value.year, value.month, value.day, value.hour, value.minute, value.second)


def _arcname_size(arcname: str) -> int:
    name = zipfile.ZipInfo(arcname).filename
    try:
        return len(name.encode("ascii"))
    except UnicodeEncodeError:
        return len(name.encode("utf-8"))


def archive_size(entries: Iterable[tuple[Path, str]]) -> int:
    offset = 0
    central = 0
    count = 0
    for source, arcname in entries:
        name_size = _arcname_size(arcname)
        size = source.stat().st_size
        extra = 0
        if size > _ZIP64_LIMIT:
            extra += 16
        if offset > _ZIP64_LIMIT:
            extra += 8
        central += _CENTRAL_HEADER_SIZE + name_size + (4 + extra if extra else 0)
        offset += (
            _LOCAL_HEADER_SIZE
            + name_size
            + _LOCAL_ZIP64_EXTRA_SIZE
            + size
            + _DATA_DESCRIPTOR_SIZE
        )
        count += 1
    trailer = _END_RECORD_SIZE
    if count > _MAX_ENTRIES or offset > _ZIP64_LIMIT or central > _ZIP64_LIMIT:
        trailer += _ZIP64_END_RECORD_SIZE + _ZIP64_LOCATOR_SIZE
    return offset + central + trailer


def stream_zip(entries: Iterable[tuple[Path, str]]) -> Iterator[bytes]:
    sink: "queue.Queue[object]" = queue.Queue(maxsize=_QUEUE_SIZE)
    cancel = threading.Event()

    def _put(item: object) -> None:
        while not cancel.is_set():
            try:
                sink.put(item, timeout=0.2)
                return
            except queue.Full:
                continue

    def _produce() -> None:
        error: BaseException | None = None
        try:
            writer = _QueueWriter(sink, cancel)
            with zipfile.ZipFile(writer, "w", zipfile.ZIP_STORED, allowZip64=True) as archive:
                for source, arcname in entries:
                    info = zipfile.ZipInfo(arcname, date_time=_zip_datetime(source))
                    info.compress_type = zipfile.ZIP_STORED
                    info.external_attr = 0o600 << 16
                    with archive.open(info, "w", force_zip64=True) as dest:
                        with source.open("rb") as src:
                            while True:
                                chunk = src.read(_READ_CHUNK)
                                if not chunk:
                                    break
                                dest.write(chunk)
        except _ClientGone:
            return
        except BaseException as exc:
            error = exc
        finally:
            if error is not None:
                _put(error)
            _put(None)

    thread = threading.Thread(target=_produce, name="zip-stream", daemon=True)
    thread.start()
    try:
        while True:
            item = sink.get()
            if item is None:
                break
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        cancel.set()
        while True:
            try:
                sink.get_nowait()
            except queue.Empty:
                break
        thread.join(timeout=10)
