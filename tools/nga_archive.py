"""
NGA full-fidelity archiver — lossless local mirror of strategy threads.

Rationale: import_nga.py distills pages straight into strategy YAML — the
HTML is discarded (inner_text loses tables/structure) and images are
FILTERED by a fleet-screenshot score (1-1: 30 urls → 1 saved). Distillation
choices can't be revisited without the source. This tool archives first:

  data/nga_archive/<map_id>/
    meta.json        url, title, fetched_at, image manifest
    page.html        full rendered DOM (after expanding collapse sections)
    page.txt         inner_text snapshot (for quick grep)
    img/NNN_<name>   EVERY inline image, unfiltered

Distill later, repeatably, with images alongside text (a VLM pass can read
fleet screenshots and route maps that text extraction loses entirely).

Thread list comes from existing data/nga_raw/*/*/meta.json (36 maps) plus
any extra URLs given on the command line.

Usage:
  python tools/nga_archive.py                 # archive all known threads
  python tools/nga_archive.py --maps 5-4 5-5  # subset
  python tools/nga_archive.py --url 1-2=https://bbs.nga.cn/read.php?pid=…
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.import_nga import NGA_SESSION_PATH, NGA_UA, _playwright_import  # noqa: E402

ARCHIVE_DIR = ROOT / "data" / "nga_archive"
RAW_DIR = ROOT / "data" / "nga_raw"
THROTTLE_S = 3.0


def known_threads() -> dict[str, str]:
    """map_id → thread url, from prior import runs."""
    out: dict[str, str] = {}
    for meta in sorted(RAW_DIR.glob("*/*/meta.json")):
        try:
            d = json.loads(meta.read_text())
            if d.get("map_id") and d.get("url"):
                out[d["map_id"]] = d["url"]
        except Exception:
            pass
    return out


def _safe_name(url: str, idx: int) -> str:
    tail = re.sub(r"[^A-Za-z0-9._-]", "_", url.split("/")[-1].split("?")[0])[-60:]
    return f"{idx:03d}_{tail or 'img'}"


def archive_thread(map_id: str, url: str, force: bool = False) -> dict:
    dest = ARCHIVE_DIR / map_id
    if (dest / "page.html").exists() and not force:
        return {"map_id": map_id, "skipped": True}
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "img").mkdir(exist_ok=True)

    sync_playwright = _playwright_import()
    kwargs: dict = {}
    if NGA_SESSION_PATH.exists():
        kwargs["storage_state"] = str(NGA_SESSION_PATH)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=NGA_UA, locale="zh-CN", **kwargs)
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # NGA redirect page + ad interstitial (same dance as import_nga)
        for _ in range(4):
            if len(page.inner_text("body")) > 500:
                break
            clicked = False
            for link_text in ("点此链接", "点此跳过广告", "跳过广告"):
                try:
                    link = page.locator("a").filter(has_text=link_text).first
                    link.wait_for(timeout=2000)
                    link.click()
                    page.wait_for_load_state("networkidle", timeout=20000)
                    clicked = True
                    break
                except Exception:
                    pass
            if not clicked:
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                break

        # Expand every AJAX collapse section so the saved DOM is complete
        try:
            for btn in page.locator("div.collapse_btn button").all():
                try:
                    btn.click(timeout=1500)
                    page.wait_for_timeout(250)
                except Exception:
                    pass
        except Exception:
            pass
        page.wait_for_timeout(800)

        html = page.content()
        text = page.inner_text("body")
        title = page.title()

        # Every inline image, no scoring, fetched WITH the page's session
        img_urls = []
        for el in page.locator("img").all():
            src = el.get_attribute("src") or ""
            if src.startswith("http") and "nga" in src.split("/")[2]:
                img_urls.append(src)
        img_urls = list(dict.fromkeys(img_urls))   # dedupe, keep order

        manifest = []
        for i, src in enumerate(img_urls):
            name = _safe_name(src, i)
            try:
                resp = page.request.get(src, timeout=20000)
                if resp.ok:
                    (dest / "img" / name).write_bytes(resp.body())
                    manifest.append({"file": name, "url": src})
                else:
                    manifest.append({"file": None, "url": src,
                                     "error": resp.status})
            except Exception as e:
                manifest.append({"file": None, "url": src, "error": str(e)})
            time.sleep(0.4)

        browser.close()

    (dest / "page.html").write_text(html)
    (dest / "page.txt").write_text(text)
    meta = {"map_id": map_id, "url": url, "title": title,
            "fetched_at": datetime.now().isoformat(),
            "html_bytes": len(html), "text_chars": len(text),
            "images_total": len(img_urls),
            "images_saved": sum(1 for m in manifest if m["file"]),
            "images": manifest}
    (dest / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    return meta


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--maps", nargs="*", help="subset of map ids (e.g. 5-4)")
    ap.add_argument("--url", action="append", default=[],
                    help="extra MAPID=URL pairs")
    ap.add_argument("--force", action="store_true", help="re-archive existing")
    args = ap.parse_args()

    threads = known_threads()
    for pair in args.url:
        mid, _, u = pair.partition("=")
        threads[mid] = u
    if args.maps:
        threads = {m: u for m, u in threads.items() if m in args.maps}
    if not threads:
        print("no threads known — run import_nga first or pass --url")
        sys.exit(1)

    print(f"archiving {len(threads)} threads → {ARCHIVE_DIR}")
    for i, (mid, url) in enumerate(sorted(threads.items()), 1):
        try:
            m = archive_thread(mid, url, force=args.force)
            if m.get("skipped"):
                print(f"[{i}/{len(threads)}] {mid}: already archived")
            else:
                print(f"[{i}/{len(threads)}] {mid}: html {m['html_bytes']//1024}KB, "
                      f"text {m['text_chars']} chars, "
                      f"images {m['images_saved']}/{m['images_total']}")
        except Exception as e:
            print(f"[{i}/{len(threads)}] {mid}: FAILED {e}", file=sys.stderr)
        time.sleep(THROTTLE_S)


if __name__ == "__main__":
    main()
