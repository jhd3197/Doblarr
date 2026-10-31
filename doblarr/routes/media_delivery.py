"""One path guard and one range-capable file response for every media route.

Job previews, saved versions and the studio all stream local files to the
browser. They share this guard (a file must sit inside the configured work or
output directory, re-read live so a changed setting takes effect) and this
response (HTTP Range, which starlette's FileResponse does not do, so seeking
works in audio and video elements).
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from ..fileio import inside

MEDIA_TYPES = {
    ".mkv": "video/x-matroska",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
}


def allowed_path(config, path_str: str) -> Path | None:
    """`path_str` resolved, or None when it is outside the configured roots."""
    if not path_str or not inside(path_str, (config.output_dir, config.work_dir)):
        return None
    return Path(path_str).resolve()


def ranged_response(path: Path, range_header: str | None):
    """Serve `path`, honoring `Range: bytes=...` for browser seeking."""
    media_type = MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
    size = path.stat().st_size
    base_headers = {"Accept-Ranges": "bytes"}
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", (range_header or "").strip())
    if not range_header:
        return FileResponse(path, media_type=media_type, headers=base_headers)
    if not m or (not m.group(1) and not m.group(2)):
        return JSONResponse(
            status_code=416,
            content={"error": "bad range"},
            headers={"Content-Range": f"bytes */{size}"},
        )
    start_s, end_s = m.groups()
    if not start_s:  # suffix range: last N bytes
        start = max(0, size - int(end_s))
        end = size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    end = min(end, size - 1)
    if start >= size or start > end:
        return JSONResponse(
            status_code=416,
            content={"error": "range unsatisfiable"},
            headers={"Content-Range": f"bytes */{size}"},
        )
    length = end - start + 1

    def iterfile():
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        iterfile(),
        status_code=206,
        media_type=media_type,
        headers={
            **base_headers,
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(length),
        },
    )
