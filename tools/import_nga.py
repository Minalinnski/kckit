#!/usr/bin/env python3
"""
NGA strategy import tool — two-phase pipeline:
  Phase 1 (--fetch): download NGA pages + fleet screenshots → data/nga_raw/world{N}/{map}/
  Phase 2 (--build): load raw data, call Claude with text+images interleaved → strategies/*.yaml

Usage:
  # Full pipeline (fetch + build)
  python tools/import_nga.py --batch 5-5
  python tools/import_nga.py --batch all --overwrite

  # Separate phases
  python tools/import_nga.py --fetch all             # download raw data for all 36 maps
  python tools/import_nga.py --build 5-4,5-5         # regenerate YAMLs from saved raw

  # Single map with custom input
  python tools/import_nga.py --map 5-4 --text nga_5-4.txt
  python tools/import_nga.py --map 5-4 --img screenshots/5-4/*.png
  python tools/import_nga.py --map 5-4 --url https://bbs.nga.cn/read.php?pid=...

  # Auth
  python tools/import_nga.py --nga-login             # save NGA session for future fetches
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import anthropic
import yaml
from rapidfuzz import process, fuzz

# Load .env from project root if present
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

# Add parent dir to path so we can import core
sys.path.insert(0, str(Path(__file__).parent.parent))
from core.schema import (
    MapStrategy, merge_strategy, save_strategy,
    QuestIndex, save_quests, map_yaml_path,
    QuestEntry, save_quest, quest_yaml_path,
)

log = logging.getLogger(__name__)

STRATEGIES_DIR = Path(__file__).parent.parent / "strategies"
MAPS_DIR = STRATEGIES_DIR / "maps"
QUESTS_DIR = STRATEGIES_DIR / "quests"
EQUIP_DB_PATH = Path(__file__).parent.parent / "data" / "equip_names.json"
EQUIP_ALIASES_PATH = Path(__file__).parent.parent / "data" / "equip_aliases.json"
NGA_IMAGES_DIR = Path(__file__).parent.parent / "data" / "nga_images"
NGA_RAW_DIR = Path(__file__).parent.parent / "data" / "nga_raw"


def raw_dir(map_id: str) -> Path:
    world_num = map_id.split("-")[0]
    return NGA_RAW_DIR / f"world{world_num}" / map_id

SYSTEM_PROMPT = """\
You are a Kantai Collection (KanColle) strategy analyst with full game knowledge.
Extract ALL fleet composition variants from the provided NGA forum post.

━━━ OUTPUT SCHEMA ━━━
{
  "map": "X-X",
  "notes": "one-line map overview",
  "presets": [{
    "name": "variant name",
    "fleet": {
      "type": "single",
      "slots": [{
        "ship_class": ["DD"],
        "count": 1,
        "min_level": null,
        "min_asw": null,
        "speed": null,
        "specific_ships": ["矢矧改二乙"],
        "notes": "旗舰"
      }]
    },
    "requirements": {
      "air_state": "none",
      "air_power_min": null,
      "enemy_air_power": null,
      "los_formula": 33,
      "los_min": null,
      "routing_nodes": ["F","G","K"]
    },
    "equip_notes": "key numbers: air power, LoS, ASW thresholds",
    "example": [{"ship": "矢矧改二乙", "equips": ["15.2cm三連装砲改","夜間瑞雲"]}],
    "special_attacks": [{
      "type": "nagato_touch",
      "ships": ["長門改二","陸奥改二"],
      "flagship": "長門改二",
      "positions": [1,2],
      "equip_hint": "電探+徹甲弾",
      "notes": ""
    }],
    "tags": []
  }]
}

━━━ SHIP CLASS VALUES ━━━
DE, DD, CL, CLT, CA, CAV, CVL, BB, FBB, BBV, CV, SS, SSV, AV, LHA, AS, AO
  DE = 海防艦 (escort ship, e.g. 択捉, 日振, 宗谷, 丹陽)
Parenthetical = alternatives:
  BB(V)  → ["BB","BBV"]    CV(L) → ["CV","CVL"]    CA(V) → ["CA","CAV"]
  FBB(BB)→ ["FBB","BB"]    CVB   → ["CV"]  (装甲空母, treat as CV)

━━━ SPECIFIC SHIPS — ALTERNATIVES SEMANTICS ━━━
specific_ships is an ORDERED ALTERNATIVES list. The composer picks the first available.
  "大和改二(重)" → specific_ships: ["大和改二重","大和改二"]   (count=1, pick whichever is available)
  "長門改二or陸奥改二旗舰" → specific_ships: ["長門改二","陸奥改二"]
Do NOT put ships from different fleet slots in the same specific_ships list.

━━━ SPECIAL ATTACKS — FULL REFERENCE ━━━
Shorthand you will see in NGA posts:
  武大流/一斉射   = yamato_2ship or yamato_3ship
  胸熱/长门touch  = nagato_touch
  NT/纳尔逊      = nelson_touch
  科罗拉多        = colorado_special
  金刚夜战        = kongou_night_raid
  黎塞留          = richelieu_special

Trigger conditions:
  yamato_2ship:  flagship=大和改二(重) [either], slot2=武蔵改二/Iowa改/Richelieu改/Jean Bart改/Bismarck drei
  yamato_3ship:  flagship=大和改二(重), slot2+slot3 = valid pair (e.g. 武蔵改二+長門改二, 長門改二+陸奥改二,
                 伊勢改二+日向改二, 山城改二+扶桑改二, ネルソン改+ロドニー改, Roma+Italia, etc.)
                 Note: 大和改二重 = slow heavy mode, 大和改二 = fast mode; different tactics, separate presets
  nagato_touch:  flagship=長門改二 OR 陸奥改二 (slot1), any BB/BBV in slot2
                 Equipment bonus: 電探(索敵≥5)×1.15, 徹甲弾×1.35
  nelson_touch:  flagship=ネルソン OR ロドニー (slot1), non-carrier in slots 3 AND 5, fleet=6 ships
  colorado_special: flagship=コロラド OR メリーランド, BB/BBV in slots 2 and 3
  kongou_night_raid: flagship=金剛改二丙/比叡改二丙/榛名改二乙/丙/霧島改二丙, specific partner in slot2;
                     NIGHT BATTLE ONLY
  richelieu_special: flagship=Richelieu改/Deux ↔ Jean Bart改
  warspite_valiant:  flagship=ウォースパイト改 ↔ Valiant改

━━━ NIGHT BATTLE CUT-INS (夜戦カットイン / CI) ━━━
These are ship equipment configurations, not separate presets. Put in equip_notes/example/special_attacks.
  魚雷CI (torpedo CI):        3× torpedo or 2× torpedo + 見張員/水雷長
  主魚CI (main+torp CI):      main gun + torpedo (+ 見張員)
  D型砲魚雷CI:                D型砲 + torpedo + 見張員 (for specific DDs e.g. 時雨改三, Fletcher)
  主主CI (双主砲CI):          2× main gun + 徹甲弾/電探
  先制雷撃CI (pre-torp):      submarine torpedo CI, fires before day battle
  魚電CI:                     torpedo + 水上電探 (for CLT)
