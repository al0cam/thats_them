"""Find LinkedIn profiles for a company through a search engine and download
their profile photos into a candidates folder. No LinkedIn page is requested.

Default: DuckDuckGo, no key needed (unofficial, rate-limited, patchier photos).
If these are set, Google Programmable Search is used instead:
    GOOGLE_API_KEY  - API key with the Custom Search API enabled
    GOOGLE_CSE_ID   - search engine ID (cx), set to search the whole web

Usage:
    python fetch_candidates.py "2e Systems" candidates/2e_systems/
    python fetch_candidates.py "2e Systems" candidates/2e_systems/ --extra Zagreb
"""

import argparse
import csv
import json
import os
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://www.googleapis.com/customsearch/v1"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def slug(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "unknown"


def search(query, key, cx, pages):
    # The API returns at most 10 results per call and 100 per query.
    for start in range(1, pages * 10, 10):
        params = urllib.parse.urlencode({"key": key, "cx": cx, "q": query, "start": start})
        with urllib.request.urlopen(f"{API}?{params}", timeout=30) as r:
            data = json.load(r)
        items = data.get("items", [])
        yield from items
        if len(items) < 10:
            return
        time.sleep(0.5)


def photo_url(item):
    pagemap = item.get("pagemap", {})
    for meta in pagemap.get("metatags", []):
        url = meta.get("og:image", "")
        if "media.licdn.com" in url:
            return url
    for img in pagemap.get("cse_image", []):
        if "media.licdn.com" in img.get("src", ""):
            return img["src"]
    return None


FIELDS = ["name", "profile", "title", "photo"]
TITLE_SPLIT = re.compile(r"\s+[-–|]\s+")


def have_keys():
    return bool(os.environ.get("GOOGLE_API_KEY") and os.environ.get("GOOGLE_CSE_ID"))


def backend():
    return "google" if have_keys() else "duckduckgo"


def _norm(text):
    return slug(text).replace("_", " ")


def _name_in(name, text):
    # Every part of the name (accents ignored) appears in the text.
    parts = _norm(name).split()
    hay = f" {_norm(text)} "
    return bool(parts) and all(f" {p} " in hay for p in parts)


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        dest.write_bytes(r.read())


def fetch_ddg(company, outdir, extra="", per_person=40, log=print):
    """Keyless search through DuckDuckGo (unofficial, rate-limited, best effort).

    Profiles come from text search; photos from image search, accepted only when
    the URL is a LinkedIn profile photo and the title names the person.
    """
    from ddgs import DDGS

    ddg = DDGS()
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    rows = {_norm(r["name"]): r for r in load_rows(out) if r.get("name")}

    def add(name, profile="", title=""):
        key = _norm(name)
        if not key or len(key.split()) < 2:
            return None
        row = rows.setdefault(key, {"name": name, "profile": "", "title": "", "photo": ""})
        row["profile"] = row["profile"] or profile
        row["title"] = row["title"] or title
        return row

    def try_photo(row, url):
        if row["photo"] and (out / row["photo"]).exists():
            return True
        dest = out / f"{slug(row['name'])}.jpg"
        try:
            download(url, dest)
        except Exception:
            return False  # LinkedIn image URLs expire; another result may still work
        row["photo"] = dest.name
        log(f"✓ {row['name']}")
        return True

    text_queries = [f'site:linkedin.com/in "{company}" {extra}',
                    f'"{company}" linkedin {extra}',
                    f'"at {company}" linkedin',
                    f'"{company}" developer OR engineer OR manager OR analyst linkedin {extra}']
    # Country subdomains are indexed separately and surface different people.
    text_queries += [f'site:{cc}.linkedin.com/in "{company}"' for cc in ("hr", "de", "uk", "au", "at", "si", "rs", "ro")]
    for q in text_queries:
        try:
            for r in ddg.text(q.strip(), max_results=60):
                if "linkedin.com/in/" in r["href"]:
                    add(TITLE_SPLIT.split(r["title"])[0].strip(), r["href"].split("?")[0], r["title"])
        except Exception as e:
            log(f"  ! text search failed: {e}")
        time.sleep(1)

    # Company-wide image search also surfaces people the text search missed.
    for q in (f'"{company}" linkedin', f'"{company}" linkedin profile {extra}'):
        try:
            for img in ddg.images(q.strip(), max_results=100):
                title = img.get("title", "")
                if "profile-displayphoto" in img["image"] and _norm(company) in _norm(title):
                    row = add(TITLE_SPLIT.split(title)[0].strip(), title=title)
                    if row:
                        try_photo(row, img["image"])
        except Exception as e:
            log(f"  ! image search failed: {e}")
        time.sleep(1)

    missing = [r for r in rows.values() if not r["photo"]][:per_person]
    for row in missing:
        try:
            imgs = ddg.images(f'"{row["name"]}" "{company}" linkedin', max_results=30)
        except Exception as e:
            log(f"  ! image search failed for {row['name']}: {e}")
            time.sleep(3)
            continue
        for img in imgs:
            if "profile-displayphoto" in img["image"] and _name_in(row["name"], img.get("title", "")):
                if try_photo(row, img["image"]):
                    break
        time.sleep(1)

    rows = list(rows.values())
    with open(out / "candidates.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    return rows


def load_rows(outdir):
    path = Path(outdir) / "candidates.csv"
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def fetch(company, outdir, extra="", log=print):
    """Search with Google if keys are set, otherwise DuckDuckGo; write candidates.csv."""
    if have_keys():
        return fetch_google(company, outdir, extra, log=log)
    return fetch_ddg(company, outdir, extra, log=log)


def fetch_google(company, outdir, extra="", pages=5, log=print):
    """Search, download photos (skipping ones already on disk), write candidates.csv."""
    key, cx = os.environ["GOOGLE_API_KEY"], os.environ["GOOGLE_CSE_ID"]

    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    queries = [f'site:linkedin.com/in "{company}" {extra}'.strip(),
               f'site:hr.linkedin.com/in "{company}" {extra}'.strip()]

    rows = {r["profile"].rsplit("/", 1)[-1]: r for r in load_rows(out)}
    for q in queries:
        for item in search(q, key, cx, pages):
            link = item["link"].split("?")[0].rstrip("/")
            handle = link.rsplit("/", 1)[-1]
            if handle in rows and rows[handle]["photo"]:
                continue
            name = item.get("title", "").split(" - ")[0].strip()
            url = photo_url(item)
            row = {"name": name, "profile": link, "title": item.get("title", ""), "photo": ""}
            if url:
                dest = out / f"{slug(name)}__{slug(handle)}.jpg"
                try:
                    download(url, dest)
                    row["photo"] = dest.name
                except Exception as e:
                    log(f"  ! photo download failed for {name}: {e}")
            rows[handle] = row
            log(f"{'✓' if row['photo'] else '·'} {name:<35} {link}")

    rows = list(rows.values())
    with open(out / "candidates.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("company")
    ap.add_argument("outdir")
    ap.add_argument("--extra", default="", help="extra search terms, e.g. a city")
    args = ap.parse_args()

    print(f"Searching with {backend()}…")
    rows = fetch(args.company, args.outdir, args.extra)
    got = sum(1 for r in rows if r["photo"])
    print(f"\n{len(rows)} profiles found, {got} photos saved to {args.outdir}/")


if __name__ == "__main__":
    main()
