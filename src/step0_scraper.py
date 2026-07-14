"""
step0_scraper.py — Async product page scraper.
- Uses nodriver for high-stealth scraping (bypassing Walmart/PerimeterX and competitor bot blocks).
- Extracts __NEXT_DATA__ JSON for Walmart (link_1) or generic HTML parsing for competitor pages (link_2).

Usage:
    uv run python src/step0_scraper.py --limit 5 --workers 2
"""

from __future__ import annotations

import asyncio
import os
import re
import json
import httpx
import argparse
import random
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sys
sys.path.insert(0, str(Path(__file__).parent))

from bs4 import BeautifulSoup
import nodriver as uc
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from utils import get_logger, jsonl_append, jsonl_index_composite, load_pairs, jsonl_read

logger = get_logger("step0_scraper")

RAW_LINK1       = Path("data/raw/link1_raw.jsonl")
RAW_LINK2       = Path("data/raw/link2_raw.jsonl")
WORKERS         = 2
CHROME_PROFILE  = Path("chrome_profile").resolve()  # Persistent profile → preserves cookies/session across runs


# ── Virtual Display helper (Xvfb) ─────────────────────────────────────────────

_xvfb_proc: subprocess.Popen | None = None

def _start_virtual_display(display: str = ":99", screen: str = "1280x800x24") -> bool:
    """
    Start a virtual X display with Xvfb so Chrome can render invisibly.
    Returns True if Xvfb was started successfully, False if not available.
    """
    global _xvfb_proc
    if not shutil.which("Xvfb"):
        logger.warning("Xvfb not found — Chrome window will be visible. Install with: sudo apt-get install -y xvfb")
        return False
    if _xvfb_proc and _xvfb_proc.poll() is None:
        # Already running — ensure DISPLAY is still set correctly
        os.environ["DISPLAY"] = display
        return True
    try:
        _xvfb_proc = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", screen, "-ac"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.5)  # give Xvfb a moment to start
        os.environ["DISPLAY"] = display
        logger.info(f"Xvfb virtual display started on {display}")
        return True
    except Exception as e:
        logger.warning(f"Could not start Xvfb: {e}")
        return False


def _stop_virtual_display():
    global _xvfb_proc
    if _xvfb_proc and _xvfb_proc.poll() is None:
        _xvfb_proc.terminate()
        _xvfb_proc = None
        logger.info("Xvfb virtual display stopped.")


# Capture the real X display (e.g. ":0") BEFORE Xvfb overwrites os.environ["DISPLAY"].
# This is used by scrape_single_url_content so the browser appears on the user's
# actual monitor, allowing manual CAPTCHA solving.
_REAL_DISPLAY: str = os.environ.get("DISPLAY", ":0")

# Start virtual display immediately on module import so DISPLAY is set
# before any Chrome process is ever spawned.
_start_virtual_display()


def _get_chrome_ua() -> str:
    """
    Build a User-Agent string that matches the *actually installed* Chrome version.
    Falls back to a safe recent version string if detection fails.
    PerimeterX cross-checks the declared UA version against TLS/JS fingerprints,
    so a mismatched version (e.g. Chrome/122 UA on a Chrome/148 binary) is a
    strong bot signal.
    """
    try:
        result = subprocess.run(
            [shutil.which("google-chrome") or "/usr/bin/google-chrome", "--version"],
            capture_output=True, text=True, timeout=5
        )
        # output: "Google Chrome 148.0.7778.167"
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", result.stdout)
        if m:
            ver = m.group(1)
            major = ver.split(".")[0]
            return (
                f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                f"AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{major}.0.0.0 Safari/537.36"
            )
    except Exception as e:
        logger.debug(f"Chrome version detection failed: {e}")
    # Safe fallback — keep this close to latest stable
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    )


# Detect once at import time so all launch sites share the same UA
_CHROME_UA = _get_chrome_ua()
logger.info(f"Detected Chrome UA: {_CHROME_UA}")


def parse_walmart_next_data(html: str) -> dict[str, Any] | None:
    """
    Extract product data from Walmart's embedded __NEXT_DATA__ JSON.
    Path: props → pageProps → initialData → data → product / idml
    Returns raw dict or None if not found.
    """
    m = re.search(
        r'<script[^>]*__NEXT_DATA__[^>]*>(.*?)</script>',
        html, re.DOTALL
    )
    if not m:
        return None

    try:
        data = json.loads(m.group(1))
        initial_data = data.get("props", {}).get("pageProps", {}).get("initialData", {})
        data_block = initial_data.get("data", {})
        product_raw = data_block.get("product", {})
        idml_raw = data_block.get("idml", {})

        # All specification key-values from the structured spec section
        spec_table: dict[str, str] = {}
        
        # 1. Specs from idml
        for spec in idml_raw.get("specifications", []):
            k = spec.get("name", "").strip()
            v = spec.get("value", "").strip()
            if k and v:
                spec_table[k] = v
                
        # 2. Specs from product
        for spec_section in product_raw.get("specifications", []):
            for spec in spec_section.get("specifications", []):
                k = spec.get("name", "").strip()
                v = spec.get("value", "").strip()
                if k and v:
                    spec_table.setdefault(k, v)
                    
        # 3. Specs from specificationSection
        for section in product_raw.get("specificationSection", {}).get("specifications", []):
            for spec in section.get("specifications", []):
                k = spec.get("name","").strip()
                v = spec.get("value","").strip()
                if k and v:
                    spec_table.setdefault(k, v)

        # Bullet points / short description (try idml first, then product)
        short_desc = idml_raw.get("shortDescription", "") or product_raw.get("shortDescription", "")
        bullet_points: list[str] = []
        if short_desc:
            soup = BeautifulSoup(short_desc, "lxml")
            bullet_points = [li.get_text(strip=True) for li in soup.find_all("li")]
            if not bullet_points:
                bullet_points = [p.get_text(strip=True) for p in soup.find_all("p") if p.get_text(strip=True)]

        # Description (try idml first, then product)
        description = idml_raw.get("longDescription", "") or product_raw.get("longDescription", "")

        # Images
        image_info = product_raw.get("imageInfo", {})
        image_urls = [
            img["url"]
            for img in image_info.get("allImages", [])
            if img.get("url")
        ][:5]

        # Brand
        brand = (product_raw.get("brand", "") or product_raw.get("brandName", "") or "").strip()

        return {
            "title":        product_raw.get("name", ""),
            "brand":        brand,
            "description":  description[:2000],
            "bullet_points": bullet_points[:20],
            "spec_table":   spec_table,
            "image_urls":   image_urls,
            "price_raw":    str(product_raw.get("priceInfo", {}).get("currentPrice", {}).get("priceString", "")),
            "breadcrumb":   [],
            "_source":      "next_data",
        }
    except Exception as e:
        logger.debug(f"__NEXT_DATA__ parse error: {e}")
        return None


# ── Generic HTML parser (fallback / competitor pages) ─────────────────────────

