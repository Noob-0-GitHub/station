"""The `view` tool: turn an image file in the AI's home into an image block the model can see.

Images are scaled so the long side is at most MAX_SIDE pixels (the size Claude and GPT read at full
detail anyway) and re-encoded when that makes them smaller. The block is stored in the thread's
history like any tool result; old ones are cleared by prune like old tool outputs.
"""

from __future__ import annotations

import base64
import io
import os
from pathlib import Path

from .runner import inside

MAX_SIDE = 1568
MAX_FILE = 30_000_000  # bytes read from disk, at most
MAX_PIXELS = 60_000_000  # refuse decompression bombs
MAX_SEND = 1_500_000  # bytes of encoded image kept as is; larger ones are re-encoded as JPEG
FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}
# A rough size of an image in the context, in characters of text (about 1,600 tokens at full size).
IMAGE_CHARS = 6400


def is_image(block) -> bool:
    return isinstance(block, dict) and block.get("type") == "image"


def has_image(content) -> bool:
    return isinstance(content, list) and any(is_image(b) for b in content)


def load(path: str, home) -> tuple[list, str]:
    """Return (tool result content with the image, one-line description)."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    if not path:
        raise ValueError("path is required")
    if path == "~" or path.startswith("~/"):  # ~ is the AI's home, not the home of the user Station runs as
        p = Path(home) / path[2:]
    else:
        p = Path(path) if Path(path).is_absolute() else Path(home) / path
    if not inside(p, home):
        raise ValueError(f"{path} is outside your home; copy it into your home first")
    real = Path(os.path.realpath(p))
    if not real.is_file():
        raise ValueError(f"{path}: no such file")
    size = real.stat().st_size
    if size > MAX_FILE:
        raise ValueError(f"{path} is {size / 1e6:.0f} MB; at most {MAX_FILE / 1e6:.0f} MB")
    raw = real.read_bytes()
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError) as e:
        raise ValueError(f"{path} is not an image this tool can read ({e})") from None
    fmt, width, height = img.format, img.width, img.height
    frames = getattr(img, "n_frames", 1)
    img = ImageOps.exif_transpose(img)
    scale = min(1.0, MAX_SIDE / max(img.width, img.height))
    if fmt in FORMATS and scale == 1.0 and len(raw) <= MAX_SEND and frames == 1:
        data, media = raw, FORMATS[fmt]
    else:
        if scale < 1.0:
            img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.Resampling.LANCZOS)
        alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
        buf = io.BytesIO()
        if alpha:
            img.convert("RGBA").save(buf, "PNG", optimize=True)
        if not alpha or buf.tell() > MAX_SEND:
            flat = img.convert("RGBA") if alpha else img.convert("RGB")
            if alpha:
                bg = Image.new("RGB", flat.size, "white")
                bg.paste(flat, mask=flat.getchannel("A"))
                flat = bg
            buf = io.BytesIO()
            flat.save(buf, "JPEG", quality=85, optimize=True)
            media = "image/jpeg"
        else:
            media = "image/png"
        data = buf.getvalue()
    shown = f"{img.width}x{img.height}" if (img.width, img.height) != (width, height) else ""
    desc = (f"{path}: {fmt or '?'} {width}x{height}" + (f", shown at {shown}" if shown else "")
            + (f", first of {frames} frames" if frames > 1 else ""))
    block = {"type": "image", "source": {"type": "base64", "media_type": media,
                                         "data": base64.b64encode(data).decode()}}
    return [{"type": "text", "text": desc}, block], desc


def text_of(content) -> str:
    """The text part of a tool result, whether a string or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""
