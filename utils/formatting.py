import math


def format_bytes(size_bytes):
    if size_bytes is None:
        return None
    if size_bytes == 0:
        return "0B"
    units = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {units[i]}"


def get_quality_tag(height: int, fps: int = 0) -> str:
    if not height:
        return None

    if height >= 2160:
        tag = f"{height}p 4K"
    elif height >= 1440:
        tag = "1440p (2K)"
    elif height >= 1080:
        tag = "1080p HD"
    elif height >= 720:
        tag = "720p HD"
    elif height >= 480:
        tag = "480p SD"
    elif height >= 360:
        tag = "360p"
    else:
        tag = f"{height}p"

    # High frame rate: insert fps before the text suffix (e.g. 1080p60 HD)
    if fps >= 50:
        if " " in tag:
            parts = tag.split(" ", 1)
            return f"{parts[0]}{int(fps)} {parts[1]}"
        return f"{tag}{int(fps)}"

    return tag
