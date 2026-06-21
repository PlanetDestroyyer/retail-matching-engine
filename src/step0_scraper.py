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

from utils import get_logger, jsonl_append, jsonl_index_composite, load_pairs

logger = get_logger("step0_scraper")

RAW_LINK1 = Path("data/raw/link1_raw.jsonl")
RAW_LINK2 = Path("data/raw/link2_raw.jsonl")
WORKERS   = 2


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


# Start virtual display immediately on module import so DISPLAY is set
# before any Chrome process is ever spawned.
_start_virtual_display()


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
        data = json.loads(script.string)
        queries = data.get("props", {}).get("dehydratedState", {}).get("queries", [])
        product = None
        for q in queries:
            prod = q.get("state", {}).get("data", {}).get("data", {}).get("product", {})
            if prod:
                product = prod
                break
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


# ── Worker ────────────────────────────────────────────────────────────────────

async def worker(
    browser: uc.Browser,
    queue: asyncio.Queue,
    raw_path: Path,
    done_keys: set,
    is_walmart: bool,
    progress,
    task_id,
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

        page = None
        try:
            # Navigate to the page
            page = await browser.get(url, new_tab=True)
            # Short dynamic delay for loading and stealth
            await page.sleep(2.5 + random.uniform(0.5, 1.5))
            
            html = await page.get_content()
            await page.close()
            page = None
            
            if "target.com" in url:
                raw = parse_target_next_data(html)
                if not raw:
                    raw = parse_page_html(html, url)
            elif is_walmart:
                # Try __NEXT_DATA__ first
                raw = parse_walmart_next_data(html)
                if not raw:
                    logger.warning(f"[{pair_id}][{link_key}] __NEXT_DATA__ not found. Falling back to HTML.")
                    raw = parse_page_html(html, url)
            else:
                raw = parse_page_html(html, url)

            record["raw"] = raw
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
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass

        jsonl_append(raw_path, record)
        done_keys.add(composite_key)
        queue.task_done()
        progress.advance(task_id)


# ── Main ──────────────────────────────────────────────────────────────────────

async def run_scraper(pairs: list[dict], workers: int = WORKERS):
    done_link1 = jsonl_index_composite(RAW_LINK1, ["pair_id", "link_key"])
    done_link2 = jsonl_index_composite(RAW_LINK2, ["pair_id", "link_key"])

    items_link1 = [{"pair_id": p["pair_id"], "link_key": "link_1", "url": p["link_1_url"]} for p in pairs]
    items_link2 = [{"pair_id": p["pair_id"], "link_key": "link_2", "url": p["link_2_url"]} for p in pairs]
    total = len(items_link1) + len(items_link2)

    logger.info(f"Scraping {len(pairs)} pairs ({total} URLs) — {workers} workers")

    # Start a virtual X display so Chrome runs invisibly (no window on desktop)
    _start_virtual_display()

    browser = await uc.start(
        headless=False,
        browser_executable_path="/usr/bin/google-chrome",
        browser_args=[
            "--window-size=1280,800",
            "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-dev-shm-usage", "--disable-gpu",
        ],
    )

    with Progress(
        SpinnerColumn(), TextColumn("[bold cyan]{task.description}"),
        BarColumn(), TaskProgressColumn()
    ) as progress:
        task_id = progress.add_task("Scraping URLs...", total=total)

        # 1. Scrape Link 1 (Walmart)
        logger.info("Scraping Walmart (link_1) product pages...")
        # Warmup on default tab
        try:
            page = browser.main_tab
            await page.get("https://www.walmart.com/")
            await page.sleep(2.0)
        except Exception as e:
            logger.debug(f"Warmup warning: {e}")

        queue_l1: asyncio.Queue = asyncio.Queue()
        for item in items_link1:
            await queue_l1.put(item)
        for _ in range(workers):
            await queue_l1.put(None)

        tasks_l1 = [
            asyncio.create_task(
                worker(browser, queue_l1, RAW_LINK1, done_link1, True, progress, task_id)
            )
            for _ in range(workers)
        ]
        await asyncio.gather(*tasks_l1)

        # 2. Scrape Link 2 (Competitor sites)
        logger.info("Scraping Competitor (link_2) product pages...")
        queue_l2: asyncio.Queue = asyncio.Queue()
        for item in items_link2:
            await queue_l2.put(item)
        for _ in range(workers):
            await queue_l2.put(None)

        tasks_l2 = [
            asyncio.create_task(
                worker(browser, queue_l2, RAW_LINK2, done_link2, False, progress, task_id)
            )
            for _ in range(workers)
        ]
        await asyncio.gather(*tasks_l2)

    browser.stop()
    logger.info("Scraping complete.")


async def scrape_single_url_content(url: str) -> dict:
    _start_virtual_display()

    browser = await uc.start(
        headless=False,
        browser_executable_path="/usr/bin/google-chrome",
        browser_args=[
            "--window-size=1280,800",
            "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-dev-shm-usage", "--disable-gpu",
        ],
    )
    page = None
    try:
        page = await browser.get(url, new_tab=True)
        # Indian e-commerce sites (Myntra/Flipkart) are heavily JS-rendered; wait longer
        wait_time = 6.0 if any(s in url for s in ["myntra.com", "flipkart.com"]) else 3.0
        await page.sleep(wait_time)
        html = await page.get_content()

        is_walmart   = "walmart.com"   in url
        is_target    = "target.com"    in url
        is_flipkart  = "flipkart.com"  in url
        is_myntra    = "myntra.com"    in url

        if is_walmart:
            raw = parse_walmart_next_data(html) or parse_page_html(html, url)
        elif is_target:
            raw = parse_target_next_data(html) or parse_page_html(html, url)
        elif is_flipkart:
            raw = parse_flipkart(html) or parse_page_html(html, url)
        elif is_myntra:
            raw = parse_myntra(html) or parse_page_html(html, url)
        else:
            raw = parse_page_html(html, url)

        return raw
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
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
