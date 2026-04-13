import glob
import os

import yt_dlp
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse

from utils import (
    clean_youtube_url,
    format_bytes,
    get_quality_tag,
    remove_file,
    send_file_chunks,
    slugify,
)

app = FastAPI()

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "downloads")

_AUDIO_MEDIA_TYPES = {
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "webm": "audio/webm",
    "ogg": "audio/ogg",
}


def _audio_media_type_for_path(path: str) -> str:
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return _AUDIO_MEDIA_TYPES.get(ext, "application/octet-stream")


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
    url = clean_youtube_url(url)
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

            video_formats_list = []
            for f in info.get("formats", []):
                if f.get("ext") == "mp4" and f.get("vcodec") != "none":
                    quality = get_quality_tag(
                        height=f.get("height", 0), fps=f.get("fps", 0)
                    )
                    video_formats_list.append(
                        {
                            "format_id": f.get("format_id"),
                            "resolution": f.get("resolution")
                            or f"{f.get('width')}x{f.get('height')}",
                            "height": f.get("height"),
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

            audio_formats_list = []
            for f in info.get("formats", []):
                if f.get("vcodec") == "none" and f.get("acodec") != "none":
                    abr = f.get("abr")
                    tbr = f.get("tbr")
                    if abr is not None:
                        audio_quality = f"{int(round(abr))} kb/s"
                    elif tbr is not None:
                        audio_quality = f"{int(round(tbr))} kb/s"
                    else:
                        audio_quality = f.get("acodec") or "audio"
                    entry = {
                        "format_id": f.get("format_id"),
                        "extension": f.get("ext"),
                        "filesize": f.get("filesize") or f.get("filesize_approx"),
                        "filesize_formatted": format_bytes(
                            f.get("filesize") or f.get("filesize_approx")
                        ),
                        "audio_codec": f.get("acodec"),
                        "abr": abr,
                        "quality": audio_quality,
                    }
                    lang = f.get("language")
                    if lang:
                        entry["language"] = lang
                    audio_formats_list.append(entry)

            def _size_key(entry):
                s = entry.get("filesize")
                return s if s is not None else -1

            best_video_by_resolution = {}
            for entry in video_formats_list:
                res = entry.get("resolution") or ""
                if res not in best_video_by_resolution:
                    best_video_by_resolution[res] = entry
                elif _size_key(entry) > _size_key(best_video_by_resolution[res]):
                    best_video_by_resolution[res] = entry
            video_formats_list = list(best_video_by_resolution.values())

            audio_formats_list.sort(key=_size_key, reverse=True)
            audio_formats_list = audio_formats_list[:2]
            if audio_formats_list:
                best_audio = audio_formats_list[0]
                audio_formats_list.insert(
                    0,
                    {
                        "format_id": "mp3",
                        "extension": "mp3",
                        "filesize": best_audio.get("filesize"),
                        "filesize_formatted": best_audio.get("filesize_formatted"),
                        "audio_codec": "mp3 (converted)",
                        "abr": best_audio.get("abr"),
                        "quality": '128 kb/s',
                    },
                )

            return {
                "title": info.get("title"),
                "url": preview_url,
                "imageUrl": info.get("thumbnail"),
                "duration": info.get("duration_string"),
                "video_formats": video_formats_list,
                "audio_formats": audio_formats_list,
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


@app.api_route("/download/stream/video", methods=["GET", "HEAD"])
async def download_video_stream(
    request: Request,
    url: str,
    format_id: str,
    background_tasks: BackgroundTasks,
    range: str = Header(None),  # HTTP Range for resume / partial content
):
    file_path = None
    url = clean_youtube_url(url)
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


@app.api_route("/download/stream/audio", methods=["GET", "HEAD"])
async def download_audio_stream(
    request: Request,
    url: str,
    format_id: str,
    background_tasks: BackgroundTasks,
    range: str = Header(None),  # HTTP Range for resume / partial content
):
    file_path = None
    url = clean_youtube_url(url)
    try:
        with yt_dlp.YoutubeDL(ydl_opts(quiet=True)) as ydl:
            info = ydl.extract_info(url, download=False)
            video_title = info.get("title", "untitled_audio")

        clean_title = slugify(video_title)
        video_id = info.get("id", "").lower()
        want_mp3 = format_id.strip().lower() == "mp3"
        # Synthetic option from /video-details; otherwise yt-dlp audio format id (e.g. 140, 251).
        format_spec = "bestaudio/best" if want_mp3 else format_id
        temp_stem = f"{video_id}_{format_id}"
        outtmpl = os.path.join(DOWNLOAD_DIR, f"{temp_stem}.%(ext)s")

        dl_kwargs = {
            "format": format_spec,
            "outtmpl": outtmpl,
            "quiet": True,
            "noplaylist": True,
        }
        if want_mp3:
            dl_kwargs["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "128",
                }
            ]
        dl_opts = ydl_opts(**dl_kwargs)

        if want_mp3:
            file_path = os.path.join(DOWNLOAD_DIR, f"{temp_stem}.mp3")
        else:
            cached = glob.glob(os.path.join(DOWNLOAD_DIR, f"{temp_stem}.*"))
            file_path = cached[0] if len(cached) == 1 else None

        if not file_path or not os.path.exists(file_path):
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                ydl.download([url])

        if want_mp3:
            file_path = os.path.join(DOWNLOAD_DIR, f"{temp_stem}.mp3")
        else:
            candidates = glob.glob(os.path.join(DOWNLOAD_DIR, f"{temp_stem}.*"))
            if not candidates:
                raise HTTPException(status_code=404, detail="File not found")
            file_path = max(candidates, key=os.path.getmtime)

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

        out_ext = os.path.splitext(file_path)[1].lstrip(".") or "bin"
        content_length = (end - start) + 1
        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "Content-Disposition": f'attachment; filename="{clean_title}.{out_ext}"',
        }

        # Full-file response: schedule cleanup after send (partial ranges keep file for retries)
        if status_code == 200:
            background_tasks.add_task(remove_file, file_path)

        return StreamingResponse(
            send_file_chunks(file_path, start, end),
            status_code=status_code,
            headers=headers,
            media_type=_audio_media_type_for_path(file_path),
        )
    except HTTPException:
        raise
    except Exception as e:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
