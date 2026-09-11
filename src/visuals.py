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
    # Strip a trailing "Season N" whether or not it's preceded by a dash —
    # most AniList titles use "Title Season N" with no dash (see the same
    # fix applied in tts.py's segment-matching keyword extraction).
    cleaned = re.sub(r'\s*-?\s*Season\s*\d+\s*$', '', cleaned, flags=re.IGNORECASE)
    return cleaned.strip()

# Reject images narrower than this — thumbnails/mobile-sized crops look
# noticeably blurry once stretched to fill a 1080-wide vertical Short.
MIN_ACCEPTABLE_IMAGE_WIDTH = 400

def download_image(url: str, output_path: Path, timeout: int = 8, min_width: int = MIN_ACCEPTABLE_IMAGE_WIDTH) -> bool:
    """Download image from URL, verify validity using PIL, and enforce a minimum
    resolution so low-quality thumbnails get rejected in favor of trying the
    next fallback source rather than silently ending up in the final video."""
    try:
        response = requests.get(url, timeout=timeout, headers=HEADERS)
        response.raise_for_status()
        
        with open(output_path, "wb") as f:
            f.write(response.content)
            
        with Image.open(output_path) as img:
            img.verify()
        # Re-open after verify() (which can leave the file object unusable) to
        # check actual pixel dimensions.
        with Image.open(output_path) as img:
            width, height = img.size
        if width < min_width:
            logger.warning(
                f"Rejected low-resolution image from {url} ({width}x{height}px, "
                f"below the {min_width}px minimum width) — trying next source."
            )
            output_path.unlink()
            return False

        logger.info(f"Successfully downloaded and verified cover image: {output_path.name} ({width}x{height}px)")
        return True
    except Exception as e:
        logger.warning(f"Download failed from {url}: {e}")
        if output_path.exists():
            output_path.unlink()
        return False

def fetch_fallback_jikan_cover(title: str) -> str:
    """Search title on Jikan API to get the best-available MyAnimeList cover
    image URL. The search endpoint's own images.jpg.large_image_url is
    usually only ~225x318px — noticeably soft once used as full-bleed
    artwork in a 1080px-wide Short. Jikan's dedicated /pictures endpoint for
    that title returns MAL's full image gallery at much higher resolution
    (frequently 1200px+), so try that first and only fall back to the
    smaller search-result thumbnail if the gallery lookup comes up empty."""
    search_q = clean_search_title(title)
    logger.info(f"Attempting Jikan search fallback for cover image: '{search_q}'...")
    try:
        url = f"{config.JIKAN_API_BASE_URL}/anime?q={requests.utils.quote(search_q)}&limit=1"
        res = requests.get(url, headers=HEADERS, timeout=8)
        res.raise_for_status()
        data = res.json().get("data", [])
        if not data:
            return ""

        mal_id = data[0].get("mal_id")
        fallback_url = data[0].get("images", {}).get("jpg", {}).get("large_image_url") or ""

        if mal_id:
            try:
                pics_url = f"{config.JIKAN_API_BASE_URL}/anime/{mal_id}/pictures"
                pics_res = requests.get(pics_url, headers=HEADERS, timeout=8)
                pics_res.raise_for_status()
                pictures = pics_res.json().get("data", [])
                if pictures:
                    hi_res = pictures[0].get("jpg", {}).get("large_image_url") or pictures[0].get("jpg", {}).get("image_url")
                    if hi_res:
                        return hi_res
            except Exception as e:
                logger.warning(f"Jikan high-res pictures lookup failed for '{search_q}' (mal_id={mal_id}): {e}")

        return fallback_url
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
            poster = data[0].get("attributes", {}).get("posterImage", {}) or {}
            # Prefer "original" (Kitsu's un-resized source upload) over the
            # downscaled "large" derivative for better final image quality.
            image_url = poster.get("original") or poster.get("large")
            return image_url or ""
    except Exception as e:
        logger.warning(f"Kitsu fallback lookup failed for '{search_q}': {e}")
    return ""

