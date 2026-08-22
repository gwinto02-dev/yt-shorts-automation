import logging
import re
import time
from pathlib import Path
from typing import List, Dict, Any
import requests
from PIL import Image

import config

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
}

def sanitize_filename(name: str) -> str:
    """Sanitize string for safe filenames."""
    return re.sub(r'[^\w\-_]', '_', name).strip('_')

def clean_search_title(name: str) -> str:
    """Clean title by removing bracketed info like (2011) or Season 4 for better API matching."""
    cleaned = re.sub(r'\s*\([^)]*\)', '', name)
    cleaned = re.sub(r'\s*-\s*Season\s*\d+', '', cleaned, flags=re.IGNORECASE)
    return cleaned.strip()

def download_image(url: str, output_path: Path, timeout: int = 8) -> bool:
    """Download image from URL and verify validity using PIL."""
    try:
        response = requests.get(url, timeout=timeout, headers=HEADERS)
        response.raise_for_status()
        
        with open(output_path, "wb") as f:
            f.write(response.content)
            
        with Image.open(output_path) as img:
            img.verify()
            
        logger.info(f"Successfully downloaded and verified cover image: {output_path.name}")
        return True
    except Exception as e:
        logger.warning(f"Download failed from {url}: {e}")
        if output_path.exists():
            output_path.unlink()
        return False

def fetch_fallback_jikan_cover(title: str) -> str:
    """Search title on Jikan API to get official MyAnimeList cover image URL."""
    search_q = clean_search_title(title)
    logger.info(f"Attempting Jikan search fallback for cover image: '{search_q}'...")
    try:
        url = f"{config.JIKAN_API_BASE_URL}/anime?q={requests.utils.quote(search_q)}&limit=1"
        res = requests.get(url, headers=HEADERS, timeout=8)
        res.raise_for_status()
        data = res.json().get("data", [])
        if data:
            image_url = data[0].get("images", {}).get("jpg", {}).get("large_image_url")
            return image_url or ""
    except Exception as e:
        logger.warning(f"Jikan fallback lookup failed for '{search_q}': {e}")
    return ""

def fetch_fallback_kitsu_cover(title: str) -> str:
    """Search title on Kitsu API as secondary free fallback."""
    search_q = clean_search_title(title)
    logger.info(f"Attempting Kitsu search fallback for cover image: '{search_q}'...")
    try:
        url = f"https://kitsu.io/api/edge/anime?filter[text]={requests.utils.quote(search_q)}&page[limit]=1"
        res = requests.get(url, headers=HEADERS, timeout=8)
        res.raise_for_status()
        data = res.json().get("data", [])
        if data:
            image_url = data[0].get("attributes", {}).get("posterImage", {}).get("large")
            return image_url or ""
    except Exception as e:
        logger.warning(f"Kitsu fallback lookup failed for '{search_q}': {e}")
    return ""

def fetch_and_save_visuals(candidates: List[Dict[str, Any]]) -> List[Path]:
    """
    Downloads official cover artwork for each candidate anime.
    Attaches structured asset_rights metadata — source/type only; does NOT
    make legal fair-use determinations or assert commercial licence clearance.
    Returns list of downloaded image file paths in order.
    """
    downloaded_paths = []

    # Clean previous images in assets/images/
    for old_file in config.IMAGES_DIR.glob("cover_*"):
        try:
            old_file.unlink()
        except Exception:
            pass

    for idx, candidate in enumerate(candidates, 1):
        title = candidate.get("title", f"anime_{idx}")
        clean_title = sanitize_filename(title)
        cover_url = candidate.get("cover_image")
        source = candidate.get("source", "API")

        ext = "jpg"
        if cover_url and cover_url.lower().endswith(".png"):
            ext = "png"

        filename = f"cover_{idx}_{clean_title}.{ext}"
        target_path = config.IMAGES_DIR / filename

        logger.info(f"Downloading cover for #{idx}: {title}")
        success = False

        # 1. Try primary URL
        if cover_url:
            success = download_image(cover_url, target_path, timeout=8)

        # 2. Try Jikan fallback if primary failed
        if not success:
            logger.info(f"Primary image source failed for '{title}'. Trying Jikan API...")
            fallback_url = fetch_fallback_jikan_cover(title)
            if fallback_url:
                success = download_image(fallback_url, target_path, timeout=10)

        # 3. Try Kitsu fallback if still failed
        if not success:
            logger.info(f"Jikan fallback failed for '{title}'. Trying Kitsu API...")
            fallback_url_2 = fetch_fallback_kitsu_cover(title)
            if fallback_url_2:
                success = download_image(fallback_url_2, target_path, timeout=10)

        # Attach structured rights metadata.
        # Source/type describe what the asset IS.
        # license_status and commercial_use_verified describe what we actually KNOW.
        # We do NOT claim fair use or any commercial licence for promotional API artwork.
        if success:
            downloaded_paths.append(target_path)
            candidate["local_image_path"] = str(target_path)
            candidate["asset_rights"] = {
                "asset_id": filename,
                "source": source,
                "source_url": cover_url or "",
                "asset_type": "Official Promotional Artwork",
                "license_status": "LICENSE_UNKNOWN",
                "commercial_use_verified": False,
                "risk_level": "REVIEW",
                "note": (
                    f"Downloaded from {source} API. Official promotional artwork — "
                    "licence status unknown. Requires human review before commercial use."
                )
            }
        else:
            candidate["asset_rights"] = {
                "asset_id": filename,
                "source": source,
                "source_url": cover_url or "",
                "asset_type": "Unknown",
                "license_status": "LICENSE_RESTRICTED",
                "commercial_use_verified": False,
                "risk_level": "HIGH",
                "note": "Download failed. Asset unavailable — do not use."
            }
            logger.error(f"Could not download artwork for '{title}' after primary and fallback attempts.")

    if not downloaded_paths:
        raise RuntimeError("Phase 3 Failed: No official cover images were successfully downloaded!")

    # Write human-readable manifest for review
    manifest_path = config.OUTPUT_DIR / "asset_rights_manifest.json"
    import json
    manifest = [c.get("asset_rights", {}) for c in candidates]
    try:
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        logger.info(f"Asset rights manifest written to {manifest_path}")
    except Exception as e:
        logger.warning(f"Could not write asset_rights_manifest.json: {e}")

    logger.info(f"Visuals Sourcing Complete: Downloaded {len(downloaded_paths)}/{len(candidates)} images with rights metadata.")
    return downloaded_paths


def get_cached_image_paths() -> List[Path]:
    """Retrieve existing cover image paths sorted by index."""
    images = sorted(list(config.IMAGES_DIR.glob("cover_*")))
    return images

if __name__ == "__main__":
    import json
    selected_file = config.OUTPUT_DIR / "selected_titles.json"
    if selected_file.exists():
        with open(selected_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        candidates = data["candidates"] if isinstance(data, dict) and "candidates" in data else data
        paths = fetch_and_save_visuals(candidates)
        print("Downloaded image paths:", paths)
