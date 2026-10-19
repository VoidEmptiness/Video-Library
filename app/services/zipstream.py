from __future__ import annotations

import datetime as dt
import queue
import threading
import zipfile
from pathlib import Path
from typing import Iterable, Iterator

_READ_CHUNK = 1024 * 1024
_QUEUE_SIZE = 8


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
