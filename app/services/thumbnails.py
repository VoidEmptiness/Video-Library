from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .transcoding import ffmpeg_available, FFMPEG_EXE

THUMBNAIL_TIMEOUT_SECONDS = int(os.getenv("THUMBNAIL_TIMEOUT_SECONDS", "30") or "30")


def generate_thumbnail(input_path: Path, output_path: Path) -> bool:
    """
    Best-effort thumbnail generation. Returns True on success.
    """
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