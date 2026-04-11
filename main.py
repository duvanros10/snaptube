import os

import yt_dlp
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, Response, StreamingResponse

from utils import (
    format_bytes,
    get_quality_tag,
    remove_file,
    send_file_chunks,
    slugify,
)

app = FastAPI()

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "downloads")


def _yt_dlp_base_opts():
    """YouTube EJS needs a JS runtime; Node is installed in Docker (yt-dlp wiki: EJS)."""
    node_cfg = {}
    bin_path = os.environ.get("YT_DLP_NODE_PATH")
    if bin_path:
        node_cfg["path"] = bin_path
    return {
        "js_runtimes": {"node": node_cfg},
        "remote_components": {"ejs:github"},
    }


def ydl_opts(**kwargs):
    opts = _yt_dlp_base_opts()
    opts.update(kwargs)
    return opts


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.get("/video-details")
async def get_video_details(url: str):
    try:
        with yt_dlp.YoutubeDL(ydl_opts(quiet=True, no_warnings=True)) as ydl:
            # Metadata only; no download
            info = ydl.extract_info(url, download=False)

            # Prefer progressive MP4 (video+audio) for inline preview; 720p if available
            preview_url = None
            for f in info.get("formats", []):
                if (
                    f.get("vcodec") != "none"
                    and f.get("acodec") != "none"
                    and f.get("ext") == "mp4"
                ):
                    preview_url = f.get("url")
                    if f.get("height") == 720:
                        break

            if not preview_url:
                preview_url = info.get("url")

            formats_list = []
            for f in info.get("formats", []):
                if f.get("ext") == "mp4" and f.get("vcodec") != "none":
                    quality = get_quality_tag(
                        height=f.get("height", 0), fps=f.get("fps", 0)
                    )
                    formats_list.append(
                        {
                            "format_id": f.get("format_id"),
                            "resolution": f.get("resolution")
                            or f"{f.get('width')}x{f.get('height')}",
                            "extension": f.get("ext"),
                            "has_audio": f.get("acodec") != "none",
                            "filesize": f.get("filesize") or f.get("filesize_approx"),
                            "filesize_formatted": format_bytes(
                                f.get("filesize") or f.get("filesize_approx")
                            ),
                            "video_codec": f.get("vcodec"),
                            "audio_codec": f.get("acodec"),
                            "quality": quality,
                        }
                    )

            return {
                "title": info.get("title"),
                "url": preview_url,
                "imageUrl": info.get("thumbnail"),
                "duration": info.get("duration_string"),
                "formats": formats_list,
            }

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/download/file/{video_id}/{format_id}")
async def delete_cached_file(video_id: str, format_id: str):
    temp_filename = f"{video_id}_{format_id}.mp4"
    file_path = os.path.join(DOWNLOAD_DIR, temp_filename)

    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except OSError:
            return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.api_route("/download/stream", methods=["GET", "HEAD"])
async def download_video_stream(
    request: Request,
    url: str,
    format_id: str,
    background_tasks: BackgroundTasks,
    range: str = Header(None),  # HTTP Range for resume / partial content
):
    file_path = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts(quiet=True)) as ydl:
            info = ydl.extract_info(url, download=False)
            video_title = info.get("title", "untitled_video")

        clean_title = slugify(video_title)
        video_id = info.get("id", "").lower()
        temp_filename = f"{video_id}_{format_id}.mp4"
        file_path = os.path.join(DOWNLOAD_DIR, temp_filename)

        dl_opts = ydl_opts(
            format=f"{format_id}+bestaudio/best",
            outtmpl=file_path,
            merge_output_format="mp4",
            quiet=True,
            noplaylist=True,
        )

        if not os.path.exists(file_path):
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                ydl.download([url])

        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="File not found")

        file_size = os.path.getsize(file_path)

        if request.method == "HEAD":
            if not file_size:
                raise HTTPException(status_code=400, detail="File size not available")

            return Response(
                status_code=200,
                headers={
                    "Content-Length": str(file_size),
                    "Accept-Ranges": "bytes",
                },
            )

        start = 0
        end = file_size - 1
        status_code = 200

        if range:
            # Typical form: bytes=0-1023
            range_value = range.replace("bytes=", "").split("-")
            start = int(range_value[0])
            if range_value[1]:
                end = int(range_value[1])

            status_code = 206  # Partial Content

        content_length = (end - start) + 1
        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "Content-Disposition": f'attachment; filename="{clean_title}.mp4"',
        }

        # Full-file response: schedule cleanup after send (partial ranges keep file for retries)
        if status_code == 200:
            background_tasks.add_task(remove_file, file_path)

        return StreamingResponse(
            send_file_chunks(file_path, start, end),
            status_code=status_code,
            headers=headers,
            media_type="video/mp4",
        )
    except HTTPException:
        raise
    except Exception as e:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/download")
async def download_video(url: str, format_id: str, background_tasks: BackgroundTasks):
    with yt_dlp.YoutubeDL(ydl_opts(quiet=True)) as ydl:
        info = ydl.extract_info(url, download=False)
        video_title = info.get("title", "untitled_video")

    clean_title = slugify(video_title)
    video_id = info.get("id", "").lower()
    temp_filename = f"{video_id}_{format_id}.mp4"
    file_path = os.path.join(DOWNLOAD_DIR, temp_filename)

    dl_opts = ydl_opts(
        format=f"{format_id}+bestaudio/best",
        outtmpl=file_path,
        merge_output_format="mp4",
        quiet=True,
        noplaylist=True,
    )

    try:
        if not os.path.exists(file_path):
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                ydl.download([url])

        background_tasks.add_task(remove_file, file_path)

        return FileResponse(
            path=file_path, filename=f"{clean_title}.mp4", media_type="video/mp4"
        )

    except Exception as e:
        if os.path.exists(file_path):
            os.remove(file_path)
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