When NGA says "魚雷CI旗舰", put that ship as specific_ships in its slot with notes="魚雷CI旗舰".

━━━ OTHER KEY MECHANICS ━━━
先制対潜 (pre-emptive ASW): ship's ASW stat ≥ 100 (base+equip). Required equip: sonar+depth charge.
  → set min_asw on that slot. Common ships: 時雨改三, Fletcher Mk.II, 矢矧改二乙, 秋月型改, etc.
発煙装置 (smoke generator): AO or DD equipped with 発煙装置; used for evasion buff on boss node.
  → mention in equip_notes, note which ship (often 宗谷) carries it.
夜間触接 (night contact): CV/CVL with 夜間偵察機 (e.g. 夜間瑞雲改) enables night-battle contact.
彩雲 (Saiun):              equip on CV to avoid T-disadvantage (T字不利). Note in equip_notes.
夜母 (night carrier):      CV/CVL equipped with 夜間作戦航空要員 enables night attacks.
照明弾 (starshell):        increases night-battle accuracy and enables night contact.
対空CI (AA CI):            ship with AA guns + director fires pre-emptively against enemy aircraft.
   Common AA CI ships: 秋月改, 摩耶改二, 五十鈴改二, 武蔵改二 (空母鬼怒改二) etc.

━━━ FORMATION SHORTHAND ━━━
单纵/単縦=単縦陣, 复纵/複縦=複縦陣, 梯形=梯形陣, 单横/単横=単横陣, 輪形=輪形陣
警戒第一/二/三/四=第一/二/三/四警戒航行序列 (combined fleet)

━━━ EXTRACTION RULES ━━━
- Extract main fleet VARIANTS (流派) only — 3~8 presets typical. Skip per-quest tables.
- Use JAPANESE ship and equipment names exactly. Apply CJK normalization: 藏→蔵, 鹤→鶴, 龙→龍.
- routing_nodes: JSON array of node letters ["F","G","K"], never a string.
- equip_notes: include all numeric thresholds (制空値, 索敵, 対潜値, etc.).
- example: IMAGES TAKE PRIORITY. Scan ALL fleet-composition screenshots provided as images.
  Each screenshot shows ship names, levels, and equipment slots with exact Japanese names.
  Match each screenshot to a preset by looking at which ships are shown (flagship, composition).
  Copy equipment names VERBATIM from the image — do NOT substitute with generic names from the text.
  If a screenshot shows 試製51cm三連装砲 for 大和改二, write "試製51cm三連装砲", not "46cm三連装砲改".
  Only fall back to text descriptions if no screenshot matches that preset.
- special_attacks: fill for every preset that uses a special attack mechanic.
- Output ONLY valid JSON, no markdown, no explanation.
"""

def _quest_system_prompt(category_filter: str) -> str:
    return f"""\
You are a Kantai Collection (KanColle) quest planner.
Extract ONLY the {category_filter} quest recommendations from the NGA table.

The table has 5 columns:
1. Quest name + ID (e.g. [Bw6])
2. Requirement
3. Recommended map(s)
4. Fleet config for doing THIS quest SOLO on that map ("单独做的配置")
5. Fleet config + other quest IDs when doing MULTIPLE quests TOGETHER ("和其它任务一起做")

Output JSON (omit null/empty fields):
{{
  "quests": [
    {{
      "quest_id": "Bw6",
      "quest_name": "敵東方艦隊を撃滅せよ！",
      "category": "weekly",
      "requirement": "4-1~4-5 12次B胜",
      "maps": [
        {{
          "map_id": "4-4",
          "fleet_hint": "3CVL+1CL+2DE/DD",
          "notes": "boss点144空确",
          "synergy_quests": ["Bw4", "Bw8"],
          "combo_fleets": {{
            "Bw4": "2CV+1CA系/CL系+1CL+2DD"
          }}
        }}
      ]
    }}
  ]
}}

