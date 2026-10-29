"""Identify an image format from its leading bytes, never from a file name."""

_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

SUPPORTED_FORMATS = "JPEG, PNG, WebP or GIF"


def detect_media_type(data: bytes) -> str | None:
    """MIME type of a JPEG, PNG, WebP or GIF image; None for anything else."""
    for signature, media_type in _SIGNATURES:
        if data.startswith(signature):
            return media_type
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None
