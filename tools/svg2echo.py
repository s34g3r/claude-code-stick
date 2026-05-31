#!/usr/bin/env python3
"""SVG → Echo-compatible 135×120 GIF.

Renders via Playwright at high scale, then crops to the union alpha bbox
across all frames, scales the cropped sprite to fit 135×120 preserving
aspect ratio, and centers on a transparent 135×120 canvas. Output is a
palettized GIF with index-0 transparency and disposal=2, matching the
format the ESP32 AnimatedGIF + character.cpp pipeline expects.

Mirrors the visual fill of the existing 19 clawd GIFs (sprite occupies
~70-85% of canvas, centered) rather than the raw SVG render (which places
the sprite at the bottom of the viewBox at small size).
"""
import io
import sys
import shutil
import tempfile
from pathlib import Path

sys.path.insert(0, "/tmp/clawd-tank/tools")
from svg2frames import detect_animation_duration, render_frames  # noqa: E402

from PIL import Image  # noqa: E402

TARGET_W = 135
TARGET_H = 120
SCALE = 6.0  # render large, downscale crisp
FPS = 12.0
MAX_FRAMES = 60


def union_bbox(frame_paths):
    bbox = None
    for p in frame_paths:
        img = Image.open(p).convert("RGBA")
        fb = img.getbbox()
        if fb is None:
            continue
        if bbox is None:
            bbox = fb
        else:
            bbox = (
                min(bbox[0], fb[0]),
                min(bbox[1], fb[1]),
                max(bbox[2], fb[2]),
                max(bbox[3], fb[3]),
            )
    return bbox


def fit_and_center(frame_paths, bbox):
    """Crop each frame to bbox, scale to fit 135x120 preserving aspect,
    center on transparent 135x120 canvas."""
    bw = bbox[2] - bbox[0]
    bh = bbox[3] - bbox[1]
    scale = min(TARGET_W / bw, TARGET_H / bh)
    new_w = max(1, round(bw * scale))
    new_h = max(1, round(bh * scale))
    off_x = (TARGET_W - new_w) // 2
    off_y = (TARGET_H - new_h) // 2

    out_frames = []
    for p in frame_paths:
        img = Image.open(p).convert("RGBA").crop(bbox)
        img = img.resize((new_w, new_h), Image.NEAREST)  # pixel-art crisp
        canvas = Image.new("RGBA", (TARGET_W, TARGET_H), (0, 0, 0, 0))
        canvas.paste(img, (off_x, off_y), img)
        out_frames.append(canvas)
    return out_frames, new_w, new_h


def palettize_with_transparency(frame: Image.Image) -> Image.Image:
    alpha = frame.split()[-1]
    mask = alpha.point(lambda a: 255 if a > 128 else 0)
    rgb = Image.new("RGB", frame.size, (0, 0, 0))
    rgb.paste(frame, mask=mask)
    pal = rgb.quantize(colors=255, method=Image.MEDIANCUT, dither=Image.NONE)
    idx_arr = pal.load()
    out = Image.new("P", pal.size, 0)
    out_px = out.load()
    mask_px = mask.load()
    w, h = pal.size
    for y in range(h):
        for x in range(w):
            out_px[x, y] = 0 if mask_px[x, y] == 0 else idx_arr[x, y] + 1
    src_pal = pal.getpalette()
    new_pal = [0, 0, 0] + src_pal[: 255 * 3]
    if len(new_pal) < 768:
        new_pal += [0] * (768 - len(new_pal))
    out.putpalette(new_pal)
    out.info["transparency"] = 0
    return out


def convert(svg_path: Path, out_path: Path) -> None:
    svg_text = svg_path.read_text(encoding="utf-8")
    duration = detect_animation_duration(svg_text)
    eff_fps = FPS
    if duration * FPS > MAX_FRAMES:
        eff_fps = max(2.0, MAX_FRAMES / duration)
        print(f"  long animation {duration:.1f}s → reducing fps {FPS} -> {eff_fps:.1f}")

    tmp = Path(tempfile.mkdtemp(prefix="svg2echo_"))
    try:
        saved, cw, ch = render_frames(
            svg_path=svg_path,
            output_dir=tmp,
            fps=eff_fps,
            duration=duration,
            scale=SCALE,
            background="transparent",
        )
        bbox = union_bbox(saved)
        if bbox is None:
            print(f"  WARN: {svg_path.name} fully transparent, skipped")
            return
        frames, nw, nh = fit_and_center(saved, bbox)
        palette_frames = [palettize_with_transparency(f) for f in frames]
        duration_ms = int(round(1000.0 / eff_fps))
        palette_frames[0].save(
            out_path,
            save_all=True,
            append_images=palette_frames[1:],
            duration=duration_ms,
            loop=0,
            disposal=2,
            transparency=0,
            optimize=False,
        )
        sz_kb = out_path.stat().st_size / 1024
        print(f"  → {out_path.name}: sprite {nw}×{nh} on {TARGET_W}×{TARGET_H}, "
              f"{len(frames)} frames, {duration_ms}ms/frame, {sz_kb:.1f} KB")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# (svg stem, output filename matching character.cpp STATE_NAMES convention)
TARGETS = [
    ("clawd-eureka",              "eureka.gif"),
    ("clawd-grooving",            "grooving.gif"),
    ("clawd-hat-mishap",          "hat_mishap.gif"),
    ("clawd-idle-low-battery",    "idle_low_battery.gif"),
    ("clawd-wake",                "wake.gif"),
    ("clawd-working-builder",     "working_builder.gif"),
    ("clawd-working-carrying",    "working_carrying.gif"),
    ("clawd-working-pushing",     "working_pushing.gif"),
    ("clawd-crab-walking",        "crab_walking.gif"),
]


def main():
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

    in_dir = Path("/tmp/clawd-tank/assets/svg-animations")
    out_dir = Path("/tmp/clawd-echo-gifs")
    out_dir.mkdir(parents=True, exist_ok=True)

    only = sys.argv[1] if len(sys.argv) > 1 else None
    targets = [t for t in TARGETS if not only or t[0] == only or t[1] == only]
    if not targets:
        print(f"No matches for filter '{only}'")
        sys.exit(1)

    for svg_stem, out_name in targets:
        svg_path = in_dir / f"{svg_stem}.svg"
        if not svg_path.exists():
            print(f"MISSING: {svg_path}")
            continue
        print(f"\n[{svg_stem}.svg]")
        try:
            convert(svg_path, out_dir / out_name)
        except Exception as e:
            print(f"  ERROR: {e}")


if __name__ == "__main__":
    main()
