from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Video
from ..services.hls import (
    ALL_RENDITION_HEIGHTS,
    HLS_BANDWIDTHS,
    _ensure_hls_async,
    _generation_active,
    _hls_dir,
    _hls_playlist_path,
    _kill_hls,
    _kill_processes,
    _valid_heights,
    _wait_for_playlist,
    _wait_for_playlist_async,
)
from ..services.storage import video_path
from ..services.utils import _format_duration, _probe_video_metadata
from .auth import User, UserHTML

router = APIRouter()


@router.get("/videos/{video_id}/renditions")
def list_renditions(
    video_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: User,
):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    result = [
        {
            "height": 0,
            "label": "Source",
            "available": True,
            "content_type": video.content_type or "video/mp4",
        }
    ]
    for h in ALL_RENDITION_HEIGHTS:
        result.append({
            "height": h,
            "label": f"{h}p",
            "available": _hls_playlist_path(video_id, h).exists(),
            "content_type": "application/vnd.apple.mpegurl",
        })
    return result


@router.get("/hls/{video_id}/master.m3u8")
def hls_master_playlist(
    video_id: int,
    db: Annotated[Session, Depends(get_db)],
    _: User,
    start: int = Query(0),
    height: int = Query(None),
):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    source_path = video_path(video.filename)
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Source missing")

    _, _, _, video_height = _probe_video_metadata(source_path)
    valid_heights = _valid_heights(video_height)
    heights = [height] if height else [h for h in valid_heights if _hls_playlist_path(video_id, h, start).exists()]
    if not heights:
        raise HTTPException(status_code=404, detail="No renditions available")

    lines = ["#EXTM3U"]
    for h in heights:
        if h not in valid_heights:
            continue
        bw = HLS_BANDWIDTHS.get(h, 500_000)
        lines.append(f"#EXT-X-STREAM-INF:BANDWIDTH={bw},RESOLUTION={h*16//9}x{h}")
        lines.append(f"{h}/{start}/playlist.m3u8")

    lines.append("")
    return Response("\n".join(lines), media_type="application/vnd.apple.mpegurl")


@router.get("/hls/{video_id}/{height}/{start}/playlist.m3u8")
def hls_rendition_playlist(
    video_id: int,
    height: int,
    start: int,
    db: Annotated[Session, Depends(get_db)],
    _: User,
):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    source_path = video_path(video.filename)
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Source missing")

    _, _, _, video_height = _probe_video_metadata(source_path)
    if height not in _valid_heights(video_height):
        raise HTTPException(status_code=400, detail="Invalid height")

    playlist_path = _hls_playlist_path(video_id, height, start)
    if not playlist_path.exists():
        if not _ensure_hls_async(source_path, video_id, height, start):
            raise HTTPException(status_code=503, detail="Failed to start HLS generation")
        if not _wait_for_playlist(video_id, height, start, timeout=120):
            raise HTTPException(status_code=503, detail="HLS generation timeout")

    _, duration_seconds, _, _ = (
        _probe_video_metadata(source_path) if source_path.exists() else (None, None, None, None)
    )
    headers = {
        "X-Original-Duration": str(duration_seconds or 0),
        "X-Start-Sec": str(start),
    }
    return FileResponse(str(playlist_path), media_type="application/vnd.apple.mpegurl", headers=headers)


@router.get("/hls/{video_id}/{height}/{start}/{filename}")
async def hls_segment(
    video_id: int,
    height: int,
    start: int,
    filename: str,
    _: User,
):
    seg_path = _hls_dir(video_id, height, start) / filename
    if seg_path.exists():
        return FileResponse(str(seg_path), media_type="video/MP2T")

    if _generation_active(video_id, height, start):
        for _ in range(120):
            if seg_path.exists():
                return FileResponse(str(seg_path), media_type="video/MP2T")
            await asyncio.sleep(0.5)

    raise HTTPException(status_code=404, detail="Segment not found")


@router.post("/videos/{video_id}/cleanup-hls")
def cleanup_hls(
    video_id: int,
    _: User,
):
    _kill_hls(video_id)
    return {"ok": True}


@router.post("/hls/{video_id}/seek")
async def hls_seek(
    video_id: int,
    body: Annotated[dict, Body()],
    db: Annotated[Session, Depends(get_db)],
    _: User,
):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    source_path = video_path(video.filename)
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Source missing")

    height = int(body["height"])
    old_start = int(body.get("start_sec", 0))
    new_position = float(body["new_position"])

    _, _, _, video_height = _probe_video_metadata(source_path)
    if height not in _valid_heights(video_height):
        raise HTTPException(status_code=400, detail="Invalid height")

    new_start = max(0, int(new_position))

    _kill_processes(video_id)

    if not _ensure_hls_async(source_path, video_id, height, new_start):
        raise HTTPException(status_code=503, detail="Failed to start HLS generation")
    if not await _wait_for_playlist_async(video_id, height, new_start, timeout=120):
        raise HTTPException(status_code=503, detail="HLS generation timeout")

    _, duration_seconds, _, _ = (
        _probe_video_metadata(source_path) if source_path.exists() else (None, None, None, None)
    )

    return {
        "start_sec": new_start,
        "original_duration": duration_seconds or 0,
    }
