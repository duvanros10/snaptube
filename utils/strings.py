import re
from urllib.parse import parse_qs, urlencode, urlparse


def slugify(text: str) -> str:
    # Strip characters unsafe for filesystem paths
    return re.sub(r'[\\/*?:"<>|]', "", text)


_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_YOUTUBE_SHORT_HOST = "youtu.be"


def clean_youtube_url(url: str) -> str:
    """Return a minimal YouTube URL keeping only the video identifier.

    Handles the common URL shapes:
      - https://www.youtube.com/watch?v=ID&...   → https://www.youtube.com/watch?v=ID
      - https://youtu.be/ID?...                  → https://www.youtube.com/watch?v=ID
      - https://www.youtube.com/shorts/ID?...    → https://www.youtube.com/watch?v=ID

    Non-YouTube URLs are returned unchanged.
    """
    parsed = urlparse(url)

    # Resolve short links (youtu.be/ID)
    if parsed.netloc.lower() == _YOUTUBE_SHORT_HOST:
        video_id = parsed.path.lstrip("/").split("/")[0]
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
        return url

    if parsed.netloc.lower() not in _YOUTUBE_HOSTS:
        return url

    # /shorts/<ID>
    shorts_match = re.match(r"^/shorts/([^/?#]+)", parsed.path)
    if shorts_match:
        return f"https://www.youtube.com/watch?v={shorts_match.group(1)}"

    # /watch?v=<ID>
    qs = parse_qs(parsed.query, keep_blank_values=False)
    if "v" in qs:
        return f"https://www.youtube.com/watch?{urlencode({'v': qs['v'][0]})}"

    return url
