import os
from collections.abc import Iterator


def remove_file(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)


def send_file_chunks(
    path: str,
    start: int,
    end: int,
    chunk_size: int = 1024 * 1024,
) -> Iterator[bytes]:
    """Read file in chunks within [start, end] inclusive (RFC 9110 Range)."""
    with open(path, "rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            data = f.read(min(chunk_size, remaining))
            if not data:
                break
            yield data
            remaining -= len(data)
