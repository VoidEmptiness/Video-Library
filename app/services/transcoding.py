from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path


# Path to bundled FFmpeg binaries (relative to project root)
_THIS_DIR = Path(__file__).resolve().parent  # app/services/
_PROJECT_ROOT = _THIS_DIR.parent.parent  # videoplayer/
FFMPEG_DIR = _PROJECT_ROOT / "ffmpeg" / "bin"

# On Windows look for .exe, on Linux look for plain binary
_FFMPEG_SUFFIX = ".exe" if os.name == "nt" else ""
_BUNDLED_FFMPEG = FFMPEG_DIR / f"ffmpeg{_FFMPEG_SUFFIX}"
_BUNDLED_FFPROBE = FFMPEG_DIR / f"ffprobe{_FFMPEG_SUFFIX}"


def _resolve_ffmpeg() -> Path | None:
    if _BUNDLED_FFMPEG.exists():
        return _BUNDLED_FFMPEG
    alt = FFMPEG_DIR / "ffmpeg.exe" if _FFMPEG_SUFFIX else FFMPEG_DIR / "ffmpeg"
    if alt.exists():
        return alt
    sys_path = shutil.which("ffmpeg")
    return Path(sys_path) if sys_path else None


def _resolve_ffprobe() -> Path | None:
    if _BUNDLED_FFPROBE.exists():
        return _BUNDLED_FFPROBE
    alt = FFMPEG_DIR / "ffprobe.exe" if _FFMPEG_SUFFIX else FFMPEG_DIR / "ffprobe"
    if alt.exists():
        return alt
    sys_path = shutil.which("ffprobe")
    return Path(sys_path) if sys_path else None


FFMPEG_EXE = _resolve_ffmpeg()
FFPROBE_EXE = _resolve_ffprobe()

TRANSCODE_ENABLED = os.getenv("TRANSCODE_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
TRANSCODE_CRF = os.getenv("TRANSCODE_CRF", "23")
TRANSCODE_PRESET = os.getenv("TRANSCODE_PRESET", "medium")
_raw_timeout = (os.getenv("TRANSCODE_TIMEOUT_SECONDS", "") or "").strip()
TRANSCODE_TIMEOUT_SECONDS = int(_raw_timeout) if _raw_timeout.isdigit() else 3600
if TRANSCODE_TIMEOUT_SECONDS <= 0:
    TRANSCODE_TIMEOUT_SECONDS = 3600

# Progress file directory
PROGRESS_DIR = Path(os.getenv("TRANSCODE_PROGRESS_DIR", "transcode_progress"))


def ffmpeg_available() -> bool:
    return FFMPEG_EXE is not None and FFMPEG_EXE.exists()


def ffprobe_available() -> bool:
    return FFPROBE_EXE is not None and FFPROBE_EXE.exists()


def video_codec(input_path: Path) -> str | None:
    if not ffprobe_available():
        return None

    cmd = [
        str(FFPROBE_EXE),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "default=nokey=1:noprint_wrappers=1",
        str(input_path),
    ]

    try:
        result = subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
        codec = result.stdout.strip().lower()
        return codec or None
    except Exception:
        return None


def video_needs_transcode(input_path: Path) -> bool:
    """Returns True if the video should be transcoded to H.264 for browser compatibility."""
    if not TRANSCODE_ENABLED:
        return False
    codec = video_codec(input_path)
    if codec is None:
        return False  # can't detect, keep as-is
    # H.264 is natively supported in all browsers, everything else needs transcoding
    return codec != "h264"


def get_progress_file(video_id: int) -> Path:
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    return PROGRESS_DIR / f"{video_id}.txt"


def write_progress(video_id: int, status: str, percent: float = 0):
    """Write progress status to a file."""
    try:
        get_progress_file(video_id).write_text(f"{percent}\n{status}")
    except Exception:
        pass


def clear_progress(video_id: int):
    """Remove progress file when done."""
    try:
        get_progress_file(video_id).unlink()
    except Exception:
        pass


def read_progress(video_id: int) -> dict:
    """Read current progress, returns dict with status/percent."""
    pf = get_progress_file(video_id)
    if not pf.exists():
        return {"status": "done", "percent": 100}
    try:
        content = pf.read_text().strip().split("\n", 1)
        percent = float(content[0].strip())
        status = content[1].strip() if len(content) > 1 else "processing"
        return {"status": status, "percent": percent}
    except Exception:
        return {"status": "unknown", "percent": 0}


def transcode_to_h264(input_path: Path, output_path: Path, video_id: int | None = None) -> bool:
    if not ffmpeg_available():
        return False

    output_path.parent.mkdir(parents=True, exist_ok=True)

    duration = None
    original_bitrate = None
    if video_id and ffprobe_available():
        try:
            r = subprocess.run(
                [str(FFPROBE_EXE), "-v", "error", "-show_entries", "format=duration,bit_rate",
                 "-of", "default=nokey=1:noprint_wrappers=1", str(input_path)],
                capture_output=True, text=True, timeout=15
            )
            if r.returncode == 0:
                lines = r.stdout.strip().split('\n')
                if len(lines) >= 2:
                    duration = float(lines[0].strip()) if lines[0].strip() else None
                    original_bitrate = int(lines[1].strip()) if lines[1].strip() else None
        except Exception:
            pass

    if video_id:
        write_progress(video_id, "Подготовка...", 0)

    # Calculate target video bitrate (subtract audio bitrate)
    target_video_bitrate = None
    if original_bitrate and duration:
        # Target bitrate slightly higher than original to compensate for H.264 inefficiency
        # but cap at reasonable limit to prevent massive file growth
        target_video_bitrate = max(500, min(original_bitrate - 128000, original_bitrate * 1.2))

    # Build command
    cmd = [
        str(FFMPEG_EXE),
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        TRANSCODE_PRESET,
        "-profile:v",
        "main",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        "-sn",
    ]

    # Use bitrate-based encoding if we have a target, otherwise use CRF
    if target_video_bitrate:
        cmd.extend(["-b:v", str(target_video_bitrate)])
        cmd.extend(["-maxrate", str(int(target_video_bitrate * 1.5))])
        cmd.extend(["-bufsize", str(int(target_video_bitrate * 2))])
    else:
        cmd.extend(["-crf", TRANSCODE_CRF])

    cmd.append(str(output_path))

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE if video_id else subprocess.DEVNULL,
            text=True,
        )
        
        # Parse ffmpeg progress output
        if video_id and proc.stderr:
            time_pattern = re.compile(r'out_time_ms=(\d+)')
            last_percent = 0
            
            for line in proc.stderr:
                match = time_pattern.search(line)
                if match and duration:
                    try:
                        current_time_ms = int(match.group(1))
                        current_time_sec = current_time_ms / 1_000_000
                        percent = min(99, int((current_time_sec / duration) * 100))
                        if percent > last_percent:
                            write_progress(video_id, f"Транскодирование: {percent}%", percent)
                            last_percent = percent
                    except (ValueError, ZeroDivisionError):
                        pass
        
        proc.wait(timeout=TRANSCODE_TIMEOUT_SECONDS)
        
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd)
        
        if video_id:
            clear_progress(video_id)
        return output_path.exists() and output_path.stat().st_size > 0
    except subprocess.CalledProcessError:
        if video_id:
            write_progress(video_id, "Ошибка транскодирования", 0)
        try:
            if output_path.exists():
                output_path.unlink()
        except Exception:
            pass
        return False
    except Exception:
        try:
            if output_path.exists():
                output_path.unlink()
        except Exception:
            pass
        if video_id:
            clear_progress(video_id)
        return False