def parse_page_html(html: str, url: str) -> dict[str, Any]:
    """Generic parser — extracts ALL available data from any product page."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
        tag.decompose()

    # Title
    title = ""
    for sel in ['#productTitle', '#title', 'meta[property="og:title"]', 'title', 'h1']:
        el = soup.select_one(sel)
        if el:
            title = el.get("content", "") or el.get_text(strip=True)
            if title:
                # Clean up screen-reader/accessibility text if present
                title = title.replace("Product summary presents key product informationKeyboard shortcutshift+alt+opt+D", "").strip()
                if title:
                    break

    # Brand
    brand = ""
    for sel in ['meta[property="og:brand"]', '[itemprop="brand"]',
                '[data-testid="brand-name"]', 'a[data-type="brandName"]']:
        el = soup.select_one(sel)
        if el:
            brand = el.get("content", "") or el.get_text(strip=True)
            if brand: break

    # Price
    price_raw = ""
    for sel in ['meta[property="product:price:amount"]', '[itemprop="price"]',
                ".price", ".a-price", "#priceblock_ourprice"]:
        el = soup.select_one(sel)
        if el:
            price_raw = el.get("content", "") or el.get_text(strip=True)
            if price_raw: break

    # Spec table — ALL key-value pairs, fully dynamic
    spec_table: dict[str, str] = {}
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) >= 2:
                k = cells[0].get_text(strip=True)
                v = cells[1].get_text(strip=True)
                if k and v and len(k) < 80: spec_table[k] = v

    for dl in soup.find_all("dl"):
        for dt, dd in zip(dl.find_all("dt"), dl.find_all("dd")):
            k, v = dt.get_text(strip=True), dd.get_text(strip=True)
            if k and v: spec_table[k] = v

    for el in soup.find_all(["div", "li"], class_=re.compile(
            r"(spec|attribute|detail|feature|characteristic|property)", re.I)):
        parts = el.get_text(separator="|", strip=True).split("|")
        if len(parts) == 2 and parts[0] and parts[1] and len(parts[0]) < 80:
            spec_table.setdefault(parts[0].strip(), parts[1].strip())

    # Amazon product details
    for row in soup.select("#productDetails_techSpec_section_1 tr, #productDetails_detailBullets_sections1 tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) >= 2:
            k = cells[0].get_text(strip=True).strip(":")
            v = cells[1].get_text(strip=True)
            if k and v: spec_table.setdefault(k, v)

    # General colon-split spec extraction from simple list/div elements
    _JUNK_KEY_PREFIXES = [
        "note", "warning", "caution", "please",
        "current time", "duration", "loaded", "buffered",
        "playback", "volume", "seek", "mute", "fullscreen",
        "add to", "notify", "sold by", "ships from", "sold and",
        "report", "feedback", "share", "follow", "subscribe",
    ]
    for el in soup.find_all(["li", "tr", "div"]):
        if el.find(["table", "ul", "ol"]):
            continue
        text = el.get_text(separator=" ", strip=True)
        if ":" in text:
            parts = text.split(":", 1)
            k = parts[0].strip()
            v = parts[1].strip()
            if 1 < len(k) < 40 and v and len(v) < 300:
                k_lower = k.lower()
                if not any(k_lower.startswith(x) for x in _JUNK_KEY_PREFIXES):
                    k_norm = k.replace("\u200e", "").strip()
                    if k_norm not in spec_table:
                        spec_table[k_norm] = v

    # Bullet points
    bullet_points: list[str] = []
    for sel in ["#feature-bullets li", ".product-bullets li",
                '[data-testid="product-highlights"] li', ".a-unordered-list .a-list-item"]:
        items = soup.select(sel)
        if items:
            bullet_points = [el.get_text(strip=True) for el in items if el.get_text(strip=True)]
            break

    # Description
    description = ""
    for sel in ['[itemprop="description"]', '#productDescription',
                '[data-testid="product-description-content"]',
                'meta[property="og:description"]']:
        el = soup.select_one(sel)
        if el:
            description = el.get("content", "") or el.get_text(separator=" ", strip=True)
            if description: break

    # Images
    image_urls: list[str] = []

    # 1. Target high-res product image selectors first (especially for Amazon/Walmart/Target)
    selectors = [
        "#landingImage", "#imgBlkFront", "#main-image", "img.a-dynamic-image",
        ".product-image img", ".main-image img", "[data-testid='hero-image'] img"
    ]
    for sel in selectors:
        for img in soup.select(sel):
            # Try data-a-dynamic-image first (contains JSON map of high-res URLs to dimensions)
            dyn = img.get("data-a-dynamic-image")
            if dyn:
                try:
                    urls = json.loads(dyn)
                    if urls:
                        # Sort by dimension
                        sorted_urls = sorted(urls.items(), key=lambda x: x[1][0] * x[1][1], reverse=True)
                        src = sorted_urls[0][0]
                        if src and src.startswith("http") and src not in image_urls:
                            image_urls.append(src)
                except Exception:
                    pass
            # Try other high-res attributes
            for attr in ["data-old-hires", "data-a-image-src", "src", "data-src"]:
                src = img.get(attr)
                if src and src.startswith("http") and "sprite" not in src.lower() and "logo" not in src.lower():
                    if src not in image_urls:
                        image_urls.append(src)
                        break

    # 2. Add og:image
    for meta in soup.select('meta[property="og:image"]'):
        src = meta.get("content", "")
        if src and src.startswith("http") and "logo" not in src.lower() and "sprite" not in src.lower():
            if src not in image_urls:
                image_urls.append(src)

    # 3. Collect other product-like images (containing m.media-amazon.com/images/I/ or walmartimages)
    for img in soup.find_all("img"):
        src = img.get("src", "") or img.get("data-src", "")
        if src and src.startswith("http") and src not in image_urls:
            is_product_pattern = ("media-amazon.com/images/I/" in src) or ("walmartimages.com" in src) or ("target.scene7.com" in src)
            is_junk = any(x in src.lower() for x in ["sprite", "logo", "pixel", "transparent", "indicator", "loading", "nav-", "header", "footer"])
            if is_product_pattern and not is_junk:
                # Upgrade thumbnail to high resolution by removing size modifier (e.g. ._AC_SR38,50_)
                clean_src = re.sub(r"\._[A-Za-z0-9_,-]+\.(jpg|jpeg|png|gif)$", r".\1", src)
                if clean_src not in image_urls:
                    image_urls.append(clean_src)

    # 4. Fallback to general images if we still have nothing
    if not image_urls:
        for img in soup.find_all("img"):
            src = img.get("src", "") or img.get("data-src", "")
            if src and src.startswith("http") and src not in image_urls:
                try:
                    if int(str(img.get("width", 999))) > 100: image_urls.append(src)
                except (ValueError, TypeError):
                    image_urls.append(src)
            if len(image_urls) >= 5: break

    breadcrumb = [
        el.get_text(strip=True)
        for el in soup.select('[aria-label="breadcrumb"] a, .breadcrumb a, .a-breadcrumb a')
        if el.get_text(strip=True)
    ]

    return {
        "title": title, "brand": brand, "price_raw": price_raw,
        "description": description[:2000], "bullet_points": bullet_points[:20],
        "spec_table": spec_table, "image_urls": image_urls[:5],
        "breadcrumb": breadcrumb, "_source": "html_parse",
    }


# ── Target __NEXT_DATA__ parser ───────────────────────────────────────────────

def parse_target_next_data(html: str) -> dict[str, Any] | None:
    try:
        soup = BeautifulSoup(html, "lxml")
        script = soup.find("script", id="__NEXT_DATA__")
        if not script:
            return None
        data = json.loads(script.string) or {}
        
        props = data.get("props") or {}
        
        # Check for Target Ghost Block (404/Out of stock due to Datacenter IP blocking)
        page_props = props.get("pageProps") or {}
        if page_props.get("statusCode") == 404:
            raise ValueError("TARGET_GHOST_BLOCK")
            
        dehydrated_state = props.get("dehydratedState") or {}
        queries = dehydrated_state.get("queries") or []
        
        product = None
        
        def find_product(d):
            if isinstance(d, dict):
                # Target's new schema nests product in data_source_modules -> module_data -> data -> product
                # or old schema state -> data -> data -> product
                if "product" in d and isinstance(d["product"], dict) and "item" in d["product"]:
                    return d["product"]
                for k, v in d.items():
                    res = find_product(v)
                    if res: return res
            elif isinstance(d, list):
                for item in d:
                    res = find_product(item)
                    if res: return res
            return None

        product = find_product(queries)
            
        if not product:
            return None
            
        item = product.get("item", {})
        desc = item.get("product_description", {})
        
        # Title
        title = desc.get("title", "")
        
        # Brand
        brand = item.get("primary_brand", {}).get("name", "")
        
        # Spec table (parsed from bullet_descriptions)
        spec_table = {}
        for spec_str in desc.get("bullet_descriptions", []):
            clean_str = re.sub(r"<[^>]+>", "", spec_str).strip()
            if ":" in clean_str:
                k, v = clean_str.split(":", 1)
                spec_table[k.strip()] = v.strip()
                
        # Bullet points
        bullet_points = desc.get("soft_bullets", {}).get("bullets", [])
        
        # Image URLs
        image_urls = []
        enrichment = item.get("enrichment", {})
        images = enrichment.get("images", {})
        if images:
            primary = images.get("primary_image_url")
            if primary:
                image_urls.append(primary)
            for alt in images.get("alternate_image_urls", []):
                if alt not in image_urls:
                    image_urls.append(alt)
                    
        if not image_urls:
            for img in soup.find_all("img"):
                src = img.get("src")
                if src and "target" in src and src not in image_urls:
                    image_urls.append(src)

        return {
            "title": title,
            "brand": brand,
            "description": desc.get("downspout_description", "")[:2000],
            "bullet_points": bullet_points[:20],
            "spec_table": spec_table,
            "image_urls": image_urls[:5],
            "price_raw": "",
            "breadcrumb": [],
            "_source": "target_next_data",
        }
    except Exception as e:
        logger.debug(f"Target NEXT_DATA parsing failed: {e}")
        return None


# ── Flipkart parser ───────────────────────────────────────────────────────────

def parse_flipkart(html: str) -> dict[str, Any] | None:
    """
    Parse Flipkart product pages.
    Specs use grid-formation grid-column-2 divs; price is in ₹ span.
    """
    try:
        soup = BeautifulSoup(html, "lxml")

        # Title — h1 tag
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else ""
        if not title:
            m = re.search(r'"name"\s*:\s*"([^"]+)"', html)
            title = m.group(1) if m else ""

        # Brand — from title first word or meta
        brand = ""
        meta_brand = soup.select_one('meta[property="og:brand"], [itemprop="brand"]')
        if meta_brand:
            brand = meta_brand.get("content", "") or meta_brand.get_text(strip=True)
        if not brand and title:
            brand = title.split()[0]

        # Price — find ₹ in text
        price_raw = ""
        for el in soup.find_all(string=re.compile(r"₹\s*[\d,]+")):
            m = re.search(r"₹\s*([\d,]+)", el)
            if m:
                price_raw = "₹" + m.group(1).replace(",", "")
                break

        # Specs — Flipkart renders specs as a long concatenated string inside a grid div.
        # The format is: "BrandJOCKEYTypeRound NeckSleeveHalf SleeveFitRegular..."
        # We extract this blob and split on known spec key names.
        spec_table: dict[str, str] = {}
        FK_SPEC_KEYS = [
            "Brand", "Type", "Sleeve", "Fit", "Fabric", "Sales Package", "Pack of",
            "Style Code", "Neck Type", "Ideal For", "Size", "Pattern", "Suitable For",
            "Reversible", "Fabric Care", "Net Quantity", "Color", "Brand Color",
            "Occasion", "Sleeve Details", "Surface Styling", "Tee Length", "Trend",
            "Material", "Care", "Wash", "Set Contains", "General",
        ]
        # Find the grid that contains the long concatenated spec text
        for grid in soup.select(".grid-formation"):
            text = grid.get_text(separator="", strip=True)
            # The concatenated spec block typically starts with a known key and is very long
            if len(text) > 100:
                # Build a regex that splits on the known keys
                pattern = "(" + "|".join(re.escape(k) for k in FK_SPEC_KEYS) + ")"
                parts = re.split(pattern, text)
                # parts will be: ['', 'Brand', 'JOCKEY', 'Type', 'Round Neck', ...]
                key = None
                for part in parts:
                    part = part.strip()
                    if not part:
                        continue
                    if part in FK_SPEC_KEYS:
                        key = part
                    elif key:
                        if key.lower() != "general" and len(part) < 200:
                            spec_table.setdefault(key, part)
                        key = None
                if spec_table:
                    break

        # Also try simple 2-text grid pairs (for pages with clean key | value grids)
        if not spec_table:
            for grid in soup.select(".grid-formation"):
                texts = [d.get_text(strip=True) for d in grid.find_all("div", recursive=False) if d.get_text(strip=True)]
                if len(texts) == 2:
                    k, v = texts[0], texts[1]
                    if k and v and len(k) < 60:
                        spec_table.setdefault(k, v)

        # Images — Flipkart uses rukminim1/rukminim2.flixcart.com CDN
        image_urls: list[str] = []
        for img in soup.find_all("img"):
            src = img.get("src", "") or img.get("data-src", "")
            if src and "rukminim" in src:
                # Upgrade to high-res: replace /image/X/Y/ with /image/832/832/
                src = re.sub(r"/(?:image|fk-p-thumbnail)/\d+/\d+/", "/image/832/832/", src)
                # Strip query parameters like ?q=80
                src = src.split("?")[0]
                if src not in image_urls:
                    image_urls.append(src)
                    if len(image_urls) >= 5:
                        break

        if not title and not spec_table:
            return None

        return {
            "title": title,
            "brand": brand,
            "description": "",
            "bullet_points": [],
            "spec_table": spec_table,
            "image_urls": image_urls,
            "price_raw": price_raw,
            "breadcrumb": [],
            "_source": "flipkart_parser",
        }
    except Exception as e:
        logger.debug(f"Flipkart parse error: {e}")
        return None


def parse_myntra(html: str) -> dict[str, Any] | None:
    try:
        # First, try to extract window.__myx structured JSON
        idx = html.find("window.__myx = ")
        if idx != -1:
            try:
                start = idx + len("window.__myx = ")
                brace_count = 0
                end = start
                for i in range(start, len(html)):
                    if html[i] == "{":
                        brace_count += 1
                    elif html[i] == "}":
                        brace_count -= 1
                        if brace_count == 0:
                            end = i + 1
                            break
                json_str = html[start:end]
                data = json.loads(json_str)
                pdp = data.get("pdpData", {})
                if pdp:
                    brand = pdp.get("brand", {}).get("name", "") or ""
                    name = pdp.get("name", "") or ""
                    title = f"{brand} {name}".strip()
                    
                    price_val = pdp.get("price", {}).get("discounted") or pdp.get("mrp")
                    price_raw = f"₹{price_val}" if price_val else ""

                    spec_table: dict[str, str] = {}
                    # Load article attributes (e.g. Fit, Neck, Fabrics, Sleeve Length)
                    for k, val in pdp.get("articleAttributes", {}).items():
                        if val and val != "NA":
                            spec_table[k] = val

                    # Extract size measurements and sizes as bullet points or specs
                    bullet_points = []
                    for size_info in pdp.get("sizes", []):
                        lbl = size_info.get("label")
                        if lbl and size_info.get("available"):
                            bullet_points.append(f"Available Size: {lbl}")

                    description = ""
                    for d in pdp.get("productDetails", []):
                        t_title = d.get("title", "").upper()
                        desc = d.get("description", "")
                        if "MATERIAL" in t_title or "CARE" in t_title:
                            description = re.sub(r"<[^>]+>", " ", desc).strip()
                            # Parse any remaining key-values inside description
                            for line in re.split(r"<br\s*/?>", desc):
                                line = re.sub(r"<[^>]+>", "", line).strip()
                                if ":" in line:
                                    k, v = line.split(":", 1)
                                    if 1 < len(k.strip()) < 50:
                                        spec_table.setdefault(k.strip(), v.strip())

                    # Images
                    image_urls = []
                    for col in pdp.get("colours", []):
                        img_url = col.get("image", "")
                        if img_url:
                            # Standardize to high-res CDN URL
                            img_url = re.sub(r"h_\d+,w_\d+,c_fill,g_auto/", "", img_url)
                            if img_url not in image_urls:
                                image_urls.append(img_url)
                    
                    for media_item in pdp.get("media", {}).get("albums", []):
                        for img in media_item.get("images", []):
                            src = img.get("imageURL", "")
                            if src:
                                src = re.sub(r"h_\d+,w_\d+,c_fill,g_auto/", "", src)
                                if src not in image_urls:
                                    image_urls.append(src)

                    # Ensure we have fallback images if none extracted
                    if not image_urls:
                        for img in pdp.get("media", {}).get("images", []):
                            src = img.get("imageURL", "")
                            if src:
                                src = re.sub(r"h_\d+,w_\d+,c_fill,g_auto/", "", src)
                                image_urls.append(src)

                    return {
                        "title": title,
                        "brand": brand,
                        "description": description[:2000],
                        "bullet_points": bullet_points[:20],
                        "spec_table": spec_table,
                        "image_urls": image_urls[:5],
                        "price_raw": price_raw,
                        "breadcrumb": [],
                        "_source": "myntra_json_parser",
                    }
            except Exception as js_err:
                logger.debug(f"Myntra __myx JSON parsing failed, falling back to DOM: {js_err}")

        # Fallback DOM Parser
        soup = BeautifulSoup(html, "lxml")

        # Title: brand in .pdp-title, name in .pdp-name
        brand_el = soup.select_one(".pdp-title")
        name_el  = soup.select_one(".pdp-name")
        brand = brand_el.get_text(strip=True) if brand_el else ""
        name  = name_el.get_text(strip=True) if name_el else ""
        title = f"{brand} {name}".strip() if brand and name else (brand or name)

        # Price
        price_raw = ""
        price_el = soup.select_one(".pdp-price, .pdp-discount-container")
        if price_el:
            m = re.search(r"₹\s*([\d,]+)", price_el.get_text())
            if m:
                price_raw = "₹" + m.group(1).replace(",", "")

        spec_table: dict[str, str] = {}
        bullet_points: list[str] = []
        description = ""

        for script in soup.find_all("script"):
            t = script.string or ""
            if "MATERIAL" not in t.upper() and "CARE" not in t.upper():
                continue
            t_decoded = t.replace("\\u003C", "<").replace("\\u003E", ">").replace("\\u002F", "/")
            for m in re.finditer(r'"title"\s*:\s*"([^"]+)"[^}]*?"description"\s*:\s*"(.*?)"(?=\s*[,}])', t_decoded, re.DOTALL):
                section_title = m.group(1).strip()
                section_desc  = m.group(2).strip()
                clean_desc = re.sub(r"<[^>]+>", " ", section_desc).strip()
                clean_desc = re.sub(r"\s+", " ", clean_desc).strip()

                if "MATERIAL" in section_title.upper() or "CARE" in section_title.upper():
                    description = clean_desc
                    for line in re.split(r"<br\s*/?>", section_desc):
                        line = re.sub(r"<[^>]+>", "", line).strip()
                        if ":" in line:
                            k, v = line.split(":", 1)
                            k, v = k.strip(), v.strip()
                            if 1 < len(k) < 50 and v:
                                spec_table.setdefault(k, v)
                elif "SIZE" in section_title.upper() or "FIT" in section_title.upper():
                    for line in re.split(r"<br\s*/?>|<li>", section_desc):
                        line = re.sub(r"<[^>]+>", "", line).strip()
                        if line:
                            bullet_points.append(line)
            if spec_table:
                break

        # If no JSON specs, try DOM selectors
        if not spec_table:
            for row in soup.select(".index-rowContainer, .pdp-product-description-content li"):
                text = row.get_text(separator=" | ", strip=True)
                if " | " in text:
                    parts = text.split(" | ", 1)
                    k, v = parts[0].strip(), parts[1].strip()
                    if k and v and len(k) < 60:
                        spec_table.setdefault(k, v)
                elif ":" in text:
                    k, v = text.split(":", 1)
                    k, v = k.strip(), v.strip()
                    if k and v and len(k) < 60:
                        spec_table.setdefault(k, v)

        # Images
        image_urls: list[str] = []
        og_img = soup.select_one('meta[property="og:image"]')
        if og_img:
            src = og_img.get("content", "")
            if src:
                src = re.sub(r"h_\d+,w_\d+,c_fill,g_auto/", "", src)
                image_urls.append(src)
        for img in soup.find_all("img"):
            src = img.get("src", "") or img.get("data-src", "")
            if src and "myntassets.com" in src and src not in image_urls:
                src = re.sub(r"h_\d+,w_\d+,c_fill,g_auto/", "", src)
                image_urls.append(src)
                if len(image_urls) >= 5:
                    break

        if not title and not spec_table:
            return None

        return {
            "title": title,
            "brand": brand,
            "description": description[:2000],
            "bullet_points": bullet_points[:20],
            "spec_table": spec_table,
            "image_urls": image_urls[:5],
            "price_raw": price_raw,
            "breadcrumb": [],
            "_source": "myntra_dom_parser",
        }
    except Exception as e:
        logger.debug(f"Myntra parse error: {e}")
        return None


def is_amazon_blocked(html: str) -> bool:
    if not html:
        return True
    indicators = [
        "api-services-support@amazon.com",
        "make sure you're not a robot",
        "enter the characters you see below",
        "robot check",
        "something went wrong",
        "dogs of amazon",
    ]
    html_lower = html.lower()
    return any(ind in html_lower for ind in indicators)


def is_perimeterx_blocked(html: str) -> bool:
    if not html:
        return True
    indicators = [
        "robot or human?",
        "verify your identity",
        "unusual activity from your computer network",
        "access to this page has been denied",
        "press & hold",
        "press and hold",
        "block-disclaimer",
    ]
    html_lower = html.lower()
    return any(ind in html_lower for ind in indicators)


async def _solve_press_hold_xdotool(page, pair_id: str = "?", link_key: str = "?") -> bool:
    """
    Automated PerimeterX 'Press & Hold' solver using xdotool.

    xdotool injects X11 input events at the OS level — Chrome's renderer treats
    them identically to real physical hardware input. Unlike CDP Input.dispatchMouseEvent
    (which PerimeterX can fingerprint via timing/source analysis), X11 events pass through
    the kernel input stack and are 100% indistinguishable from a real mouse.

    Steps:
      1. Find the captcha button via JS getBoundingClientRect.
      2. Compute absolute screen coords (window position + toolbar offset + button offset).
      3. Move mouse → mousedown → jitter for 4-5 s → mouseup.
      4. Poll for captcha resolution.
    """
    if not shutil.which("xdotool"):
        logger.warning(f"[{pair_id}][{link_key}] xdotool not found — install with: sudo apt-get install -y xdotool")
        return False

    try:
        await page.sleep(2.0 + random.uniform(0.3, 0.7))  # let captcha fully render

        def _unwrap(v):
            """
            nodriver wraps JS values as Python dicts: {'type': 'number', 'value': 123.4}
            This helper extracts the raw value regardless of format.
            """
            if isinstance(v, dict):
                return v.get("value", 0)
            return v

        # ── 1. Find button position in viewport ───────────────────────────────
        # Return a flat array [x, y, found] to avoid object-deserialisation issues
        pos_raw = await page.evaluate("""
            (function() {
                var sels = [
                    '#px-captcha', 'div[id*="px-captcha"]', 'div[class*="px-captcha"]',
                    'div[class*="captcha-container"]', 'div[class*="px-block"]',
                    '[data-testid*="captcha"]',
                ];
                var targetEl = null;
                for (var s of sels) {
                    var el = document.querySelector(s);
                    if (el && el.getBoundingClientRect().width > 10) {
                        targetEl = el;
                        break;
                    }
                }
                if (targetEl) {
                    targetEl.scrollIntoView({block: 'center'});
                    return [0, 0, 1]; // Signal we found it and scrolled
                }
                return [0, 0, 0];
            })()
        """)
        logger.debug(f"[{pair_id}][{link_key}] pos_raw={pos_raw!r}")

        if not pos_raw or (isinstance(pos_raw, list) and len(pos_raw) >= 3 and _unwrap(pos_raw[2]) == 0):
            html = await page.get_content()
            logger.error(f"[{pair_id}][{link_key}] PerimeterX HARD BLOCK detected! No interactive CAPTCHA button found. Your IP is completely blocked by the retailer.")
            return False

        # Give the page a moment to scroll
        await asyncio.sleep(0.5)

        # Now get the exact bounding client rect after scrolling
        pos_rect = await page.evaluate("""
            (function() {
                var sels = [
                    '#px-captcha', 'div[id*="px-captcha"]', 'div[class*="px-captcha"]',
                    'div[class*="captcha-container"]', 'div[class*="px-block"]',
                    '[data-testid*="captcha"]',
                ];
                for (var s of sels) {
                    var el = document.querySelector(s);
                    if (el && el.getBoundingClientRect().width > 10) {
                        var r = el.getBoundingClientRect();
                        return [r.left + r.width/2, r.top + r.height/2];
                    }
                }
                return [0, 0];
            })()
        """)

        btn_x = float(_unwrap(pos_rect[0]))
        btn_y = float(_unwrap(pos_rect[1]))

        # ── 2. Focus Chrome window ────────────────────────────────────────────
        disp = os.environ.get("DISPLAY", ":99")
        env  = {**os.environ, "DISPLAY": disp}

        win_id_result = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--class", "google-chrome"],
            env=env, capture_output=True, text=True
        )
        if win_id_result.stdout.strip():
            wid = win_id_result.stdout.strip().split("\n")[0]
            subprocess.run(["xdotool", "windowfocus", "--sync", wid], env=env, capture_output=True)
            subprocess.run(["xdotool", "windowraise", wid], env=env, capture_output=True)
            logger.info(f"[{pair_id}][{link_key}] Focused Chrome window id={wid}")
            await asyncio.sleep(0.3)

        # ── 3. Calibrate X11-to-Viewport Offset ───────────────────────────────
        # Inject listener for the calibration click
        await page.evaluate("""
            window.calibClick = null;
            window._calibListener = function(e) { window.calibClick = {x: e.clientX, y: e.clientY}; };
            document.addEventListener('mousedown', window._calibListener, true);
        """)
        
        # Click at arbitrary absolute screen coordinate (100, 200)
        subprocess.run(["xdotool", "mousemove", "100", "200"], env=env, capture_output=True)
        subprocess.run(["xdotool", "click", "1"], env=env, capture_output=True)
        await asyncio.sleep(0.15)
        
        calib = await page.evaluate("window.calibClick")
        await page.evaluate("document.removeEventListener('mousedown', window._calibListener, true);")
        
        if calib and "x" in calib and "y" in calib:
            offset_x = 100 - float(calib["x"])
            offset_y = 200 - float(calib["y"])
            logger.info(f"[{pair_id}][{link_key}] Calibrated X11 offset: dx={offset_x}, dy={offset_y}")
        else:
            logger.warning(f"[{pair_id}][{link_key}] Calibration click failed to register in viewport. Using fallback math.")
            offset_x = 10
            offset_y = 97

        sx = int(btn_x + offset_x)
        sy = int(btn_y + offset_y)
        logger.info(f"[{pair_id}][{link_key}] CAPTCHA button targeted at absolute screen ({sx}, {sy})")

        # Smooth approach curve (ease-out cubic) with micro-noise
        steps = 18
        start_x = sx - random.randint(150, 300)
        start_y = sy + random.randint(50, 150)
        for i in range(steps):
            t = i / float(steps - 1)
            ease_t = 1 - (1 - t) ** 3
            cx = start_x + (sx - start_x) * ease_t + random.randint(-1, 1)
            cy = start_y + (sy - start_y) * ease_t + random.randint(-1, 1)
            subprocess.run(["xdotool", "mousemove", str(int(cx)), str(int(cy))], env=env)
            await asyncio.sleep(random.uniform(0.015, 0.04))
        
        await asyncio.sleep(0.1 + random.uniform(0.05, 0.15))
        
        # Jitter exactly on the button for half a second before pressing
        for _ in range(3):
            jx = sx + random.randint(-4, 4)
            jy = sy + random.randint(-4, 4)
            subprocess.run(["xdotool", "mousemove", str(jx), str(jy)], env=env, capture_output=True)
            await asyncio.sleep(0.15 + random.uniform(0.02, 0.08))

        # ── 4. Press & hold with micro-jitter ────────────────────────────────
        hold_time = 4.0 + random.uniform(0.5, 1.5)
        logger.info(f"[{pair_id}][{link_key}] xdotool holding for {hold_time:.1f}s...")
        subprocess.run(["xdotool", "mousedown", "1"], env=env, capture_output=True)

        # Hold perfectly still to avoid dragging out of the target
        await asyncio.sleep(hold_time - 0.5)
        
        # Take a screenshot right before release to verify visual state
        try:
            await page.save_screenshot(f"/home/pranav/projects/live-ass/captcha_hold_{link_key[-5:]}.png")
            logger.info(f"[{pair_id}][{link_key}] Saved debug screenshot during hold.")
        except Exception as e:
            logger.warning(f"Failed to save screenshot: {e}")
            
        await asyncio.sleep(0.5)

        subprocess.run(["xdotool", "mouseup", "1"], env=env, capture_output=True)
        logger.info(f"[{pair_id}][{link_key}] Released. Polling for PX resolution...")

        # ── 5. Poll for resolution ────────────────────────────────────────────
        for _ in range(8):
            await page.sleep(1.5 + random.uniform(0.2, 0.5))
            try:
                html = await page.get_content()
                if not is_perimeterx_blocked(html):
                    logger.info(f"[{pair_id}][{link_key}] ✓ CAPTCHA solved via xdotool!")
                    return True
            except Exception:
                pass # ignore navigation errors during reload

        logger.warning(f"[{pair_id}][{link_key}] CAPTCHA still present after xdotool attempt")
        return False

    except Exception as e:
        logger.warning(f"[{pair_id}][{link_key}] xdotool solver error: {e}")
        return False


async def scrape_url_with_stealth_and_retry(
    browser: uc.Browser,
    url: str,
    is_walmart: bool,
    pair_id: str = "?",
    link_key: str = "?",
    max_attempts: int = 3,
) -> dict:
    is_target = "target.com" in url
    
    # ── Fast-Path for Target (Bypass Headless Chrome Telemetry) ──────────
    if is_target:
        try:
            logger.info(f"[{pair_id}][{link_key}] Target URL detected. Attempting fast-path httpx bypass...")
            async with httpx.AsyncClient(http2=True) as client:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.5",
                }
                resp = await client.get(url, headers=headers, follow_redirects=True, timeout=15.0)
                if resp.status_code == 200 and "__NEXT_DATA__" in resp.text:
                    logger.info(f"[{pair_id}][{link_key}] Target httpx bypass successful!")
                    raw = parse_target_next_data(resp.text)
                    if raw and raw.get("spec_table"):
                        return raw
        except Exception as e:
            logger.warning(f"[{pair_id}][{link_key}] Target fast-path failed: {e}")

    page = None
    for attempt in range(1, max_attempts + 1):
        try:
            page = await asyncio.wait_for(browser.get(url, new_tab=True), timeout=15.0)
            
            # Custom wait time
            if any(s in url for s in ["myntra.com", "flipkart.com"]):
                wait_time = 6.0
            else:
                wait_time = 2.5 + random.uniform(0.5, 1.5)
                
            await page.sleep(wait_time)
            html = await page.get_content()
            
            # Check for PerimeterX block/CAPTCHA (Walmart and Target)
            is_target = "target.com" in url
            if (is_walmart or is_target) and is_perimeterx_blocked(html):
                logger.warning(f"[{pair_id}][{link_key}] PerimeterX CAPTCHA detected on attempt {attempt}/{max_attempts} — running xdotool solver")
                solved = await _solve_press_hold_xdotool(page, pair_id, link_key)
                if solved:
                    # Re-navigate to product URL now that the PX cookie is set
                    logger.info(f"[{pair_id}][{link_key}] Navigating to product URL with valid PX session...")
                    await page.get(url)
                    await page.sleep(wait_time)
                    html = await page.get_content()
                    if is_perimeterx_blocked(html):
                        logger.warning(f"[{pair_id}][{link_key}] Still blocked after xdotool solve — retrying")
                        await page.close()
                        page = None
                        if attempt < max_attempts:
                            await asyncio.sleep(5.0 + random.uniform(1.0, 3.0))
                        continue
                    # Not blocked — fall through to parse
                else:
                    await page.close()
                    page = None
                    if attempt < max_attempts:
                        delay = 6.0 + random.uniform(2.0, 4.0)
                        logger.info(f"[{pair_id}][{link_key}] Waiting {delay:.1f}s before retry...")
                        await asyncio.sleep(delay)
                    continue

            # Check for Amazon block/CAPTCHA
            if "amazon." in url and is_amazon_blocked(html):
                logger.warning(f"[{pair_id}][{link_key}] Amazon CAPTCHA/Block page detected on attempt {attempt}/{max_attempts}")
                
                # Try to locate and click "Continue Shopping" or similar button
                clicked_continue = False
                for btn_text in ["Continue Shopping", "continue shopping", "Go back to the homepage"]:
                    try:
                        btn = await page.find(btn_text, best_match=True)
                        if btn:
                            logger.info(f"[{pair_id}][{link_key}] Found click target '{btn_text}', clicking...")
                            await btn.mouse_click()
                            await page.sleep(2.0 + random.uniform(0.5, 1.0))
                            clicked_continue = True
                            break
                    except Exception as btn_err:
                        logger.debug(f"Error finding/clicking button '{btn_text}': {btn_err}")
                
                if clicked_continue:
                    # After clicking continue, re-navigate to the original URL
                    logger.info(f"[{pair_id}][{link_key}] Re-navigating to original URL: {url}")
                    await page.get(url)
                    await page.sleep(wait_time)
                    html = await page.get_content()
                
                # Check block status again
                if is_amazon_blocked(html):
                    logger.warning(f"[{pair_id}][{link_key}] Still blocked on attempt {attempt}/{max_attempts}.")
                    # Close page and try next attempt
                    await page.close()
                    page = None
                    if attempt < max_attempts:
                        delay = 4.0 + random.uniform(1.0, 3.0)
                        logger.info(f"[{pair_id}][{link_key}] Waiting {delay:.1f}s before retry...")
                        await asyncio.sleep(delay)
                    continue

            # Parse content
            is_flipkart = "flipkart.com" in url
            is_myntra = "myntra.com" in url

            if is_walmart:
                raw = parse_walmart_next_data(html)
                if not raw:
                    logger.warning(f"[{pair_id}][{link_key}] __NEXT_DATA__ not found. Falling back to HTML.")
                    raw = parse_page_html(html, url)
            elif is_target:
                try:
                    raw = parse_target_next_data(html)
                except ValueError as ve:
                    if str(ve) == "TARGET_GHOST_BLOCK":
                        logger.error(f"[{pair_id}][{link_key}] Target Ghost Block detected! Datacenter IP flagged. Target served a 404 Ghost page. Retrying...")
                        await page.close()
                        page = None
                        if attempt < max_attempts:
                            delay = 4.0 + random.uniform(1.0, 3.0)
                            logger.info(f"[{pair_id}][{link_key}] Waiting {delay:.1f}s before retry...")
                            await asyncio.sleep(delay)
                        continue
                    else:
                        raw = None
                
                if not raw:
                    raw = parse_page_html(html, url)
            elif is_flipkart:
                raw = parse_flipkart(html) or parse_page_html(html, url)
            elif is_myntra:
                raw = parse_myntra(html) or parse_page_html(html, url)
            else:
                raw = parse_page_html(html, url)

            # Extra validation for Walmart product page (must have a valid title and not be empty)
            if is_walmart:
                title = raw.get("title", "")
                if not title or any(term in title.lower() for term in ["robot or human", "verify your identity", "perimeterx", "px-captcha", "blocked", "www.walmart.com"]):
                    logger.warning(f"[{pair_id}][{link_key}] Parsed Walmart title is invalid/empty/robot: '{title}'")
                    await page.close()
                    page = None
                    if attempt < max_attempts:
                        delay = 4.0 + random.uniform(1.0, 3.0)
                        logger.info(f"[{pair_id}][{link_key}] Waiting {delay:.1f}s before retry...")
                        await asyncio.sleep(delay)
                    continue

            # Extra validation for Target product page (must have specifications)
            if is_target:
                if not raw.get("spec_table"):
                    logger.warning(f"[{pair_id}][{link_key}] Target Ghost Block detected (missing specs). Retrying...")
                    await page.close()
                    page = None
                    if attempt < max_attempts:
                        delay = 4.0 + random.uniform(1.0, 3.0)
                        logger.info(f"[{pair_id}][{link_key}] Waiting {delay:.1f}s before retry...")
                        await asyncio.sleep(delay)
                    continue

            # Extra validation for Amazon product page (must have a valid title and not be empty)
            if "amazon." in url:
                title = raw.get("title", "")
                if not title or any(term in title.lower() for term in ["captcha", "robot check", "automated access", "something went wrong"]):
                    logger.warning(f"[{pair_id}][{link_key}] Parsed Amazon title is invalid/empty/robot: '{title}'")
                    await page.close()
                    page = None
                    if attempt < max_attempts:
                        delay = 4.0 + random.uniform(1.0, 3.0)
                        logger.info(f"[{pair_id}][{link_key}] Waiting {delay:.1f}s before retry...")
                        await asyncio.sleep(delay)
                    continue

            # General validation for completely empty data (any retailer)
            if not raw.get("title") and not raw.get("spec_table") and not raw.get("image_urls") and not raw.get("price_raw"):
                logger.warning(f"[{pair_id}][{link_key}] Scraped data is completely empty. Likely blocked or failed to load.")
                await page.close()
                page = None
                if attempt < max_attempts:
                    delay = 4.0 + random.uniform(1.0, 3.0)
                    logger.info(f"[{pair_id}][{link_key}] Waiting {delay:.1f}s before retry...")
                    await asyncio.sleep(delay)
                continue

            # If we successfully parsed, close the tab and return
            await page.close()
            return raw

        except Exception as e:
            logger.error(f"[{pair_id}][{link_key}] Error during attempt {attempt}: {e}")
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
                page = None
            if attempt < max_attempts:
                await asyncio.sleep(3.0 + random.uniform(1.0, 2.0))

    raise Exception("Failed to scrape and bypass anti-bot page after max retries")


# ── Worker ────────────────────────────────────────────────────────────────────

async def worker(
    browser: uc.Browser,
    queue: asyncio.Queue,
    raw_path: Path,
    done_keys: set,
    is_walmart: bool,
    progress,
    task_id,
    url_to_raw: dict,
):
    while True:
        item = await queue.get()
        if item is None:
            break

        pair_id  = item["pair_id"]
        link_key = item["link_key"]
        url      = item["url"]
        composite_key = (pair_id, link_key)

        if composite_key in done_keys:
            queue.task_done()
            progress.advance(task_id)
            continue

        record = {
            "pair_id": pair_id, "link_key": link_key, "url": url,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "status": "ok", "raw": {},
        }

        # Check URL cache
        if url in url_to_raw:
            record["raw"] = url_to_raw[url]
            logger.info(
                f"[{pair_id}][{link_key}] ⟲ [URL Cache Hit] "
                f"specs={len(record['raw'].get('spec_table', {}))} "
                f"bullets={len(record['raw'].get('bullet_points', []))} "
                f"images={len(record['raw'].get('image_urls', []))}"
            )
            jsonl_append(raw_path, record)
            done_keys.add(composite_key)
            queue.task_done()
            progress.advance(task_id)
            continue

        try:
            raw = await scrape_url_with_stealth_and_retry(
                browser=browser,
                url=url,
                is_walmart=is_walmart,
                pair_id=pair_id,
                link_key=link_key
            )
            record["raw"] = raw
            # Update cache map
            url_to_raw[url] = raw
            source = raw.get("_source", "?")
            logger.info(
                f"[{pair_id}][{link_key}] ✓ [{source}] "
                f"specs={len(raw['spec_table'])} "
                f"bullets={len(raw['bullet_points'])} "
                f"images={len(raw['image_urls'])}"
            )
        except Exception as e:
            record["status"] = "failed"
            record["error"]  = str(e)
            logger.error(f"[{pair_id}][{link_key}] ✗ {e}")

        jsonl_append(raw_path, record)
        done_keys.add(composite_key)
        queue.task_done()
        progress.advance(task_id)



def load_proxies() -> list[str]:
    """
    Loads proxies from:
    1. Environment variable: PROXY_POOL (comma-separated)
    2. File: proxies.txt (one per line)
    Returns a list of proxy strings (e.g. ['http://ip:port', ...])
    """
    proxies = []
    # 1. Env Var
    env_pool = os.environ.get("PROXY_POOL")
    if env_pool:
        for p in env_pool.split(","):
            p = p.strip()
            if p:
                if not any(p.startswith(scheme) for scheme in ["http://", "https://", "socks4://", "socks5://"]):
                    p = f"http://{p}"
                proxies.append(p)
    
    # 2. File proxies.txt
    proxy_file = Path("proxies.txt")
    if proxy_file.exists():
        try:
            with open(proxy_file, "r") as f:
                for line in f:
                    p = line.strip()
                    if p and not p.startswith("#"):
                        if not any(p.startswith(scheme) for scheme in ["http://", "https://", "socks4://", "socks5://"]):
                            p = f"http://{p}"
                        proxies.append(p)
        except Exception as e:
            logger.warning(f"Failed to read proxies.txt: {e}")
            
    return proxies


# ── Main ──────────────────────────────────────────────────────────────────────

async def run_scraper(pairs: list[dict], workers: int = WORKERS, proxy_list: list[str] | None = None):
    done_link1 = jsonl_index_composite(RAW_LINK1, ["pair_id", "link_key"])
    done_link2 = jsonl_index_composite(RAW_LINK2, ["pair_id", "link_key"])

    items_link1 = [{"pair_id": p["pair_id"], "link_key": "link_1", "url": p["link_1_url"]} for p in pairs]
    items_link2 = [{"pair_id": p["pair_id"], "link_key": "link_2", "url": p["link_2_url"]} for p in pairs]
    total = len(items_link1) + len(items_link2)

    logger.info(f"Scraping {len(pairs)} pairs ({total} URLs) — {workers} workers")

    # Start a virtual X display so Chrome runs invisibly (no window on desktop)
    _start_virtual_display()

    # Load proxy list if not provided
    if proxy_list is None:
        proxy_list = load_proxies()

    if proxy_list:
        logger.info(f"Loaded {len(proxy_list)} proxies for rotation.")
    else:
        logger.info("No proxy list found. Operating with standard local IP.")

    # Build URL cache map from existing scraped files
    url_to_raw = {}
    for r in jsonl_read(RAW_LINK1):
        if r.get("status") == "ok" and r.get("raw") and r.get("url"):
            url_to_raw[r["url"]] = r["raw"]
    for r in jsonl_read(RAW_LINK2):
        if r.get("status") == "ok" and r.get("raw") and r.get("url"):
            url_to_raw[r["url"]] = r["raw"]

    if url_to_raw:
        logger.info(f"Loaded {len(url_to_raw)} URLs from cache.")

    # We launch a separate browser instance for each worker so they can use separate proxies
    browsers = []
    
    async def start_browser_for_worker(worker_idx: int) -> uc.Browser:
        args = [
            "--window-size=1280,800",
            "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--ozone-platform=x11",
            # NOTE: --disable-gpu is intentionally omitted — it breaks Canvas/WebGL
            # fingerprinting and is a bot signal to PerimeterX.
            f"--user-agent={_CHROME_UA}",
        ]
        if proxy_list:
            proxy = proxy_list[worker_idx % len(proxy_list)]
            args.append(f"--proxy-server={proxy}")
            logger.info(f"Worker {worker_idx} using proxy: {proxy}")

        b = await uc.start(
            headless=False,
            browser_executable_path="/usr/bin/google-chrome",
            browser_args=args,
            user_data_dir=str(CHROME_PROFILE),  # Persistent profile → reuse cookies/session
        )
        return b

    # Create browsers
    for i in range(workers):
        try:
            b = await start_browser_for_worker(i)
            browsers.append(b)
        except Exception as e:
            logger.error(f"Failed to start browser for worker {i}: {e}")

    if not browsers:
        logger.critical("No browsers started successfully. Exiting scraper.")
        return

    actual_workers = len(browsers)

    with Progress(
        SpinnerColumn(), TextColumn("[bold cyan]{task.description}"),
        BarColumn(), TaskProgressColumn()
    ) as progress:
        task_id = progress.add_task("Scraping URLs...", total=total)

        # Concurrently run both Walmart and Competitor scraping tasks by splitting workers
        if actual_workers >= 2:
            l1_workers = actual_workers // 2
            l2_workers = actual_workers - l1_workers
            
            logger.info(f"Concurrently scraping: {l1_workers} workers on Walmart (link_1) & {l2_workers} workers on Competitor (link_2)")
            
            # Warm up Walmart tab for L1 workers — a real user navigates, waits, scrolls
            for idx in range(l1_workers):
                try:
                    page = browsers[idx].main_tab
                    await page.get("https://www.walmart.com/")
                    await page.sleep(3.0 + random.uniform(0.5, 1.5))
                    # Simulate a human scroll to build a behavioral trust score
                    await page.evaluate("window.scrollBy(0, 300)")
                    await page.sleep(1.0 + random.uniform(0.3, 0.7))
                    logger.info(f"Warmup for worker {idx} complete.")
                except Exception as e:
                    logger.debug(f"Warmup warning for worker {idx}: {e}")

            queue_l1: asyncio.Queue = asyncio.Queue()
            for item in items_link1:
                await queue_l1.put(item)
            for _ in range(l1_workers):
                await queue_l1.put(None)

            queue_l2: asyncio.Queue = asyncio.Queue()
            for item in items_link2:
                await queue_l2.put(item)
            for _ in range(l2_workers):
                await queue_l2.put(None)

            tasks_l1 = [
                asyncio.create_task(
                    worker(browsers[i], queue_l1, RAW_LINK1, done_link1, True, progress, task_id, url_to_raw)
                )
                for i in range(l1_workers)
            ]
            tasks_l2 = [
                asyncio.create_task(
                    worker(browsers[i + l1_workers], queue_l2, RAW_LINK2, done_link2, False, progress, task_id, url_to_raw)
                )
                for i in range(l2_workers)
            ]
            await asyncio.gather(*(tasks_l1 + tasks_l2))
        else:
            # Sequential fallback for 1 worker
            logger.info("Running sequentially because only 1 worker/browser is available.")
            
            # Warm up Walmart — simulate a human browsing session
            try:
                page = browsers[0].main_tab
                await page.get("https://www.walmart.com/")
                await page.sleep(3.0 + random.uniform(0.5, 1.5))
                await page.evaluate("window.scrollBy(0, 300)")
                await page.sleep(1.0 + random.uniform(0.3, 0.7))
                logger.info("Warmup complete.")
            except Exception as e:
                logger.debug(f"Warmup warning: {e}")

            queue_l1: asyncio.Queue = asyncio.Queue()
            for item in items_link1:
                await queue_l1.put(item)
            await queue_l1.put(None)
            await worker(browsers[0], queue_l1, RAW_LINK1, done_link1, True, progress, task_id, url_to_raw)

            queue_l2: asyncio.Queue = asyncio.Queue()
            for item in items_link2:
                await queue_l2.put(item)
            await queue_l2.put(None)
            await worker(browsers[0], queue_l2, RAW_LINK2, done_link2, False, progress, task_id, url_to_raw)

    # Stop all browsers
    for b in browsers:
        try:
            b.stop()
        except Exception:
            pass
            
    logger.info("Scraping complete.")


async def scrape_single_url_content(url: str) -> dict:
    _start_virtual_display()  # ensure Xvfb is up; xdotool will use DISPLAY=:99

    proxy_list = load_proxies()
    # Try up to 3 random proxies sequentially if available
    if proxy_list:
        candidates = random.sample(proxy_list, min(3, len(proxy_list)))
        for idx, proxy in enumerate(candidates):
            args_with_proxy = [
                "--window-size=1280,800",
                "--no-sandbox", "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--ozone-platform=x11",
                f"--user-agent={_CHROME_UA}",
                f"--proxy-server={proxy}"
            ]
            logger.info(f"Single scrape: trying with proxy {idx+1}/{len(candidates)}: {proxy}")
            try:
                browser = await uc.start(
                    headless=False,
                    browser_executable_path="/usr/bin/google-chrome",
                    browser_args=args_with_proxy,
                    user_data_dir=str(CHROME_PROFILE),
                )
                try:
                    is_walmart = "walmart.com" in url
                    # Wait at most 75s (longer for captcha wait) for the scrape to finish
                    raw = await asyncio.wait_for(
                        scrape_url_with_stealth_and_retry(
                            browser=browser,
                            url=url,
                            is_walmart=is_walmart,
                            pair_id="single_scrape",
                            link_key="single_link",
                            max_attempts=1,
                        ),
                        timeout=180.0
                    )
                    logger.info(f"Single scrape succeeded using proxy {proxy}")
                    return raw
                except Exception as inner_e:
                    logger.warning(f"Proxy {proxy} scraping failed: {inner_e}")
                finally:
                    try:
                        browser.stop()
                    except Exception:
                        pass
            except Exception as outer_e:
                logger.warning(f"Failed to start browser with proxy {proxy}: {outer_e}")
        logger.warning("All proxy attempts failed. Falling back to direct connection...")

    # Fallback to direct connection (no proxy)
    args_direct = [
        "--window-size=1280,800",
        "--no-sandbox", "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--ozone-platform=x11",
        f"--user-agent={_CHROME_UA}",
    ]
    logger.info("Single scrape: using direct connection (no proxy)")
    browser = await uc.start(
        headless=False,
        browser_executable_path="/usr/bin/google-chrome",
        browser_args=args_direct,
        user_data_dir=str(CHROME_PROFILE),
    )
    try:
        is_walmart = "walmart.com" in url
        # Wait up to 75s for direct connection (since captcha might be solved)
        raw = await asyncio.wait_for(
            scrape_url_with_stealth_and_retry(
                browser=browser,
                url=url,
                is_walmart=is_walmart,
                pair_id="single_scrape",
                link_key="single_link",
                max_attempts=2,
            ),
            timeout=180.0
        )
        logger.info("Single scrape succeeded using direct connection")
        return raw
    finally:
        try:
            browser.stop()
        except Exception:
            pass

async def scrape_urls_concurrently(urls: list[str]) -> list[dict]:
    _start_virtual_display()  # ensure Xvfb is up

    proxy_list = load_proxies()
    browser = None
    used_proxy = None
    
    # Try up to 3 random proxies sequentially if available
    if proxy_list:
        candidates = random.sample(proxy_list, min(3, len(proxy_list)))
        for idx, proxy in enumerate(candidates):
            args_with_proxy = [
                "--window-size=1280,800",
                "--no-sandbox", "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--ozone-platform=x11",
                f"--user-agent={_CHROME_UA}",
                f"--proxy-server={proxy}"
            ]
            logger.info(f"Concurrent scrape: trying with proxy {idx+1}/{len(candidates)}: {proxy}")
            try:
                browser = await uc.start(
                    headless=False,
                    browser_executable_path="/usr/bin/google-chrome",
                    browser_args=args_with_proxy,
                    user_data_dir=str(CHROME_PROFILE),
                )
                used_proxy = proxy
                break
            except Exception as outer_e:
                logger.warning(f"Failed to start browser with proxy {proxy}: {outer_e}")
                
        if not browser:
            logger.warning("All proxy attempts failed. Falling back to direct connection...")

    # Fallback to direct connection
    if not browser:
        args_direct = [
            "--window-size=1280,800",
            "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--ozone-platform=x11",
            f"--user-agent={_CHROME_UA}",
        ]
        logger.info("Concurrent scrape: using direct connection (no proxy)")
        browser = await uc.start(
            headless=False,
            browser_executable_path="/usr/bin/google-chrome",
            browser_args=args_direct,
            user_data_dir=str(CHROME_PROFILE),
        )

    try:
        tasks = []
        for i, url in enumerate(urls):
            is_walmart = "walmart.com" in url
            task = asyncio.wait_for(
                scrape_url_with_stealth_and_retry(
                    browser=browser,
                    url=url,
                    is_walmart=is_walmart,
                    pair_id=f"concurrent_scrape_{i}",
                    link_key=f"link_{i}",
                    max_attempts=2,
                ),
                timeout=180.0
            )
            tasks.append(task)
            
        # Run all URLs concurrently in separate tabs of the same browser instance
        results = await asyncio.gather(*tasks, return_exceptions=False)
        logger.info(f"Concurrent scrape succeeded for {len(urls)} URLs")
        return list(results)
    finally:
        try:
            browser.stop()
        except Exception:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit",   type=int, default=None)
    parser.add_argument("--workers", type=int, default=WORKERS)
    args = parser.parse_args()

    # Back up existing files if we start fresh, but otherwise let it resume
    pairs = load_pairs("data.csv", limit=args.limit)
    asyncio.run(run_scraper(pairs, workers=args.workers))
