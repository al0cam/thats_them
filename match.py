"""Rank candidate profile photos by how well they match a blurred LinkedIn avatar.

The blurred avatar cannot be un-blurred, so we go the other way: blur every
candidate the same way and compare what survives (colour layout, hair/background
shape). For each candidate we search a small grid of blur strengths, zooms and
offsets, and keep the best score.

Usage:
    python match.py targets/2e_systems.png candidates/2e_systems/ [-o report.html]
"""

import argparse
import base64
import html
import io
import itertools
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter
from skimage.color import rgb2lab
from skimage.metrics import structural_similarity

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

BLUR_SIGMAS = [2.0, 3.0, 4.0, 5.0, 6.5, 8.0]
ZOOMS = [1.0, 0.9, 0.8]
OFFSETS = [-0.05, 0.0, 0.05]


def circle_mask(size, shrink=0.88):
    # Ignore the anti-aliased rim of the circular avatar.
    yy, xx = np.mgrid[:size, :size]
    c = (size - 1) / 2
    return (xx - c) ** 2 + (yy - c) ** 2 <= (c * shrink) ** 2


def square_crop(im, zoom, dx, dy):
    w, h = im.size
    side = min(w, h) * zoom
    cx = w / 2 + dx * w
    cy = h / 2 + dy * h
    left = min(max(cx - side / 2, 0), w - side)
    top = min(max(cy - side / 2, 0), h - side)
    return im.crop((left, top, left + side, top + side))


WORK_SCALE = 2


def crop_work(candidate, size, zoom, dx, dy):
    work = size * WORK_SCALE
    return square_crop(candidate, zoom, dx, dy).resize((work, work), Image.LANCZOS)


def blur_down(sq, size, sigma):
    # Blur at a small multiple of the target size, then downsample: this
    # roughly matches a server-side blur on a small thumbnail.
    sq = sq.filter(ImageFilter.GaussianBlur(sigma * WORK_SCALE))
    return sq.resize((size, size), Image.LANCZOS)


def render(candidate, size, sigma, zoom, dx, dy):
    return blur_down(crop_work(candidate, size, zoom, dx, dy), size, sigma)


def score(target_lab, cand_img, mask):
    lab = rgb2lab(np.asarray(cand_img, dtype=np.float64) / 255)
    delta_e = np.sqrt(((target_lab - lab) ** 2).sum(-1))[mask].mean()
    l_t = np.where(mask, target_lab[..., 0], 0)
    l_c = np.where(mask, lab[..., 0], 0)
    ssim = structural_similarity(l_t, l_c, data_range=100)
    # Lower is better: colour distance, penalised by structural mismatch.
    return delta_e * (1.5 - ssim), delta_e, ssim


def best_match(target_lab, candidate, mask):
    size = target_lab.shape[0]
    best = None
    for zoom, dx, dy in itertools.product(ZOOMS, OFFSETS, OFFSETS):
        sq = crop_work(candidate, size, zoom, dx, dy)
        for sigma in BLUR_SIGMAS:
            img = blur_down(sq, size, sigma)
            s = score(target_lab, img, mask)
            if best is None or s[0] < best["score"]:
                best = {"score": s[0], "delta_e": s[1], "ssim": s[2], "sigma": sigma,
                        "zoom": zoom, "dx": dx, "dy": dy, "render": img}
    return best


def prepare_target(target):
    target = target.convert("RGB")
    size = min(target.size)
    target = target.resize((size, size))
    return target, rgb2lab(np.asarray(target, dtype=np.float64) / 255), circle_mask(size)


def _match_file(job):
    target_lab, mask, path = job
    try:
        im = Image.open(path).convert("RGB")
    except Exception:
        return None
    r = best_match(target_lab, im, mask)
    r.update(name=Path(path).name, path=str(path), image=im)
    return r


def rank(target, files):
    """Score every candidate file against the blurred target, best first."""
    target, target_lab, mask = prepare_target(target)
    jobs = [(target_lab, mask, p) for p in files]
    with ProcessPoolExecutor() as pool:
        results = [r for r in pool.map(_match_file, jobs) if r]
    results.sort(key=lambda r: r["score"])
    return target, results


def to_data_uri(im, size=None):
    if size:
        im = im.copy()
        im.thumbnail((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def write_report(path, target, results):
    big = lambda im: to_data_uri(im.resize((144, 144), Image.LANCZOS))
    rows = []
    for pos, r in enumerate(results, 1):
        rows.append(
            f"<tr><td>{pos}</td><td><img src='{big(r['render'])}'></td>"
            f"<td><img src='{to_data_uri(r['image'], 144)}'></td>"
            f"<td>{html.escape(r['name'])}</td><td>{r['score']:.2f}</td>"
            f"<td>{r['delta_e']:.2f}</td><td>{r['ssim']:.3f}</td>"
            f"<td>{r['sigma']}</td></tr>"
        )
    Path(path).write_text(f"""<!doctype html><meta charset="utf-8"><title>Blur match report</title>
<style>body{{font-family:sans-serif;margin:24px}}img{{border-radius:50%;width:144px;height:144px;object-fit:cover}}
td,th{{padding:6px 10px;text-align:left;vertical-align:middle}}tr:nth-child(even){{background:#f4f4f4}}</style>
<h1>Blur match report</h1>
<p>Target (blurred avatar):</p><img src="{big(target)}">
<table><tr><th>#</th><th>Candidate, blurred</th><th>Candidate</th><th>File</th>
<th>Score ↓</th><th>ΔE ↓</th><th>SSIM ↑</th><th>Blur σ</th></tr>
{''.join(rows)}</table>""")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="cropped blurred avatar (square)")
    ap.add_argument("candidates", help="folder of candidate profile photos")
    ap.add_argument("-o", "--output", default="report.html")
    ap.add_argument("-n", "--top", type=int, default=10)
    args = ap.parse_args()

    files = sorted(p for p in Path(args.candidates).iterdir() if p.suffix.lower() in IMAGE_EXTS)
    if not files:
        raise SystemExit(f"No images found in {args.candidates}")

    target, results = rank(Image.open(args.target), files)

    for pos, r in enumerate(results[: args.top], 1):
        print(f"{pos:>2}. {r['name']:<40} score={r['score']:.2f}  ΔE={r['delta_e']:.2f}  "
              f"SSIM={r['ssim']:.3f}  σ={r['sigma']}")
    write_report(args.output, target, results)
    print(f"\nReport: {Path(args.output).resolve()}")


if __name__ == "__main__":
    main()
