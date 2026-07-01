from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .transcoding import FFMPEG_EXE

logger = logging.getLogger(__name__)

ALL_RENDITION_HEIGHTS = [240, 360, 480, 720, 1080]
HLS_TEMP_DIR = Path(os.getenv("HLS_TEMP_DIR", "/tmp/hls"))

HLS_BANDWIDTHS = {
    240: 400_000,
    360: 700_000,
    480: 1_400_000,
    720: 3_000_000,
    1080: 6_000_000,
}

_generation_tasks: dict[tuple[int, int, int], dict] = {}
_generation_lock = threading.Lock()


def _valid_heights(video_height: int | None) -> list[int]:
    if not video_height:
        return list(ALL_RENDITION_HEIGHTS)
    return [h for h in ALL_RENDITION_HEIGHTS if h <= video_height]


def _hls_dir(video_id: int, height: int, start_sec: int = 0) -> Path:
    return HLS_TEMP_DIR / str(video_id) / f"{height}_{start_sec}"


def _hls_playlist_path(video_id: int, height: int, start_sec: int = 0) -> Path:
    return _hls_dir(video_id, height, start_sec) / "playlist.m3u8"


def _kill_ffmpeg_by_video(video_id: int) -> None:
    pattern = f"{HLS_TEMP_DIR}/{video_id}/"
    try:
        subprocess.run(["pkill", "-f", pattern], capture_output=True, timeout=5)
    except Exception:
        try:
            for p in Path("/proc").glob("*/cmdline"):
                try:
                    data = p.read_bytes().replace(b"\0", b" ")
                    if b"ffmpeg" in data and pattern.encode() in data:
                        os.kill(int(p.parent.name), 9)
                except (ValueError, OSError):
                    pass
        except Exception:
            pass


