"""Turn a post's photos and videos into files Instagram accepts.

Photos/videos outside Instagram's allowed shapes are padded (never cropped) with
PAD_COLOR, black like the app's letterboxing. All items in a carousel are padded
to the same shape, because Instagram shows a carousel at the first item's shape.
Needs Pillow, and ffmpeg for videos: the one on PATH, else the copy that comes
with the imageio-ffmpeg package (so a pip install is enough).
"""
import json
import math
import re
import shutil
import statistics
import subprocess
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

IMAGE_RATIO = (4 / 5, 1.91)
VIDEO_RATIO = (4 / 5, 16 / 9)
MAX_WIDTH = 1440
MIN_VIDEO = 3.0
PAD_COLOR = "black"  # bars around photos/videos that aren't an allowed shape
API_MAX_CAROUSEL = 10  # posting by hand allows 20; the publishing API is documented at 10


class PostError(Exception):
    pass


@lru_cache(maxsize=None)
def ffmpeg():
    """The ffmpeg to use: on PATH, else imageio-ffmpeg's bundled one."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        raise PostError("ffmpeg isn't installed: pip install imageio-ffmpeg (or put ffmpeg on the PATH)") from None


def run(*cmd, cwd=None):
    if cmd and cmd[0] == "ffmpeg":
        cmd = (ffmpeg(), *cmd[1:])
    out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if out.returncode:
        raise PostError(f"{cmd[0]} {cmd[1]} failed: {out.stderr.strip()[-500:]}")
    return out.stdout.strip()


def ffmpeg_info(path):
    """What `ffmpeg -i` says about a file (it exits 1: no output given, which is expected)."""
    r = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.stderr


def probe(path):
    """(width, height, seconds) of a video, as shown (a phone video rotated 90° swaps width and height).
    Read from ffmpeg's own description, so ffprobe isn't needed."""
    info = ffmpeg_info(path)
    video = next((l for l in info.splitlines() if re.search(r"Stream #.*Video:", l)), None)
    if not video:
        raise PostError(f"no video stream in {Path(path).name}")
    m = re.search(r"(?<!\w)(\d{2,5})x(\d{2,5})(?!\w)", video)
    if not m:
        raise PostError(f"couldn't read the size of {Path(path).name}")
    w, h = int(m[1]), int(m[2])
    rot = re.search(r"(?:rotate\s*:\s*|rotation of )(-?\d+(?:\.\d+)?)", info)
    if rot and abs(round(float(rot[1]))) % 180 == 90:
        w, h = h, w
    d = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", info)
    seconds = int(d[1]) * 3600 + int(d[2]) * 60 + float(d[3]) if d else 0.0
    return w, h, seconds


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def padded_size(w, h, ratio, bounds, step=1):
    """Smallest size >= (w, h) with shape ~ratio that rounding can't push outside bounds."""
    lo, hi = bounds
    if w / h < ratio:
        w = math.ceil(h * ratio / step) * step
        while w / h > hi:
            w -= step
    else:
        h = math.ceil(w / ratio / step) * step
        while w / h < lo:
            h -= step
    return w, h


