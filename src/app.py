import asyncio
import os
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from loguru import logger

# Import step modules
from src.step0_scraper import scrape_single_url_content, scrape_urls_concurrently
from src.step1_extractor import extract_features
from src.step2_matcher import compute_diff
from src.step3_classifier import classify

app = FastAPI(title="Antigravity Product Matcher")

class CompareRequest(BaseModel):
    link_1_url: str
    link_2_url: str

@app.get("/", response_class=HTMLResponse)
async def get_index():
    index_path = Path(__file__).parent / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    with open(index_path, "r", encoding="utf-8") as f:
        return f.read()

@app.post("/api/compare")
async def api_compare(req: CompareRequest):
    url1 = req.link_1_url.strip()
    url2 = req.link_2_url.strip()
    if not url1 or not url2:
        raise HTTPException(status_code=400, detail="Both Link 1 and Link 2 URLs are required.")

    logger.info(f"Dynamic compare request received:\nURL 1: {url1}\nURL 2: {url2}")

    # 1. Scrape concurrently
    try:
        results = await scrape_urls_concurrently([url1, url2])
        raw1, raw2 = results[0], results[1]
    except Exception as e:
        logger.error(f"Scraping failed: {e}")
        raise HTTPException(status_code=500, detail=f"Scraping failed: {str(e)}")

    if not raw1 or not raw2:
        raise HTTPException(status_code=500, detail="Could not retrieve or parse product data from one or both URLs.")

    # 2. Extract features
    try:
        rec1 = {"pair_id": "web_run", "link_key": "link_1", "url": url1, "status": "ok", "raw": raw1}
        rec2 = {"pair_id": "web_run", "link_key": "link_2", "url": url2, "status": "ok", "raw": raw2}
        
        loop = asyncio.get_running_loop()
        feat1, feat2 = await asyncio.gather(
            loop.run_in_executor(None, extract_features, rec1),
            loop.run_in_executor(None, extract_features, rec2)
        )
    except Exception as e:
        logger.error(f"Feature extraction failed: {e}")
        raise HTTPException(status_code=500, detail=f"Feature extraction failed: {str(e)}")

    # 3. Compute match difference
    try:
        diff = compute_diff(feat1, feat2)
    except Exception as e:
        logger.error(f"Matching failed: {e}")
        raise HTTPException(status_code=500, detail=f"Matching failed: {str(e)}")

    # 4. Classify relationship
    try:
        result = classify(diff)
    except Exception as e:
        logger.error(f"Classification failed: {e}")
        raise HTTPException(status_code=500, detail=f"Classification failed: {str(e)}")

    # Construct clean result payload
    return {
        "verdict": result,
        "product_1": {
            "title": feat1.get("title") or raw1.get("title", ""),
            "brand": feat1.get("brand") or raw1.get("brand", ""),
            "image": feat1.get("image_urls")[0] if feat1.get("image_urls") else None,
            "price": feat1.get("price", "N/A"),
            "features": feat1.get("features_normalized", {}),
            "raw_features": feat1.get("features_raw", {})
        },
        "product_2": {
            "title": feat2.get("title") or raw2.get("title", ""),
            "brand": feat2.get("brand") or raw2.get("brand", ""),
            "image": feat2.get("image_urls")[0] if feat2.get("image_urls") else None,
            "price": feat2.get("price", "N/A"),
            "features": feat2.get("features_normalized", {}),
            "raw_features": feat2.get("features_raw", {})
        },
        "comparison": {
            "title_similarity": diff.get("title_similarity_pct"),
            "image_similarity": diff.get("image_similarity_pct"),
            "total_features_compared": diff.get("total_features_compared"),
            "feature_diff": diff.get("feature_diff"),
            "matched_keys": diff.get("matched_keys"),
            "mismatched_keys": diff.get("mismatched_keys")
        }
    }
