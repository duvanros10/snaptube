import re


def slugify(text: str) -> str:
    # Strip characters unsafe for filesystem paths
    return re.sub(r'[\\/*?:"<>|]', "", text)