def pad_image(src, dest, ratio, bounds=IMAGE_RATIO):
    img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    w, h = img.size
    if ratio is not None:
        cw, ch = padded_size(w, h, ratio, bounds)
        canvas = Image.new("RGB", (cw, ch), PAD_COLOR)
        canvas.paste(img, ((cw - w) // 2, (ch - h) // 2))
        img = canvas
    if img.width > MAX_WIDTH:
        img = img.resize((MAX_WIDTH, round(img.height * MAX_WIDTH / img.width)), Image.LANCZOS)
    img.save(dest, "JPEG", quality=92)


def fix_color_tags(src):
    """Some downloaded videos declare 'reserved' color values, which ffmpeg refuses
    to filter. Rewrite those tags as BT.709 without re-encoding."""
    video = next((l for l in ffmpeg_info(src).splitlines() if re.search(r"Stream #.*Video:", l)), "")
    if "reserved" not in video:
        return src
    codec = re.search(r"Video: (\w+)", video)
    bsf = {"h264": "h264_metadata", "hevc": "hevc_metadata"}.get(codec[1] if codec else "")
    if not bsf:
        raise PostError(f"video has invalid color tags and is {codec[1] if codec else '?'}, which can't be retagged")
    fixed = src.with_name(src.stem + "-fixed" + src.suffix)
    run("ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-c", "copy", "-bsf:v",
        f"{bsf}=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1", str(fixed))
    return fixed


def encode_video(src, dest, ratio, seconds, loop_short=True):
    src = fix_color_tags(src)
    seconds = seconds if loop_short else 0
    # The publishing API rejects videos under 3 s, so loop them (seconds=0 turns this off)
    loops = math.ceil(MIN_VIDEO / seconds) - 1 if 0 < seconds < MIN_VIDEO else 0
    w, h, _ = probe(src)
    scale = 540 / w if w < 540 else 1080 / w if w > 1080 else 1  # upscale tiny GIFs, cap at 1080 wide
    w, h = int(w * scale) // 2 * 2, int(h * scale) // 2 * 2
    filters = [f"scale={w}:{h}"]
    if ratio is not None:
        pw, ph = padded_size(w, h, ratio, VIDEO_RATIO, step=2)
        filters.append(f"pad={pw}:{ph}:(ow-iw)/2:(oh-ih)/2:{PAD_COLOR}")
    run("ffmpeg", "-y", "-loglevel", "error", *(["-stream_loop", str(loops)] if loops else []),
        "-i", str(src), "-vf", ",".join(filters), "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-preset", "medium", "-crf", "21", "-r", "30", "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-movflags", "+faststart", str(dest))


def prepare(name, media, workdir, fetch, loop_short=True):
    """Fetch and convert a post's media. `media` is a list of {"type": "image"|"video"|"gif",
    "path": ...} (plus whatever `fetch` needs); fetch(m) returns the file's bytes.
    Returns [(kind, local path)] ready to upload."""
    folder = workdir / name
    folder.mkdir(parents=True, exist_ok=True)
    raw = []
    for i, m in enumerate(media, 1):
        src = folder / f"src-{i}{Path(m['path']).suffix.lower()}"
        src.write_bytes(fetch(m))
        if m["type"] == "image":
            try:
                with Image.open(src) as im:
                    w, h = ImageOps.exif_transpose(im).size
                raw.append(("image", src, w / h, 0))
            except Exception:  # formats Pillow can't read (e.g. some AVIF): convert via ffmpeg
                png = src.with_suffix(".png")
                run("ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-frames:v", "1", str(png))
                with Image.open(png) as im:
                    raw.append(("image", png, im.width / im.height, 0))
        else:
            w, h, seconds = probe(src)
            raw.append(("video", src, w / h, seconds))

    carousel = len(raw) > 1
    bounds = VIDEO_RATIO if any(k == "video" for k, *_ in raw) else IMAGE_RATIO
    if carousel:
        target = clamp(statistics.median(r for _, _, r, _ in raw), *bounds)
    out = []
    for i, (kind, src, r, seconds) in enumerate(raw, 1):
        if kind == "image":
            ratio = target if carousel else (None if bounds[0] <= r <= bounds[1] else clamp(r, *bounds))
            dest = folder / f"{name}-{i}.jpg"
            pad_image(src, dest, ratio, bounds)
        else:
            ratio = target if carousel else None  # Reels accept any shape
            dest = folder / f"{name}-{i}.mp4"
            encode_video(src, dest, ratio, seconds, loop_short)
        out.append((kind, dest))
    same_size_photos(out)
    return out


def same_size_photos(items):
    """Instagram rejects a carousel whose photos aren't exactly the same shape, and
    prepare()'s rounding can leave one a pixel off (1440x1799 next to 1152x1440).
    Resize every photo to the smallest one's size; the shapes already match to
    within a fraction of a percent, so nothing visibly stretches."""
    photos = [path for kind, path in items if kind == "image"]
    if len(items) < 2 or len(photos) < 2:
        return
    sizes = {}
    for path in photos:
        with Image.open(path) as im:
            sizes[path] = im.size
    target = min(sizes.values())
    for path, size in sizes.items():
        if size != target:
            with Image.open(path) as im:
                im.convert("RGB").resize(target, Image.LANCZOS).save(path, "JPEG", quality=92)
