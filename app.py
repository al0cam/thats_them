"""Local web UI: paste a blurred avatar plus a hint like "works at 2e Systems",
get the candidate profiles whose photos match it best.

Run:
    .venv/bin/python app.py      then open http://127.0.0.1:5000
"""

import base64
import io
import re
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from PIL import Image

import fetch_candidates
from match import IMAGE_EXTS, rank, to_data_uri

ROOT = Path(__file__).parent
CANDIDATES = ROOT / "candidates"

app = Flask(__name__, static_folder=None)

HINT_PREFIX = re.compile(
    r"^\s*(\d+\s+)?(someone|somebody|person|people|viewer)?\s*"
    r"(who\s+)?(works|worked|working)?\s*(at|from|@|in)?\s+", re.I)


def parse_hint(text):
    """'1 works at 2e Systems, Zagreb' -> ('2e Systems', 'Zagreb')."""
    company, _, extra = text.partition(",")
    company = HINT_PREFIX.sub("", " " + company).strip(" .\"'")
    return company, extra.strip()


@app.get("/")
def index():
    return send_from_directory(ROOT / "static", "index.html")


@app.get("/photos/<folder>/<path:name>")
def photo(folder, name):
    return send_from_directory(CANDIDATES / folder, name)


@app.post("/api/match")
def api_match():
    body = request.get_json(force=True)
    company, extra = parse_hint(body.get("hint", ""))
    if not company:
        return jsonify(error="Couldn't read a company name from the message."), 400
    try:
        data = body["image"].split(",", 1)[1]
        target = Image.open(io.BytesIO(base64.b64decode(data)))
    except Exception:
        return jsonify(error="Couldn't read the pasted image."), 400

    folder = fetch_candidates.slug(company)
    outdir = CANDIDATES / folder
    notes = []
    cached = fetch_candidates.load_rows(outdir)
    if body.get("refresh") or not cached:
        try:
            rows = fetch_candidates.fetch(company, outdir, extra, log=lambda m: None)
            found = sum(1 for r in rows if r["photo"])
            notes.append(f"Searched {fetch_candidates.backend()}: {len(rows)} people found, "
                         f"{found} with a photo.")
        except Exception as e:
            notes.append(f"Search failed: {e}")
    else:
        notes.append("Used cached search results (tick “search again” to refresh).")

    files = sorted(p for p in outdir.glob("*") if p.suffix.lower() in IMAGE_EXTS) if outdir.exists() else []
    if not files:
        return jsonify(company=company, extra=extra, notes=notes, results=[],
                       error=f"No candidate photos for “{company}”."), 200

    meta = {r["photo"]: r for r in fetch_candidates.load_rows(outdir) if r.get("photo")}
    target, results = rank(target, files)
    out = []
    for r in results[: int(body.get("top", 12))]:
        m = meta.get(r["name"], {})
        out.append({
            "file": r["name"],
            "name": m.get("name") or Path(r["name"]).stem.split("__")[0].replace("_", " ").title(),
            "title": m.get("title", ""),
            "profile": m.get("profile", ""),
            "photo": f"/photos/{folder}/{r['name']}",
            "blurred": to_data_uri(r["render"].resize((96, 96), Image.LANCZOS)),
            "score": round(r["score"], 2),
            "delta_e": round(r["delta_e"], 2),
            "ssim": round(r["ssim"], 3),
        })
    return jsonify(company=company, extra=extra, notes=notes, total=len(results),
                   target=to_data_uri(target.resize((96, 96), Image.LANCZOS)), results=out)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
