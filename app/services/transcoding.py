from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent.parent
FFMPEG_DIR = _PROJECT_ROOT / "ffmpeg" / "bin"

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

TRANSCODE_DOWNSCALE_HEIGHT = int(os.getenv("TRANSCODE_DOWNSCALE_HEIGHT", "720"))
TRANSCODE_DOWNSCALE_MAX_HEIGHT = int(os.getenv("TRANSCODE_DOWNSCALE_MAX_HEIGHT", "1080"))
TRANSCODE_DOWNSCALE_FPS = int(os.getenv("TRANSCODE_DOWNSCALE_FPS", "24"))

_APP_DIR = Path(__file__).resolve().parent.parent
PROGRESS_DIR = Path(os.getenv("TRANSCODE_PROGRESS_DIR", str(_APP_DIR / "transcode_progress")))


def ffmpeg_available() -> bool:
    return FFMPEG_EXE is not None and FFMPEG_EXE.exists()


def ffprobe_available() -> bool:
    return FFPROBE_EXE is not None and FFPROBE_EXE.exists()


def video_fps(input_path: Path) -> float | None:
    if not ffprobe_available():
        return None
    cmd = [
        str(FFPROBE_EXE),
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(input_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
        if result.returncode != 0:
            return None
        raw = result.stdout.strip()
        if not raw or "/" not in raw:
            return None
        parts = raw.split("/")
        if len(parts) == 2:
            num = float(parts[0])
            den = float(parts[1])
            if den > 0:
                return round(num / den, 3)
        return None
    except Exception:
        return None


def video_height(input_path: Path) -> int | None:
    if not ffprobe_available():
        return None
    cmd = [
        str(FFPROBE_EXE),
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=height",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(input_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
        if result.returncode != 0:
            return None
        raw = result.stdout.strip()
        return int(raw) if raw else None
    except Exception:
        return None


def video_needs_downscale(input_path: Path) -> bool:
    if not TRANSCODE_ENABLED:
        return False
    h = video_height(input_path)
    if h is None:
        return False
    return h > TRANSCODE_DOWNSCALE_MAX_HEIGHT


def get_progress_file(video_id: int) -> Path:
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    return PROGRESS_DIR / f"{video_id}.txt"


def write_progress(video_id: int, status: str, percent: float = 0):
    try:
        get_progress_file(video_id).write_text(f"{percent}\n{status}")
    except Exception:
        pass


def clear_progress(video_id: int):
    try:
        get_progress_file(video_id).unlink()
    except Exception:
        pass


def transcode_to_h264(
    input_path: Path,
    output_path: Path,
    video_id: int | None = None,
    downscale_height: int | None = None,
    target_fps: int | None = None,
) -> bool:
    if not ffmpeg_available():
        logger.warning("ffmpeg not available, cannot transcode %s", input_path)
        return False

    output_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Starting transcode: %s -> %s (height=%s, fps=%s)",
                input_path.name, output_path.name, downscale_height, target_fps)

    duration = None
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
        except Exception:
            logger.exception("Failed to probe duration for transcode %s", input_path)

    if video_id:
        write_progress(video_id, "Подготовка...", 0)

    vf_parts = []
    if downscale_height:
        vf_parts.append(f"scale=-2:{downscale_height}")
    if target_fps:
        vf_parts.append(f"fps={target_fps}")

    cmd = [
        str(FFMPEG_EXE),
        "-y",
        "-i",
        str(input_path),
        "-map", "0:v:0",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", TRANSCODE_PRESET,
        "-crf", TRANSCODE_CRF,
        "-tune", "fastdecode",
        "-profile:v", "main",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "96k",
        "-movflags", "+faststart",
        "-sn",
    ]
    if vf_parts:
        cmd.extend(["-vf", ",".join(vf_parts)])
    cmd.append(str(output_path))

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE if video_id else subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        
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
        logger.info("Transcode finished: %s -> %s (%d bytes)",
                    input_path.name, output_path.name, output_path.stat().st_size)
        return output_path.exists() and output_path.stat().st_size > 0
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        logger.warning("Transcode timeout for %s after %d seconds", input_path, TRANSCODE_TIMEOUT_SECONDS)
        if video_id:
            write_progress(video_id, "Таймаут транскодирования", 0)
        try:
            if output_path.exists():
                output_path.unlink()
        except Exception:
            pass
        return False
    except subprocess.CalledProcessError as e:
        logger.error("Transcode failed for %s: returncode=%s", input_path, e.returncode)
        if video_id:
            write_progress(video_id, "Ошибка транскодирования", 0)
        try:
            if output_path.exists():
                output_path.unlink()
        except Exception:
            pass
        return False
    except Exception as e:
        logger.exception("Transcode error for %s: %s", input_path, e)
        try:
            if output_path.exists():
                output_path.unlink()
        except Exception:
            pass
        if video_id:
            clear_progress(video_id)
        return False