Rules:
- Extract {category_filter} quests ONLY
- fleet_hint: COMPACT notation like "1CL+5DD" for SOLO config, max 35 chars
- combo_fleets: dict of quest_id → compact fleet hint to use when doing BOTH quests simultaneously on this map. Extract from column 5. OMIT if column 5 only lists quest IDs without a different fleet.
- synergy_quests: ALL quest IDs mentioned in column 5 for this map (whether or not they have a separate fleet config)
- notes: air power numbers, LoS threshold, special ship requirements — keep very short
- maps: list each recommended map as a separate entry; omit "随意" (any map) entries with no fleet info
- requirement: the completion condition (include specific ship requirements if any)
- Category values: "daily", "weekly", "monthly", "quarterly", "yearly"
- Quest ID prefix: Bd=daily, Bw=weekly, Bm=monthly, Bq=quarterly, By=yearly
- Output ONLY valid JSON, no markdown, no explanation
"""


def load_equip_db() -> dict[str, str]:
    """Load equipment name database for fuzzy correction."""
    if EQUIP_DB_PATH.exists():
        with open(EQUIP_DB_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_equip_aliases() -> dict[str, str]:
    """Load explicit variant→canonical aliases (data/equip_aliases.json)."""
    if EQUIP_ALIASES_PATH.exists():
        with open(EQUIP_ALIASES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    return {}


# Generic category terms that Claude uses as placeholders — skip fuzzy matching
_EQUIP_GENERIC = {
    # Japanese generic terms
    "主砲", "副砲", "魚雷", "電探", "水上偵察機", "水上爆撃機",
    "艦攻", "艦爆", "艦戦", "夜間攻撃機", "夜間偵察機", "対潜装備",
    "対空機銃", "対空装備", "缶", "タービン", "バルジ", "ダメコン",
    "損傷管制", "夜戦カットイン装備", "Sonar", "対潜迫撃砲",
    "上陸用舟艇(特大発)", "改良型缶",
    # Simplified Chinese equivalents (Claude sometimes mixes languages)
    "主炮", "副炮", "鱼雷", "电探", "水上侦察机", "水上爆击机",
    "舰攻", "舰爆", "舰战", "对潜装备", "对空机枪",
    # OCR garbage: upgrade markers, multi-item descriptions, garbled ship+equip merges
    "+0", "+1", "+2", "+3", "+4", "+5",
    "嵐電改", "壹電改", "瓮電改",
    "後期型電探&逆探+通気管装備",
}


def _normalize_brackets(name: str) -> str:
    """Normalize full-width brackets to half-width for matching."""
    return name.replace("（", "(").replace("）", ")")


def correct_equip_names(strategy: MapStrategy, equip_db: dict[str, str]) -> MapStrategy:
    """Fuzzy-match equipment names against known DB and correct typos."""
    if not equip_db:
        return strategy
    aliases = load_equip_aliases()
    known = list(equip_db.values())
    known_set = set(known)
    for preset in strategy.presets:
        if not preset.example:
            continue
        for ship_example in preset.example:
            corrected = []
            for name in ship_example.equips:
                if not name:
                    continue   # skip empty strings from Claude
                if name in _EQUIP_GENERIC:
                    corrected.append(name)   # intentional placeholder, keep as-is
                    continue
                # Explicit alias map first
                if name in aliases:
                    resolved = aliases[name]
                    if resolved != name:
                        log.debug("Equip alias: %r → %r", name, resolved)
                    corrected.append(resolved)
                    continue
                # Try bracket normalization before fuzzy
                normalized = _normalize_brackets(name)
                if normalized in known_set:
                    if normalized != name:
                        log.debug("Equip bracket-normalized: %r → %r", name, normalized)
                    corrected.append(normalized)
                    continue
                if name in known_set:
                    corrected.append(name)
                    continue
                match, score, _ = process.extractOne(normalized, known, scorer=fuzz.WRatio)
                if score >= 80:
                    if match != name:
                        log.debug("Equip name corrected: %r → %r (score %d)", name, match, score)
                    corrected.append(match)
                else:
                    log.warning("Unknown equip name (score %d): %r", score, name)
                    corrected.append(name)
            ship_example.equips = corrected
    return strategy


def encode_image(path: str) -> tuple[str, str]:
    """Return (base64_data, media_type) for an image file."""
    ext = Path(path).suffix.lower()
    media_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(ext, "image/png")

    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode()
    return data, media_type


# NGA strategy index post IDs (from bbs.nga.cn/read.php?tid=23451223)
NGA_MAP_PIDS: dict[str, str] = {
    "1-1": "454451942", "1-2": "454452006", "1-3": "454452053",
    "1-4": "454452119", "1-5": "454452178", "1-6": "454452236",
    "2-1": "454452562", "2-2": "454452628", "2-3": "454452698",
    "2-4": "454452788", "2-5": "454452843",
    "3-1": "454453286", "3-2": "454453344", "3-3": "454453396",
    "3-4": "454453451", "3-5": "454453495",
    "4-1": "454453876", "4-2": "454454019", "4-3": "454454084",
    "4-4": "454454163", "4-5": "454454205",
    "5-1": "454454625", "5-2": "454454730", "5-3": "454454783",
    "5-4": "454454829", "5-5": "454454888",
    "6-1": "454455307", "6-2": "454455364", "6-3": "454455426",
    "6-4": "454455513", "6-5": "454455575",
    "7-1": "454455961", "7-2": "454456055", "7-3": "454456110",
    "7-4": "454456265", "7-5": "454456331",
}

NGA_SESSION_PATH = Path(__file__).parent.parent / "data" / "nga_session.json"

NGA_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _playwright_import():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        print("playwright not installed. Run:")
        print("  pip install playwright && python -m playwright install chromium")
        sys.exit(1)


def nga_login_and_save() -> None:
    """
    Open a headed browser, let the user log in to NGA, then save the session.
    Run once; after this --url works headlessly.
    """
    sync_playwright = _playwright_import()
    print("Opening browser → log in to NGA, then press Enter in this terminal.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(user_agent=NGA_UA, locale="zh-CN")
        page = ctx.new_page()
        page.goto("https://bbs.nga.cn/", wait_until="domcontentloaded", timeout=30000)

        import time
        signal_file = Path("/tmp/kckit_nga_ready")
        signal_file.unlink(missing_ok=True)
        print("\n" + "="*55, flush=True)
        print("Log in to NGA in the browser window.", flush=True)
        print("When done, run this command in any terminal:", flush=True)
        print(f"  touch {signal_file}", flush=True)
        print("="*55, flush=True)
        while not signal_file.exists():
            time.sleep(1)
        signal_file.unlink(missing_ok=True)

        NGA_SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        ctx.storage_state(path=str(NGA_SESSION_PATH))

        # Quick check: warn if still guest
        cookies = ctx.cookies()
        uid_cookie = next((c for c in cookies if c["name"] == "ngaPassportUid"), None)
        if uid_cookie and uid_cookie["value"].startswith("guest"):
            print("WARNING: Session looks like a guest. If --url fails, re-run --nga-login after logging in.")
        browser.close()

    print(f"✓ Session saved to {NGA_SESSION_PATH}")
    print("You can now use --url without logging in again.")


def _extract_text_with_image_markers(soup) -> tuple[str, list[str]]:
    """
    Walk the HTML tree and return (text_with_markers, image_urls) where the text
    contains [IMAGE_N] tokens at the exact positions images appear.
    This preserves context so Claude can see what text surrounds each fleet screenshot.
    """
    from bs4 import Tag, NavigableString
    import re as _re

    image_urls: list[str] = []
    parts: list[str] = []

    _SKIP = {"script", "style", "head", "nav", "header", "footer", "meta", "link"}
    _BLOCK = {"p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5",
              "blockquote", "pre", "table", "thead", "tbody"}

    def walk(node):
        if isinstance(node, NavigableString):
            t = str(node)
            if t.strip():
                parts.append(t)
        elif isinstance(node, Tag):
            if node.name in _SKIP:
                return
            if node.name == "br":
                parts.append("\n")
                return
            if node.name == "img":
                src = (node.get("src") or node.get("data-src") or
                       node.get("data-original") or "").strip()
                if src and not src.startswith("data:"):
                    if src.startswith("//"):
                        src = "https:" + src
                    elif not src.startswith("http"):
                        src = "https://bbs.nga.cn" + src
                    idx = len(image_urls)
                    image_urls.append(src)
                    parts.append(f"\n[IMAGE_{idx}]\n")
                return
            if node.name in _BLOCK:
                parts.append("\n")
            for child in node.children:
                walk(child)
            if node.name in _BLOCK:
                parts.append("\n")

    walk(soup)

    text = "".join(parts)
    text = _re.sub(r" +", " ", text)
    text = _re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), image_urls


def fetch_url_with_browser(url: str, expand_collapses: bool = True,
                           _no_session: bool = False,
                           fetch_images: bool = True) -> tuple[str, list[str]]:
    """
    Fetch an NGA page using headless Playwright.
    Returns (text_content, inline_image_urls).
    Handles JS redirect + ad interstitial, then expands all AJAX-loaded
    collapse sections (div.collapse_btn) to get complete fleet/equipment details.
    """
    sync_playwright = _playwright_import()
    from bs4 import BeautifulSoup

    kwargs: dict = {}
    if not _no_session and NGA_SESSION_PATH.exists():
        kwargs["storage_state"] = str(NGA_SESSION_PATH)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=NGA_UA, locale="zh-CN", **kwargs)
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # Click through NGA redirect page + ad interstitial
        for _ in range(4):
            body = page.inner_text("body")
            if len(body) > 500:
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

        body = page.inner_text("body")
        if len(body) < 300 and "登录" in body:
            if kwargs:
                # Stale session → retry without it
                browser.close()
                log.debug("Stale session detected, retrying without session…")
                return fetch_url_with_browser(url, expand_collapses=expand_collapses,
                                              _no_session=True, fetch_images=fetch_images)
            browser.close()
            print("ERROR: This thread requires NGA login.")
            print("Run: python tools/import_nga.py --nga-login")
            sys.exit(1)

        # Scroll to bottom to trigger lazy-loaded images
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(800)
            page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass

        # Expand all NGA collapse sections (AJAX-loaded fleet/equipment details)
        if expand_collapses:
            btns = page.locator("div.collapse_btn button").all()
            if btns:
                log.debug("Expanding %d collapse sections…", len(btns))
                for btn in btns:
                    try:
                        btn.click(timeout=1500)
                    except Exception:
                        pass
                try:
                    page.wait_for_load_state("networkidle", timeout=30000)
                except Exception:
                    pass
            # Scroll again after expanding
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(500)
            except Exception:
                pass

        # Extract from main post content div (#postcontent0) when available
        post_html = None
        try:
            post_el = page.locator("#postcontent0")
            if post_el.count():
                post_html = post_el.inner_html()
        except Exception:
            pass
        if post_html is None:
            post_html = page.content()

        browser.close()

    soup = BeautifulSoup(post_html, "html.parser")

    if fetch_images:
        text, image_urls = _extract_text_with_image_markers(soup)
    else:
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        image_urls = []

    log.debug("Fetched %d chars, %d image refs from %s", len(text), len(image_urls), url)
    return text, image_urls


def _fleet_screenshot_score(raw_bytes: bytes, media_type: str) -> float:
    """
    Score an image for "fleet screenshot" usefulness (higher = more useful).
    Returns 0.0 to discard the image entirely.

    Fleet screenshots (noro6 / in-game):  ~1100×640px, >80KB, ratio ~1.7
    Route maps:                            ~1080×720px, >100KB, ratio ~1.5
    Useless: formation UI screenshots (400×240), icons (<80KB), avatars.
    """
    try:
        import io
        from PIL import Image as _Image
        img = _Image.open(io.BytesIO(raw_bytes))
        w, h = img.size
    except Exception:
        # Can't read dimensions — keep if large enough
        return float(len(raw_bytes)) if len(raw_bytes) > 80_000 else 0.0

    size_kb = len(raw_bytes) / 1024
    ratio = w / h if h > 0 else 0

    # Hard reject: too small in either dimension or too little data
    if w <= 500 or h <= 200 or size_kb < 80:
        return 0.0

    # Score: reward large width and fleet-screenshot aspect ratio (~1.6-1.9)
    width_bonus = min(w / 1000, 2.0)
    ratio_bonus = 1.5 if 1.5 <= ratio <= 2.0 else 1.0
    return size_kb * width_bonus * ratio_bonus


def download_nga_images(
    image_urls: list[str],
    max_images: int = 8,
    cache_dir: Optional[Path] = None,
    force: bool = False,
) -> list[tuple[str, str]]:
    """Download NGA post images, return list of (base64_data, media_type) tuples.

    Images are scored and ranked — fleet screenshots score highest, icons/
    formation-UI screenshots are discarded. Only the top max_images are returned.
    If cache_dir is given, images are cached so re-runs skip network fetches.
    """
    import urllib.request
    import io

    _VALID_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    _EXT = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}

    # Load from cache if available (skip if force=True)
    if cache_dir and cache_dir.exists() and not force:
        cached_files = sorted(
            f for f in cache_dir.iterdir()
            if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp")
        )
        if cached_files:
            scored = []
            for f in cached_files:
                raw = f.read_bytes()
                ext = f.suffix.lower().lstrip(".")
                ct = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                      "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
                score = _fleet_screenshot_score(raw, ct)
                if score > 0:
                    scored.append((score, raw, ct))
            scored.sort(key=lambda x: x[0], reverse=True)
            results = [(base64.standard_b64encode(r).decode(), ct)
                       for _, r, ct in scored[:max_images]]
            if results:
                log.info("Loaded %d fleet-screenshot images from cache: %s", len(results), cache_dir)
                return results

    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)

    # Download all URLs, score each, then return top max_images
    scored: list[tuple[float, bytes, str]] = []
    tried = 0
    for url in image_urls:
        tried += 1
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": NGA_UA, "Referer": "https://bbs.nga.cn/"},
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                raw = resp.read()
                ct = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
                if ct not in _VALID_TYPES:
                    ct = "image/jpeg"
                score = _fleet_screenshot_score(raw, ct)
                log.debug("Image %dKB score=%.0f: %s", len(raw)//1024, score, url)
                if score > 0:
                    scored.append((score, raw, ct))
        except Exception as e:
            log.debug("Image download failed %s: %s", url, e)

    # Sort by score descending, take top max_images
    scored.sort(key=lambda x: x[0], reverse=True)
    results = []
    for i, (score, raw, ct) in enumerate(scored[:max_images]):
        if cache_dir:
            ext = _EXT.get(ct, "jpg")
            cache_file = cache_dir / f"img_{i:02d}_score{int(score)}.{ext}"
            cache_file.write_bytes(raw)
        data = base64.standard_b64encode(raw).decode()
        results.append((data, ct))

    log.info("Downloaded %d fleet-screenshot images (tried %d URLs, %d passed filter)%s",
             len(results), tried, len(scored),
             f" → cached in {cache_dir}" if cache_dir else "")
    return results


def fetch_and_save_raw(map_id: str, force: bool = False, url: Optional[str] = None) -> Path:
    """
    Fetch NGA page for map_id and save raw data to data/nga_raw/world{N}/{map_id}/:
      post.txt  — full post text with [IMAGE_N] markers at image positions
      images/   — fleet screenshots saved as img_{N:03d}.{ext}
      meta.json — URL, fetch time, image stats

    Skips network fetch if post.txt already exists and force=False.
    Returns the raw_dir path.
    """
    import urllib.request
    from datetime import datetime

    rdir = raw_dir(map_id)
    post_path = rdir / "post.txt"
    img_dir = rdir / "images"
    meta_path = rdir / "meta.json"

    if url is None:
        pid = NGA_MAP_PIDS.get(map_id)
        if not pid:
            raise ValueError(f"No PID in NGA_MAP_PIDS for map {map_id}")
        url = f"https://bbs.nga.cn/read.php?pid={pid}"

    if not force and post_path.exists():
        log.info("Raw data already present for %s — skipping fetch", map_id)
        return rdir
    log.info("Fetching raw data for %s from %s", map_id, url)

    text_with_markers, image_urls = fetch_url_with_browser(url, fetch_images=True)
    if len(text_with_markers) < 100:
        raise RuntimeError(f"Only {len(text_with_markers)} chars fetched for {map_id} — likely blocked")

    rdir.mkdir(parents=True, exist_ok=True)
    img_dir.mkdir(exist_ok=True)

    _VALID = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    _EXT = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}

    saved: dict[int, str] = {}   # page_img_idx → filename
    scores: dict[int, float] = {}

    for idx, img_url in enumerate(image_urls):
        try:
            req = urllib.request.Request(
                img_url,
                headers={"User-Agent": NGA_UA, "Referer": "https://bbs.nga.cn/"},
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                raw = resp.read()
                ct = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
                if ct not in _VALID:
                    ct = "image/jpeg"
                score = _fleet_screenshot_score(raw, ct)
                if score > 0:
                    ext = _EXT.get(ct, "jpg")
                    fname = f"img_{idx:03d}.{ext}"
                    (img_dir / fname).write_bytes(raw)
                    saved[idx] = fname
                    scores[idx] = score
        except Exception as e:
            log.debug("Skip image %d: %s", idx, e)

    post_path.write_text(text_with_markers, encoding="utf-8")
    meta_path.write_text(
        json.dumps({
            "map_id": map_id,
            "url": url,
            "fetched_at": datetime.now().isoformat(),
            "total_image_urls": len(image_urls),
            "fleet_screenshots_saved": len(saved),
            "image_scores": {str(k): v for k, v in scores.items()},
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log.info("Saved raw for %s: %d chars, %d/%d fleet screenshots → %s",
             map_id, len(text_with_markers), len(saved), len(image_urls), rdir)
    return rdir


def load_raw(map_id: str, max_images: int = 12) -> tuple[str, dict[int, tuple[str, str]]]:
    """
    Load saved raw data for map_id.
    Returns (text_with_markers, {page_img_idx: (base64_data, media_type)}).
    Images are limited to the top max_images by score (from meta.json).
    """
    rdir = raw_dir(map_id)
    post_path = rdir / "post.txt"
    img_dir = rdir / "images"
    meta_path = rdir / "meta.json"

    if not post_path.exists():
        raise FileNotFoundError(f"No raw data for {map_id}. Run --fetch {map_id} first.")

    text = post_path.read_text(encoding="utf-8")

    # Determine which image indices to load (top N by score)
    scores: dict[int, float] = {}
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        scores = {int(k): v for k, v in meta.get("image_scores", {}).items()}

    top_indices = sorted(scores, key=lambda k: scores[k], reverse=True)[:max_images]
    top_set = set(top_indices)

    _CT = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
           "gif": "image/gif", "webp": "image/webp"}

    images: dict[int, tuple[str, str]] = {}
    if img_dir.exists():
        for fpath in img_dir.iterdir():
            if fpath.suffix.lower().lstrip(".") not in _CT:
                continue
            # Parse index from img_NNN.ext
            stem = fpath.stem
            if stem.startswith("img_") and stem[4:].isdigit():
                idx = int(stem[4:])
                if not top_set or idx in top_set:
                    raw = fpath.read_bytes()
                    ct = _CT.get(fpath.suffix.lower().lstrip("."), "image/jpeg")
                    images[idx] = (base64.standard_b64encode(raw).decode(), ct)

    # Strip [IMAGE_N] markers for images that weren't loaded — keeps the text clean
    # so call_claude_interleaved never encounters a marker without a matching image.
    import re as _re
    all_indices_in_text = {int(m) for m in _re.findall(r"\[IMAGE_(\d+)\]", text)}
    for idx in all_indices_in_text - set(images):
        text = text.replace(f"[IMAGE_{idx}]", "")
    # Collapse any multiple blank lines created by stripping
    text = _re.sub(r"\n{3,}", "\n\n", text).strip()

    log.info("Loaded raw for %s: %d chars text, %d images", map_id, len(text), len(images))
    return text, images


def read_text_input(args: argparse.Namespace) -> tuple[str, list[str], list[str]]:
    """Returns (text_content, local_image_paths, nga_image_urls)."""
    text = ""
    images: list[str] = []
    nga_image_urls: list[str] = []

    if getattr(args, "url", None):
        fetched_text, fetched_urls = fetch_url_with_browser(
            args.url, fetch_images=not getattr(args, "no_images", False)
        )
        text += fetched_text
        nga_image_urls.extend(fetched_urls)

    if args.text:
        with open(args.text, encoding="utf-8") as f:
            text += f.read()

    if args.html:
        from bs4 import BeautifulSoup
        with open(args.html, encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        text += soup.get_text(separator="\n", strip=True)

    if args.clip:
        try:
            import subprocess
            result = subprocess.run(["pbpaste"], capture_output=True, text=True)
            text += result.stdout
        except Exception as e:
            log.warning("Clipboard read failed: %s", e)

    if args.img:
        for pattern in args.img:
            for path in sorted(glob.glob(pattern)):
                if os.path.isfile(path):
                    images.append(path)

    return text, images, nga_image_urls


def call_claude(
    text: str,
    images: list[str],
    map_id: str,
    model: str = "claude-sonnet-4-6",
    image_data: Optional[list[tuple[str, str]]] = None,
) -> dict:
    """Call Claude API with text and images, return parsed JSON.

    images: local file paths (from --img flag)
    image_data: pre-downloaded (base64_data, media_type) pairs from NGA post
    """
    client = anthropic.Anthropic()

    content = []

    if text.strip():
        content.append({
            "type": "text",
            "text": f"Map: {map_id}\n\nNGA Strategy Content:\n\n{text[:100000]}",
        })

    # Local file images (--img flag)
    for img_path in images[:5]:
        data, media_type = encode_image(img_path)
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        })
        content.append({
            "type": "text",
            "text": f"(Screenshot from NGA strategy page for map {map_id})",
        })

    # Pre-downloaded NGA inline images
    if image_data:
        for i, (data, media_type) in enumerate(image_data):
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": data},
            })
            content.append({
                "type": "text",
                "text": (
                    f"[Image {i+1}/{len(image_data)}] "
                    "If this is a fleet-composition screenshot: identify the preset by its ships, "
                    "then copy ALL equipment names VERBATIM from the image slots into that preset's example field."
                ),
            })

    if not content:
        raise ValueError("No input content provided (no text, no images)")

    total_images = len(images) + len(image_data or [])
    log.info("Calling Claude API — %d chars text, %d images…", len(text), total_images)
    response = client.messages.create(
        model=model,
        max_tokens=16384,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("JSON parse error at char %d: %s", e.pos, e.msg)
        log.debug("Raw response (first 2000 chars):\n%s", raw[:2000])
        log.debug("Raw response (around error):\n%s", raw[max(0, e.pos-200):e.pos+200])
        raise


def call_claude_interleaved(
    text_with_markers: str,
    images_by_idx: dict[int, tuple[str, str]],
    map_id: str,
    model: str = "claude-sonnet-4-6",
) -> dict:
    """
    Call Claude with text and fleet screenshots interleaved at their original positions.
    text_with_markers contains [IMAGE_N] tokens; images_by_idx maps page index → (b64, media_type).
    This lets Claude see each image in the context of the surrounding strategy text.
    """
    import re as _re
    client = anthropic.Anthropic()

    content: list[dict] = []
    # Split text on [IMAGE_N] markers
    parts = _re.split(r"\[IMAGE_(\d+)\]", text_with_markers)
    # parts: [text, idx, text, idx, ..., text]

    for i, chunk in enumerate(parts):
        if i % 2 == 0:
            # Text chunk
            header = f"Map: {map_id}\n\nNGA Strategy Content:\n\n" if i == 0 else ""
            full = header + chunk
            if full.strip():
                content.append({"type": "text", "text": full[:30000]})
        else:
            # Image index — load_raw guarantees all markers have a matching image
            idx = int(chunk)
            if idx in images_by_idx:
                data, media_type = images_by_idx[idx]
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": data},
                })

    if not content:
        raise ValueError("No content to send to Claude")

    total_images = sum(1 for i, c in enumerate(parts) if i % 2 == 1 and int(c) in images_by_idx)
    log.info("Calling Claude API — %d chars text, %d images (interleaved)…",
             len(text_with_markers), total_images)

    response = client.messages.create(
        model=model,
        max_tokens=16384,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("JSON parse error at char %d: %s", e.pos, e.msg)
        raise


_AIR_STATE_MAP = {
    "inferiority": "none",
    "denial": "none",
    "contest": "parity",
    "supremecy": "supremacy",   # common typo
}

# Chinese → Japanese kanji substitutions that appear in ship/equip names
_CJK_NORM = str.maketrans({
    "藏": "蔵",  # 藏 → 蔵 (武蔵)
    "鹤": "鶴",  # 鹤 → 鶴 (瑞鶴/翔鶴)
    "龙": "龍",  # 龙 → 龍 (龍鳳)
    "凤": "鳳",  # 凤 → 鳳 (鳳翔)
    "云": "雲",  # 云 → 雲 (那智/雲龍 etc.)
    "飞": "飛",  # 飞 → 飛 (飛龍)
    "运": "運",  # 运 → 運 (山雲 etc.)
    "灵": "靈",  # 灵 → 靈 — rare
})


def _normalize_ship_name(name: str) -> str:
    return name.translate(_CJK_NORM)


# Expand parenthetical ship_class notation: BB(V) → [BB, BBV], CV(L) → [CV, CVL]
_CLASS_EXPAND: dict[str, list[str]] = {
    "BB(V)": ["BB", "BBV"],
    "CV(L)": ["CV", "CVL"],
    "BBV(BB)": ["BBV", "BB"],
    "CVL(CV)": ["CVL", "CV"],
    "CA(V)": ["CA", "CAV"],
    "CVB": ["CV"],        # 装甲空母 → CV
    "FBB(BB)": ["FBB", "BB"],
    "DD/DE": ["DD", "DE"],
}


def _expand_ship_classes(classes: list[str]) -> list[str]:
    result = []
    for c in classes:
        result.extend(_CLASS_EXPAND.get(c, [c]))
    return list(dict.fromkeys(result))  # deduplicate preserving order


def _normalize_presets(raw_data: dict) -> None:
    """Normalize preset data in-place before schema validation."""
    for preset in raw_data.get("presets", []):
        # Clip fleet slot totals to 6
        slots = preset.get("fleet", {}).get("slots", [])
        total = sum(s.get("count", 1) for s in slots)
        if total > 6:
            excess = total - 6
            for slot in reversed(slots):
                if excess <= 0:
                    break
                reduce = min(slot.get("count", 1), excess)
                slot["count"] = slot.get("count", 1) - reduce
                excess -= reduce
            preset["fleet"]["slots"] = [s for s in slots if s.get("count", 1) > 0]

        # Normalize air_state values
        req = preset.get("requirements", {})
        air = req.get("air_state")
        if air and air not in ("supremacy", "superiority", "parity", "none"):
            req["air_state"] = _AIR_STATE_MAP.get(air, "none")

        # Normalize routing_nodes from string to list
        rn = req.get("routing_nodes")
        if isinstance(rn, str):
            req["routing_nodes"] = [n for n in rn.replace("1-", "").split("-") if n and not n.isdigit()]

        # Expand BB(V)/CV(L) notation and normalize ship_class lists
        for slot in preset.get("fleet", {}).get("slots", []):
            if slot.get("ship_class"):
                slot["ship_class"] = _expand_ship_classes(slot["ship_class"])

        # Normalize ship names in specific_ships (Chinese→Japanese kanji variants)
        for slot in preset.get("fleet", {}).get("slots", []):
            if slot.get("specific_ships"):
                slot["specific_ships"] = [_normalize_ship_name(n) for n in slot["specific_ships"]]

        # Normalize ship names in example blocks
        for ex in preset.get("example", []):
            if isinstance(ex, dict) and ex.get("ship"):
                ex["ship"] = _normalize_ship_name(ex["ship"])

        # Normalize ship names in special_attacks
        for sa_entry in preset.get("special_attacks", []):
            if isinstance(sa_entry, dict):
                sa_entry["ships"] = [_normalize_ship_name(n) for n in sa_entry.get("ships", [])]
                if sa_entry.get("flagship"):
                    sa_entry["flagship"] = _normalize_ship_name(sa_entry["flagship"])

        # Normalize special_attacks: ensure it's a list, drop malformed entries
        sa = preset.get("special_attacks")
        if sa is not None:
            if not isinstance(sa, list):
                preset["special_attacks"] = []
            else:
                # Keep only dict entries that have at least a "type" key
                preset["special_attacks"] = [
                    item for item in sa
                    if isinstance(item, dict) and item.get("type")
                ]




def import_strategy(args: argparse.Namespace) -> None:
    map_id = args.map
    if args.output:
        output_path = Path(args.output) / f"{map_id.replace('-', '_')}.yaml"
    else:
        output_path = map_yaml_path(map_id)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if getattr(args, "url", None):
        # NGA URL path — same pipeline as batch: fetch raw → load → call interleaved
        try:
            fetch_and_save_raw(map_id, force=getattr(args, "force_images", False), url=args.url)
        except Exception as e:
            print(f"ERROR fetching {map_id}: {e}", file=sys.stderr)
            sys.exit(1)
        text, images_by_idx = load_raw(map_id)
        try:
            raw_data = call_claude_interleaved(text, images_by_idx, map_id, model=args.model)
        except Exception as e:
            print(f"ERROR: Claude API failed: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        # Manual input: --text / --html / --clip / --img (no image position context)
        text, local_images, _ = read_text_input(args)
        if not text and not local_images:
            print("ERROR: Provide --url, --text, --html, --img, or --clip input.", file=sys.stderr)
            sys.exit(1)
        try:
            raw_data = call_claude(text, local_images, map_id, model=args.model)
        except json.JSONDecodeError as e:
            print(f"ERROR: Claude returned invalid JSON: {e}", file=sys.stderr)
            sys.exit(1)
        except anthropic.APIError as e:
            print(f"ERROR: Anthropic API error: {e}", file=sys.stderr)
            sys.exit(1)

    # Override map field
    raw_data["map"] = map_id
    raw_data.setdefault("source", "nga_import")

    _normalize_presets(raw_data)

    # Validate with schema
    try:
        new_strategy = MapStrategy.model_validate(raw_data)
    except Exception as e:
        print(f"ERROR: Schema validation failed: {e}", file=sys.stderr)
        if args.debug:
            print("Raw data:", json.dumps(raw_data, ensure_ascii=False, indent=2))
        sys.exit(1)

    # Fuzzy-correct equipment names
    equip_db = load_equip_db()
    new_strategy = correct_equip_names(new_strategy, equip_db)

    # Merge with existing file
    merged = merge_strategy(str(output_path), new_strategy)
    save_strategy(merged, str(output_path))

    print(f"✓ Saved {len(new_strategy.presets)} preset(s) to {output_path}")
    for p in new_strategy.presets:
        print(f"  · {p.name}")


def build_equip_db_from_poi(poi_const_path: Optional[str] = None) -> None:
    """
    Build equipment name DB for fuzzy matching.
    Tries sources in order:
      1. wctf-db items.nedb (always present after poi install)
      2. poi cached api_start2 JSON
      3. poi WebSocket bridge (live state)
    """
    import glob as _glob
    db: dict[str, str] = {}

    # Source 1: wctf-db items.nedb
    wctf_nedb = os.path.expanduser(
        "~/Library/Application Support/poi/wctf-db/node_modules/"
        "whocallsthefleet-database/db/items.nedb"
    )
    if os.path.exists(wctf_nedb) and not db:
        with open(wctf_nedb, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    name_ja = (item.get("name") or {}).get("ja_jp", "")
                    if item.get("id") and name_ja:
                        db[str(item["id"])] = name_ja
                except json.JSONDecodeError:
                    continue
        if db:
            print(f"Loaded {len(db)} items from wctf-db nedb")

    # Source 2: poi cached api_start2 JSON (akashic-records or similar)
    if not db and poi_const_path is None:
        candidates = _glob.glob(
            os.path.expanduser(
                "~/Library/Application Support/poi/db/*/kcsapi/api_start2/*.json"
            )
        )
        poi_const_path = candidates[0] if candidates else None

    if not db and poi_const_path and os.path.exists(poi_const_path):
        with open(poi_const_path, encoding="utf-8") as f:
            data = json.load(f)
        for item in data.get("api_data", {}).get("api_mst_slotitem", []):
            db[str(item["api_id"])] = item["api_name"]
        if db:
            print(f"Loaded {len(db)} items from api_start2 cache")

    # Source 3: poi WebSocket bridge
    if not db:
        print("Trying poi WebSocket bridge…")
        try:
            import asyncio, websockets as _ws

            async def _fetch():
                async with _ws.connect("ws://127.0.0.1:23456", max_size=8*1024*1024, open_timeout=8) as ws:
                    raw = await asyncio.wait_for(ws.recv(), timeout=8)
                    return json.loads(raw)

            state_msg = asyncio.run(_fetch())
            equips = state_msg.get("payload", {}).get("equips", {})
            for eq in equips.values():
                master = eq.get("$master", {})
                eq_id = eq.get("api_slotitem_id")
                name = master.get("api_name", "")
                if eq_id and name:
                    db[str(eq_id)] = name
            if db:
                print(f"Loaded {len(db)} items from poi bridge")
        except Exception as e:
            print(f"Bridge unavailable: {e}")

    if not db:
        print("ERROR: No equipment data source found.")
        print("Make sure poi is installed and has been run at least once.")
        return

    out = EQUIP_DB_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    print(f"✓ Saved equip DB with {len(db)} items → {out}")


def batch_fetch(maps: list[str], force: bool = False) -> None:
    """Phase 1: download NGA posts and fleet screenshots to data/nga_raw/."""
    ok, fail = 0, 0
    for map_id in maps:
        if map_id not in NGA_MAP_PIDS:
            print(f"  SKIP {map_id}: no PID in index")
            continue
        print(f"\n→ {map_id}")
        try:
            rdir = fetch_and_save_raw(map_id, force=force)
            meta = json.loads((rdir / "meta.json").read_text(encoding="utf-8"))
            n_img = meta.get("fleet_screenshots_saved", 0)
            n_total = meta.get("total_image_urls", 0)
            print(f"  ✓ {meta.get('fetched_at', '')[:19]}  text: {len((rdir/'post.txt').read_text(encoding='utf-8'))} chars  images: {n_img}/{n_total} fleet screenshots")
            ok += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            fail += 1
    print(f"\nFetch done: {ok} ok, {fail} failed")


def batch_build(maps: list[str], output: Optional[str], model: str, debug: bool,
                overwrite: bool = False) -> None:
    """Phase 2: load raw data and generate strategy YAMLs using Claude."""
    equip_db = load_equip_db()
    output_dir = Path(output) if output else MAPS_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    ok, fail = 0, 0
    for map_id in maps:
        rdir = raw_dir(map_id)
        if not (rdir / "post.txt").exists():
            print(f"  SKIP {map_id}: no raw data — run --fetch first")
            fail += 1
            continue
        print(f"\n→ {map_id} (raw: {rdir})")
        try:
            text, images_by_idx = load_raw(map_id)
            print(f"  Images: {len(images_by_idx)}")
            raw_data = call_claude_interleaved(text, images_by_idx, map_id, model=model)
            raw_data["map"] = map_id
            raw_data.setdefault("source", "nga_import")
            _normalize_presets(raw_data)
            strategy = MapStrategy.model_validate(raw_data)
            strategy = correct_equip_names(strategy, equip_db)
            output_path = (map_yaml_path(map_id) if not output
                          else output_dir / f"{map_id.replace('-', '_')}.yaml")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if overwrite:
                save_strategy(strategy, str(output_path))
            else:
                merged = merge_strategy(str(output_path), strategy)
                save_strategy(merged, str(output_path))
            print(f"  ✓ {len(strategy.presets)} preset(s): {[p.name for p in strategy.presets]}")
            ok += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            if debug:
                import traceback; traceback.print_exc()
            fail += 1

    print(f"\nBuild done: {ok} ok, {fail} failed")


def batch_import(maps: list[str], output: Optional[str], model: str, debug: bool,
                 force_images: bool = False, overwrite: bool = False) -> None:
    """Import multiple maps: fetch raw data then build strategy YAMLs."""
    batch_fetch(maps, force=force_images)
    batch_build(maps, output, model, debug, overwrite=overwrite)


NGA_QUEST_PID = "454450573"  # 简易日/周/月/季/年常推荐海域配置


def _repair_json(raw: str) -> str:
    """Best-effort repair of truncated JSON: find last complete quest object."""
    # Try closing open structures progressively
    for suffix in ("]}}", "]}", "}]}", "]}"):
        try:
            json.loads(raw + suffix)
            return raw + suffix
        except json.JSONDecodeError:
            pass
    # Find the last complete {...} quest object
    depth, last_close = 0, -1
    for i, ch in enumerate(raw):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 1:   # closed a top-level quest object
                last_close = i
    if last_close > 0:
        truncated = raw[:last_close + 1]
        for suffix in ("]}", "]}}"):
            try:
                json.loads(truncated + suffix)
                return truncated + suffix
            except json.JSONDecodeError:
                pass
    return raw


def _call_quest_claude(text: str, category_filter: str, model: str) -> list[dict]:
    """Call Claude for one batch of quest categories; return list of quest dicts."""
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=8192,
        system=_quest_system_prompt(category_filter),
        messages=[{"role": "user", "content": text[:30000]}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Quest JSON parse failed, attempting repair…")
        raw = _repair_json(raw)
        data = json.loads(raw)
    return data.get("quests", [])


def import_quests(output: Optional[str], model: str, debug: bool) -> None:
    """Fetch the quest recommendation table and save one YAML file per quest."""
    url = f"https://bbs.nga.cn/read.php?pid={NGA_QUEST_PID}"
    print(f"Fetching quest index from {url} …")
    text = fetch_url_with_browser(url)
    print(f"  Fetched {len(text)} chars")

    all_quests: list[dict] = []
    for batch_label, categories in [
        ("daily+weekly", "daily (日常) and weekly (周常)"),
        ("monthly", "monthly (月常)"),
        ("quarterly", "quarterly (季常)"),
        ("yearly", "yearly (年常)"),
    ]:
        print(f"  Extracting {batch_label} quests …")
        try:
            quests = _call_quest_claude(text, categories, model)
            print(f"    → {len(quests)} quests")
            all_quests.extend(quests)
        except json.JSONDecodeError as e:
            print(f"  WARN: JSON error for {batch_label}: {e}", file=sys.stderr)
            if debug:
                raise
        except Exception as e:
            print(f"  WARN: error for {batch_label}: {e}", file=sys.stderr)
            if debug:
                raise

    quests_dir = Path(output) if output else QUESTS_DIR
    quests_dir.mkdir(parents=True, exist_ok=True)

    ok, fail = 0, 0
    for q in all_quests:
        # Ensure maps list is present
        for m in q.get("maps", []):
            m.setdefault("synergy_quests", [])

        quest_id = q.get("quest_id", "")
        try:
            entry = QuestEntry.model_validate(q)
        except Exception as e:
            print(f"  WARN: validation error for {quest_id}: {e}")
            if debug:
                raise
            fail += 1
            continue

        subdir = {"daily": "daily", "weekly": "weekly", "monthly": "monthly",
                  "quarterly": "quarterly", "yearly": "yearly"}.get(entry.category, "other")
        safe_name = entry.quest_name.replace("/", "").replace("\\", "").replace("\x00", "")
        out_path = quests_dir / subdir / f"{entry.quest_id}_{safe_name}.yaml"
        try:
            save_quest(entry, str(out_path))
            print(f"  ✓ {entry.quest_id} ({entry.quest_name[:30]}): {len(entry.maps)} map(s)")
            ok += 1
        except Exception as e:
            print(f"  WARN: save error for {quest_id}: {e}")
            fail += 1

    print(f"\n✓ Total: {ok} quests saved, {fail} failed — in {quests_dir}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(
        description="Import KanColle strategy from NGA into YAML"
    )
    parser.add_argument("--map", help="Map ID e.g. 5-4 (required unless --build-equip-db/--nga-login/--batch)")
    parser.add_argument("--url", help="NGA strategy thread URL (requires saved session via --nga-login)")
    parser.add_argument("--text", help="Path to text file (copy-pasted NGA content)")
    parser.add_argument("--html", help="Path to saved HTML file of NGA page")
    parser.add_argument("--img", nargs="+", help="Screenshot paths (glob supported)")
    parser.add_argument("--clip", action="store_true", help="Read from clipboard")
    parser.add_argument("--output", help="Output directory (default: strategies/)")
    parser.add_argument("--model", default="claude-sonnet-4-6", help="Claude model to use")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--build-equip-db",
        action="store_true",
        help="Build equipment name DB from poi cache",
    )
    parser.add_argument(
        "--nga-login",
        action="store_true",
        help="Open browser, log in to NGA, save session for future --url usage",
    )
    parser.add_argument(
        "--batch",
        metavar="MAPS",
        help="Comma-separated map IDs to batch import (e.g. 1-5,2-4,5-4) or 'all'",
    )
    parser.add_argument(
        "--quests",
        action="store_true",
        help="Import quest recommendation index (strategies/quests.yaml)",
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        dest="no_images",
        help="Skip downloading inline images from NGA posts (text-only extraction)",
    )
    parser.add_argument(
        "--force-images",
        action="store_true",
        dest="force_images",
        help="Ignore cached images and re-download (useful when increasing max_images)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing strategy files instead of merging presets",
    )
    parser.add_argument(
        "--fetch",
        metavar="MAPS",
        help="Phase 1: download NGA posts+images to data/nga_raw/ (e.g. 5-4,5-5 or all)",
    )
    parser.add_argument(
        "--build",
        metavar="MAPS",
        help="Phase 2: generate strategy YAMLs from data/nga_raw/ (e.g. 5-4,5-5 or all)",
    )

    args = parser.parse_args()

    if args.nga_login:
        nga_login_and_save()
        return

    if args.build_equip_db:
        build_equip_db_from_poi()
        return

    if args.quests:
        import_quests(args.output, args.model, args.debug)
        return

    if args.fetch:
        maps = sorted(NGA_MAP_PIDS.keys()) if args.fetch.strip().lower() == "all" \
               else [m.strip() for m in args.fetch.split(",") if m.strip()]
        batch_fetch(maps, force=args.force_images)
        return

    if args.build:
        maps = sorted(NGA_MAP_PIDS.keys()) if args.build.strip().lower() == "all" \
               else [m.strip() for m in args.build.split(",") if m.strip()]
        batch_build(maps, args.output, args.model, args.debug, overwrite=args.overwrite)
        return

    if args.batch:
        if args.batch.strip().lower() == "all":
            maps = sorted(NGA_MAP_PIDS.keys())
        else:
            maps = [m.strip() for m in args.batch.split(",") if m.strip()]
        batch_import(maps, args.output, args.model, args.debug,
                     force_images=args.force_images, overwrite=args.overwrite)
        return

    if not args.map:
        parser.error("--map is required unless --build-equip-db, --nga-login, --batch, --fetch, or --build is set")

    import_strategy(args)


if __name__ == "__main__":
    main()