def _run_ffmpeg(
    source_path: Path, video_id: int, height: int, start_sec: int = 0
) -> None:
    out_dir = _hls_dir(video_id, height, start_sec)
    out_dir.mkdir(parents=True, exist_ok=True)
    seg_pattern = str(out_dir / "seg_%03d.ts")
    playlist_path = str(out_dir / "playlist.m3u8")

    cmd = [str(FFMPEG_EXE), "-y"]
    if start_sec > 0:
        cmd += ["-ss", str(start_sec)]
    cmd += [
        "-i",
        str(source_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "28",
        "-threads",
        "0",
        "-g",
        "30",
        "-keyint_min",
        "30",
        "-sc_threshold",
        "0",
        "-vf",
        f"scale=-2:{height}",
        "-sws_flags",
        "fast_bilinear",
        "-profile:v",
        "main",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-sn",
        "-f",
        "hls",
        "-hls_time",
        "3",
        "-hls_list_size",
        "0",
        "-hls_playlist_type",
        "event",
        "-hls_segment_filename",
        seg_pattern,
        "-hls_flags",
        "independent_segments+temp_file",
        playlist_path,
    ]

    logger.info(
        "Starting HLS: %s (%dp, start=%ds)", source_path.name, height, start_sec
    )
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        with _generation_lock:
            key = (video_id, height, start_sec)
            if key not in _generation_tasks or _generation_tasks[key].get("done"):
                proc.kill()
                proc.wait(timeout=5)
                return
            _generation_tasks[key]["proc"] = proc
        proc.wait(timeout=7200)
        ok = proc.returncode == 0 and _hls_playlist_path(
            video_id, height, start_sec
        ).exists()
        if ok:
            logger.info(
                "HLS done: video %d (%dp, start=%d), %d segments",
                video_id,
                height,
                start_sec,
                len(list(out_dir.glob("seg_*.ts"))),
            )
    except Exception:
        logger.exception(
            "HLS failed for video %d (%dp, start=%d)", video_id, height, start_sec
        )
        ok = False

    with _generation_lock:
        key = (video_id, height, start_sec)
        if key in _generation_tasks:
            _generation_tasks[key]["done"] = True
            _generation_tasks[key]["success"] = ok


def _kill_processes(
    video_id: int, same_start: int | None = None
) -> list[tuple[int, int, int]]:
    killed: list[tuple[int, int, int]] = []
    with _generation_lock:
        for key in list(_generation_tasks.keys()):
            if key[0] != video_id:
                continue
            if same_start is not None and key[2] != same_start:
                continue
            task = _generation_tasks[key]
            if task.get("done"):
                continue
            proc = task.get("proc")
            if proc:
                threading.Thread(
                    target=lambda p: (p.kill(), p.wait(timeout=3)),
                    args=(proc,),
                    daemon=True,
                ).start()
            task["done"] = True
            killed.append(key)
        for key in killed:
            del _generation_tasks[key]
    for _, h, s in killed:
        d = _hls_dir(video_id, h, s)
        if d.exists():
            threading.Thread(
                target=shutil.rmtree,
                args=(str(d),),
                kwargs={"ignore_errors": True},
                daemon=True,
            ).start()
    _kill_ffmpeg_by_video(video_id)
    return killed


def _ensure_hls_async(
    source_path: Path, video_id: int, height: int, start_sec: int = 0
) -> bool:
    playlist = _hls_playlist_path(video_id, height, start_sec)
    if playlist.exists():
        if _generation_active(video_id, height, start_sec):
            return True
        out_dir = _hls_dir(video_id, height, start_sec)
        if any(out_dir.glob("seg_*.ts")):
            return True
        shutil.rmtree(str(out_dir), ignore_errors=True)

    with _generation_lock:
        for key in list(_generation_tasks.keys()):
            if key[0] != video_id:
                continue
            task = _generation_tasks[key]
            if task.get("done"):
                continue
            proc = task.get("proc")
            if proc:
                threading.Thread(
                    target=lambda p: (p.kill(), p.wait(timeout=3)),
                    args=(proc,),
                    daemon=True,
                ).start()
            task["done"] = True
            d = _hls_dir(video_id, key[1], key[2])
            if d.exists():
                threading.Thread(
                    target=shutil.rmtree,
                    args=(str(d),),
                    kwargs={"ignore_errors": True},
                    daemon=True,
                ).start()
        _kill_ffmpeg_by_video(video_id)

        key = (video_id, height, start_sec)
        if key in _generation_tasks:
            task = _generation_tasks[key]
            if not task["done"]:
                return True
            del _generation_tasks[key]
        thread = threading.Thread(
            target=_run_ffmpeg,
            args=(source_path, video_id, height, start_sec),
            daemon=True,
        )
        _generation_tasks[key] = {"done": False, "success": False, "thread": thread}
        thread.start()
        return True


def _wait_for_playlist(
    video_id: int, height: int, start_sec: int = 0, timeout: int = 120
) -> bool:
    playlist = _hls_playlist_path(video_id, height, start_sec)
    for _ in range(timeout):
        if playlist.exists() and playlist.stat().st_size > 10:
            return True
        time.sleep(1)
    return False


def _generation_active(video_id: int, height: int, start_sec: int = 0) -> bool:
    with _generation_lock:
        key = (video_id, height, start_sec)
        task = _generation_tasks.get(key)
        if task and not task["done"]:
            return True
        return False


def _cleanup_hls(video_id: int) -> None:
    with _generation_lock:
        for key in list(_generation_tasks.keys()):
            if key[0] == video_id:
                task = _generation_tasks[key]
                if not task.get("done"):
                    proc = task.get("proc")
                    if proc:
                        threading.Thread(
                            target=lambda p: (p.kill(), p.wait(timeout=3)),
                            args=(proc,),
                            daemon=True,
                        ).start()
                    task["done"] = True
                del _generation_tasks[key]
    d = HLS_TEMP_DIR / str(video_id)
    if d.exists():
        shutil.rmtree(str(d), ignore_errors=True)


def _kill_hls(video_id: int) -> None:
    with _generation_lock:
        for key in list(_generation_tasks.keys()):
            if key[0] == video_id:
                task = _generation_tasks[key]
                if not task.get("done"):
                    proc = task.get("proc")
                    if proc:
                        threading.Thread(
                            target=lambda p: (p.kill(), p.wait(timeout=3)),
                            args=(proc,),
                            daemon=True,
                        ).start()
                    task["done"] = True
                del _generation_tasks[key]
    _kill_ffmpeg_by_video(video_id)


async def _wait_for_playlist_async(
    video_id: int, height: int, start_sec: int = 0, timeout: int = 120
) -> bool:
    playlist = _hls_playlist_path(video_id, height, start_sec)
    for _ in range(timeout):
        if playlist.exists() and playlist.stat().st_size > 10:
            return True
        await asyncio.sleep(0.5)
    return False