def compute_image_sha256(image_path: Path) -> str:
    """Compute SHA-256 content hash of image file."""
    import hashlib
    hasher = hashlib.sha256()
    with open(image_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest().lower()

def fetch_and_save_visuals(candidates: List[Dict[str, Any]]) -> List[Path]:
    """
    Downloads official cover artwork for each candidate anime.
    Attaches structured asset_rights metadata and enforces SHA-256 content-hash image deduplication
    against past video history and across candidates in the current run.
    Returns list of downloaded image file paths in order.
    """
    from src.history_manager import get_used_image_hashes, record_used_images

    downloaded_paths: List[Path] = [None] * len(candidates)
    past_used_hashes = get_used_image_hashes()
    run_used_hashes = set()
    used_records_to_save = []

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
        accepted_hash = ""
        accepted_url = ""
        last_duplicate_path = None
        last_duplicate_hash = ""
        last_duplicate_url = ""

        # Collect candidate image URLs to try: (url, source_name)
        sources_to_try = []
        if cover_url:
            sources_to_try.append((cover_url, source))
        
        jikan_url = fetch_fallback_jikan_cover(title)
        if jikan_url and jikan_url != cover_url:
            sources_to_try.append((jikan_url, "Jikan API"))

        kitsu_url = fetch_fallback_kitsu_cover(title)
        if kitsu_url and kitsu_url not in [cover_url, jikan_url]:
            sources_to_try.append((kitsu_url, "Kitsu API"))

        for url_attempt, src_attempt in sources_to_try:
            if download_image(url_attempt, target_path, timeout=8):
                img_hash = compute_image_sha256(target_path)
                if img_hash in past_used_hashes or img_hash in run_used_hashes:
                    logger.warning(
                        f"[Image Deduplication WARNING] Cover image from {src_attempt} for '{title}' is a duplicate "
                        f"of previously used artwork (SHA-256: {img_hash[:10]}...). Trying next fallback source..."
                    )
                    last_duplicate_hash = img_hash
                    last_duplicate_url = url_attempt
                    if target_path.exists():
                        try:
                            target_path.unlink()
                        except Exception:
                            pass
                else:
                    success = True
                    accepted_hash = img_hash
                    accepted_url = url_attempt
                    source = src_attempt
                    logger.info(
                        f"[Image Deduplication UNIQUE] Accepted distinct image for '{title}' (SHA-256: {img_hash[:10]}...)"
                    )
                    break

        # Fallback: If all available sources are duplicates, use the primary/least-bad option
        if not success and sources_to_try:
            logger.warning(
                f"[Image Deduplication FALLBACK] All available cover sources for '{title}' are duplicate artwork. "
                f"Using primary source as least-bad fallback option rather than failing pipeline."
            )
            fallback_url, fallback_src = sources_to_try[0]
            # Bypass the minimum-resolution gate here: this is the last-resort
            # "better than nothing" attempt after every source has already
            # either duplicated or been rejected as too low-res, so a small
            # but valid image beats having no image for the candidate at all.
            if download_image(fallback_url, target_path, timeout=8, min_width=1):
                success = True
                accepted_hash = compute_image_sha256(target_path)
                accepted_url = fallback_url
                source = fallback_src

        # Attach structured rights metadata and record hash
        if success:
            downloaded_paths[idx - 1] = target_path
            candidate["local_image_path"] = str(target_path)
            candidate["image_hash"] = accepted_hash
            run_used_hashes.add(accepted_hash)
            used_records_to_save.append({
                "hash": accepted_hash,
                "title": title,
                "source_url": accepted_url
            })

            candidate["asset_rights"] = {
                "asset_id": filename,
                "source": source,
                "source_url": accepted_url or cover_url or "",
                "image_hash": accepted_hash,
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

    if not any(downloaded_paths):
        raise RuntimeError("Phase 3 Failed: No official cover images were successfully downloaded!")

    # Every downstream sync step (video_editor.py's segment-to-image alignment, via
    # segment_timestamps' candidate_idx) assumes image_paths[i] is candidate[i]'s cover.
    # A partial download failure used to silently shrink/shift this list, which desynced
    # image switches from the narration/captions for the whole video. Fail loudly instead -
    # a skipped run is better than a video with a title's cover mismatched or missing.
    missing_titles = [c.get("title", f"anime_{i+1}") for i, c in enumerate(candidates) if downloaded_paths[i] is None]
    if missing_titles:
        raise RuntimeError(
            f"Phase 3 Failed: Could not download cover art for {len(missing_titles)}/{len(candidates)} "
            f"candidate(s): {missing_titles}. Refusing to continue with a shifted image list, since "
            f"that would desync images from the narration/captions for every title after the gap."
        )

    # Record accepted image hashes to persistent history
    try:
        record_used_images(used_records_to_save)
    except Exception as e:
        logger.warning(f"Could not record image hashes to history: {e}")

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

    logger.info(f"Visuals Sourcing Complete: Downloaded {len(downloaded_paths)}/{len(candidates)} unique images with rights metadata.")
    return downloaded_paths


def get_cached_image_paths() -> List[Path]:
    """Retrieve existing cover image paths sorted by numeric candidate index (not alphabetically -
    alphabetical sort would put cover_10 before cover_2, silently misaligning image order for any
    run with 10+ candidates)."""
    images = list(config.IMAGES_DIR.glob("cover_*"))

    def _index_key(p: Path) -> int:
        m = re.match(r"cover_(\d+)_", p.name)
        return int(m.group(1)) if m else 0

    return sorted(images, key=_index_key)

if __name__ == "__main__":
    import json
    selected_file = config.OUTPUT_DIR / "selected_titles.json"
    if selected_file.exists():
        with open(selected_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        candidates = data["candidates"] if isinstance(data, dict) and "candidates" in data else data
        paths = fetch_and_save_visuals(candidates)
        print("Downloaded image paths:", paths)
