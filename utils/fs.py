import os
from collections.abc import Iterator


def remove_file(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)


def send_file_chunks(path: str, start: int, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    """Read file in chunks from a byte offset (for Range requests)."""
    with open(path, "rb") as f:
        f.seek(start)
        while chunk := f.read(chunk_size):
            yield chunk
