from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path

from ..database import SessionLocal
from ..models import Video
from .storage import THUMB_DIR
from .transcoding import ffmpeg_available, FFMPEG_EXE

logger = logging.getLogger(__name__)

THUMBNAIL_TIMEOUT_SECONDS = int(os.getenv("THUMBNAIL_TIMEOUT_SECONDS", "30") or "30")
THUMBNAIL_CONCURRENCY = int(os.getenv("THUMBNAIL_CONCURRENCY", "4"))


def generate_thumbnail(input_path: Path, output_path: Path) -> bool:
    if not ffmpeg_available():
        return False
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(FFMPEG_EXE),
        "-y",
        "-ss",
        "00:00:01.000",
        "-i",
        str(input_path),
        "-frames:v",
        "1",
        "-vf",
        "scale=320:-1",
        str(output_path),
    ]
    try:
        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=THUMBNAIL_TIMEOUT_SECONDS,
        )
        return output_path.exists()
    except Exception:
        return False


async def generate_all_thumbnails(pairs: list[tuple[int, Path]]) -> None:
    sem = asyncio.Semaphore(THUMBNAIL_CONCURRENCY)

    async def _one(video_id: int, src: Path) -> Path | None:
        async with sem:
            thumb = THUMB_DIR / f"{video_id}.jpg"
            ok = await asyncio.to_thread(generate_thumbnail, src, thumb)
            return thumb if ok else None

    results = await asyncio.gather(*[_one(vid, p) for vid, p in pairs])

    db = SessionLocal()
    try:
        for video_id, thumb_path in zip([vid for vid, _ in pairs], results):
            if thumb_path is None:
                continue
            video = db.get(Video, video_id)
            if video:
                video.thumbnail_path = str(thumb_path)
                db.add(video)
        db.commit()
    except Exception:
        logger.exception("Failed to save thumbnails")
    finally:
        db.close()