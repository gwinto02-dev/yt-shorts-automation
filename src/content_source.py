import json
import logging
import random
import re
from pathlib import Path
from typing import List, Dict, Any, Tuple

import requests
import config
from src.history_manager import is_concept_allowed_by_history, record_concept_usage, is_anime_title_allowed_by_history
from src.popularity_filter import can_qualify_as_hidden_gem, is_mainstream_anime

logger = logging.getLogger(__name__)

# Available Shorts Concept Types
CONCEPT_TYPES = {
    "top_recommendations": {
        "name": "Top Recommendations",
        "tagline": "Top Anime You Need to Watch Right Now",
        "description": "High energy recommendation list of top trending & acclaimed titles."
    },
    "hidden_gems": {
        "name": "Underrated Trio",
        "tagline": "Underrated Anime Gems You've Been Sleeping On",
        "description": "3 critically acclaimed anime strictly below mainstream popularity floor."
    },
    "genre_spotlight": {
        "name": "Genre-Diverse Trio",
        "tagline": "Peak Anime Across 3 Completely Distinct Genres",
        "description": "3 top tier anime titles with zero primary genre overlap."
    },
    "upcoming_spotlight": {
        "name": "Upcoming Trio",
        "tagline": "Most Anticipated Anime Airing Soon",
        "description": "3 highly anticipated unreleased or upcoming anime titles."
    },
    "character_spotlight": {
        "name": "Character & Hero Spotlight",
        "tagline": "Most Badass Anime Characters & Iconic Leads",
        "description": "Focus on anime featuring iconic MCs and legendary character arcs."
    },
    "anime_comparison": {
        "name": "Anime Head-to-Head & Matchup",
        "tagline": "Battle of the Masterpieces: Which Should You Watch?",
        "description": "Direct comparison of powerhouse anime in similar genres."
    }
}

ANILIST_TRENDING_QUERY = """
query ($page: Int, $perPage: Int) {
  Page(page: $page, perPage: $perPage) {
    media(type: ANIME, sort: [TRENDING_DESC, POPULARITY_DESC], status_in: [RELEASING, FINISHED]) {
      id
      title {
        romaji
        english
      }
      coverImage {
        extraLarge
        large
      }
      genres
      averageScore
      popularity
      trending
      description(asHtml: false)
      seasonYear
      status
    }
  }
}
"""

ANILIST_UPCOMING_QUERY = """
query ($page: Int, $perPage: Int) {
  Page(page: $page, perPage: $perPage) {
    media(type: ANIME, sort: [POPULARITY_DESC, TRENDING_DESC], status_in: [NOT_YET_RELEASED]) {
      id
      title {
        romaji
        english
      }
      coverImage {
        extraLarge
        large
      }
      genres
      averageScore
      popularity
      trending
      description(asHtml: false)
      seasonYear
      status
    }
  }
}
"""

def fetch_local_trend_data() -> List[Dict[str, Any]]:
    """Look for local trend JSON files produced by Daily Anime Buzz Tracker."""
    data_dir = config.DATA_DIR
    processed_dir = data_dir / "processed"
    search_paths = []
    
    if processed_dir.exists():
        search_paths.extend(list(processed_dir.glob("normalized_anime_*.json")))
    if data_dir.exists():
        search_paths.extend(list(data_dir.glob("daily_report_*.json")))
        
    if not search_paths:
        return []

    latest_file = max(search_paths, key=lambda p: p.stat().st_mtime)
    logger.info(f"Loading local trend data from {latest_file}")
    try:
        with open(latest_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            elif isinstance(data, dict) and "anime" in data:
                return data["anime"]
    except Exception as e:
        logger.warning(f"Failed to parse local trend data from {latest_file}: {e}")
        
    return []

def fetch_anilist_trending(count: int = 50, page: int = 1) -> List[Dict[str, Any]]:
    """Fetch trending anime list directly from AniList GraphQL API with expanded pool size."""
    logger.info(f"Fetching trending anime from AniList GraphQL API (page={page}, count={count})...")
    variables = {"page": page, "perPage": count}
    try:
        response = requests.post(
            config.ANILIST_GRAPHQL_URL,
            json={"query": ANILIST_TRENDING_QUERY, "variables": variables},
            timeout=10
        )
        response.raise_for_status()
        res_data = response.json()
        media_list = res_data.get("data", {}).get("Page", {}).get("media", [])
        
        normalized = []
        for item in media_list:
            title_eng = item.get("title", {}).get("english") or item.get("title", {}).get("romaji")
            normalized.append({
                "id": item.get("id"),
                "title": title_eng,
                "title_romaji": item.get("title", {}).get("romaji"),
                "cover_image": item.get("coverImage", {}).get("extraLarge") or item.get("coverImage", {}).get("large"),
                "genres": item.get("genres", []),
                "average_score": item.get("averageScore", 0) / 10.0 if item.get("averageScore") else 0.0,
                "popularity": item.get("popularity", 0),
                "trending_score": item.get("trending", 0),
                "synopsis": (item.get("description") or "").replace("<br>", "\n").replace("<i>", "").replace("</i>", ""),
                "status": item.get("status", "FINISHED"),
                "seasonYear": item.get("seasonYear"),
                "source": "AniList"
            })
        return normalized
    except Exception as e:
        logger.error(f"Error fetching from AniList API: {e}")
        return []

def fetch_anilist_upcoming(count: int = 50, page: int = 1) -> List[Dict[str, Any]]:
    """Fetch upcoming unreleased anime list directly from AniList GraphQL API with expanded pool size."""
    logger.info(f"Fetching upcoming anime from AniList GraphQL API (page={page}, count={count})...")
    variables = {"page": page, "perPage": count}
    try:
        response = requests.post(
            config.ANILIST_GRAPHQL_URL,
            json={"query": ANILIST_UPCOMING_QUERY, "variables": variables},
            timeout=10
        )
        response.raise_for_status()
        res_data = response.json()
        media_list = res_data.get("data", {}).get("Page", {}).get("media", [])
        
        normalized = []
        for item in media_list:
            title_eng = item.get("title", {}).get("english") or item.get("title", {}).get("romaji")
            normalized.append({
                "id": item.get("id"),
                "title": title_eng,
                "title_romaji": item.get("title", {}).get("romaji"),
                "cover_image": item.get("coverImage", {}).get("extraLarge") or item.get("coverImage", {}).get("large"),
                "genres": item.get("genres", []),
                "average_score": item.get("averageScore", 0) / 10.0 if item.get("averageScore") else 0.0,
                "popularity": item.get("popularity", 0),
                "trending_score": item.get("trending", 0),
                "synopsis": (item.get("description") or "").replace("<br>", "\n").replace("<i>", "").replace("</i>", ""),
                "status": item.get("status", "NOT_YET_RELEASED"),
                "seasonYear": item.get("seasonYear") or 2026,
                "is_upcoming": True,
                "source": "AniList"
            })
        return normalized
    except Exception as e:
        logger.error(f"Error fetching upcoming anime from AniList API: {e}")
        return []

def fetch_jikan_top(count: int = 50) -> List[Dict[str, Any]]:
    """Fallback: Fetch top anime list from Jikan v4 REST API."""
    logger.info(f"Fetching top anime from Jikan REST API (count={count})...")
    url = f"{config.JIKAN_API_BASE_URL}/top/anime?limit={count}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        res_data = response.json()
        data_list = res_data.get("data", [])
        
        normalized = []
        for item in data_list:
            normalized.append({
                "id": item.get("mal_id"),
                "title": item.get("title_english") or item.get("title"),
                "title_romaji": item.get("title"),
                "cover_image": item.get("images", {}).get("jpg", {}).get("large_image_url"),
                "genres": [g.get("name") for g in item.get("genres", [])],
                "average_score": item.get("score", 0.0),
                "popularity": item.get("popularity", 0),
                "trending_score": item.get("members", 0),
                "synopsis": item.get("synopsis", ""),
                "status": item.get("status", "FINISHED"),
                "seasonYear": item.get("year"),
                "source": "Jikan"
            })
        return normalized
    except Exception as e:
        logger.error(f"Error fetching from Jikan API: {e}")
        return []

def select_today_concept() -> Tuple[str, Dict[str, Any]]:
    """
    Enforces 5-day cooldown rule: picks a concept type not used in the last 5 days.
    Returns (concept_key, concept_details_dict).
    """
    available_keys = list(CONCEPT_TYPES.keys())
    allowed_keys = [k for k in available_keys if is_concept_allowed_by_history(k, days=config.CONCEPT_COOLDOWN_DAYS)]
    
    if not allowed_keys:
        logger.warning("All concept types used in last 5 days! Resetting pool to all concept types.")
        allowed_keys = available_keys

    selected_key = random.choice(allowed_keys)
    concept_info = CONCEPT_TYPES[selected_key]
    logger.info(f"[Concept Selection] Selected concept: '{selected_key}' ({concept_info['name']}) [Allowed by 5-day rule]")
    
    record_concept_usage(selected_key)
    return selected_key, concept_info

def select_candidate_titles(num_candidates: int = 3, concept_key: str = None) -> Tuple[List[Dict[str, Any]], str, Dict[str, Any]]:
    """
    Selects candidate titles tailored to today's Short concept type.
    Enforces 30-day anime title cooldown filtering and search pool expansion.
    """
    if not concept_key:
        concept_key, concept_info = select_today_concept()
    else:
        concept_info = CONCEPT_TYPES.get(concept_key, CONCEPT_TYPES["top_recommendations"])

    # Fetch expanded pool based on concept mode (50-100 titles)
    if concept_key == "upcoming_spotlight":
        candidates = fetch_anilist_upcoming(50)
    else:
        candidates = fetch_local_trend_data()
        if not candidates or len(candidates) < 15:
            anilist_pool = fetch_anilist_trending(50, page=1)
            # Combine local and AniList
            existing_ids = {c.get("id") for c in candidates}
            for item in anilist_pool:
                if item.get("id") not in existing_ids:
                    candidates.append(item)
                    existing_ids.add(item.get("id"))
        if not candidates:
            candidates = fetch_jikan_top(50)

    if not candidates:
        raise RuntimeError("Failed to retrieve anime candidate data from API or local files!")

    valid_candidates = [c for c in candidates if c.get("title") and c.get("cover_image")]

    # Apply 30-day Anime Title Cooldown Filter
    uncooldowned_candidates = []
    excluded_candidates = []

    for c in valid_candidates:
        allowed, reason = is_anime_title_allowed_by_history(c["title"], c.get("id"), days=config.ANIME_TITLE_COOLDOWN_DAYS)
        if allowed:
            uncooldowned_candidates.append(c)
        else:
            excluded_candidates.append({"title": c["title"], "id": c.get("id"), "reason": reason})

    logger.info("=" * 60)
    logger.info(f"[Title Cooldown Audit] {len(uncooldowned_candidates)} titles available, {len(excluded_candidates)} excluded by 30-day cooldown:")
    for ex in excluded_candidates[:10]:  # Log first 10 excluded
        logger.info(f"  - EXCLUDED: '{ex['title']}' -> Reason: {ex['reason']}")
    logger.info("=" * 60)

    # Search pool expansion if uncooldowned pool is too small
    if len(uncooldowned_candidates) < num_candidates:
        logger.warning(f"Uncooldowned pool low ({len(uncooldowned_candidates)} titles). Expanding AniList search pool (Page 2 & Jikan)...")
        extra_candidates = []
        if concept_key == "upcoming_spotlight":
            extra_candidates = fetch_anilist_upcoming(50, page=2)
        else:
            extra_candidates = fetch_anilist_trending(50, page=2) + fetch_jikan_top(50)

        seen_in_uncooldowned = {c["id"] for c in uncooldowned_candidates}
        for c in extra_candidates:
            if c.get("id") in seen_in_uncooldowned or not c.get("title") or not c.get("cover_image"):
                continue
            allowed, reason = is_anime_title_allowed_by_history(c["title"], c.get("id"), days=config.ANIME_TITLE_COOLDOWN_DAYS)
            if allowed:
                uncooldowned_candidates.append(c)
                seen_in_uncooldowned.add(c["id"])
            else:
                excluded_candidates.append({"title": c["title"], "id": c.get("id"), "reason": reason})

    # If still empty/insufficient, fall back to valid_candidates with warning as absolute last resort
    if len(uncooldowned_candidates) < num_candidates:
        logger.warning(f"LAST RESORT FALLBACK: Uncooldowned pool exhausted even after expansion. Repeating titles to satisfy selection count.")
        selection_pool = valid_candidates
    else:
        selection_pool = uncooldowned_candidates

    selected: List[Dict[str, Any]] = []
    seen_ids = set()

    # ==================== MODE 1: GENRE-DIVERSE TRIO ====================
    if concept_key == "genre_spotlight":
        used_genres = set()
        sorted_candidates = sorted(selection_pool, key=lambda x: x.get("average_score", 0), reverse=True)
        
        for c in sorted_candidates:
            if len(selected) >= num_candidates:
                break
            if c["id"] in seen_ids:
                continue
            genres = c.get("genres", [])
            primary_genre = genres[0] if genres else "General"
            
            # Ensure no primary genre overlap with selected picks
            if primary_genre not in used_genres:
                c["selection_category"] = "Genre-Diverse Pick"
                c["selection_reasoning"] = f"Primary Genre: '{primary_genre}' (Zero genre overlap with other picks in trio)."
                c["excluded_in_run"] = excluded_candidates
                selected.append(c)
                seen_ids.add(c["id"])
                used_genres.add(primary_genre)

        if len(selected) < num_candidates:
            raise ValueError(
                f"Genre-Diverse Trio criteria failed: Found only {len(selected)}/{num_candidates} titles with distinct primary genres in candidate pool!"
            )

    # ==================== MODE 2: UNDERRATED TRIO ====================
    elif concept_key == "hidden_gems":
        sorted_candidates = sorted(selection_pool, key=lambda x: x.get("average_score", 0), reverse=True)
        
        for c in sorted_candidates:
            if len(selected) >= num_candidates:
                break
            if c["id"] in seen_ids:
                continue
            qualifies, reasoning = can_qualify_as_hidden_gem(c)
            if qualifies:
                c["selection_category"] = "Underrated Hidden Gem"
                c["selection_reasoning"] = reasoning
                c["excluded_in_run"] = excluded_candidates
                selected.append(c)
                seen_ids.add(c["id"])
            else:
                logger.info(f"[ContentSource EXCLUDE] '{c['title']}' did not qualify for Underrated Trio: {reasoning}")

        if len(selected) < num_candidates:
            raise ValueError(
                f"Underrated Trio criteria failed: Found only {len(selected)}/{num_candidates} qualifying underrated titles strictly below popularity floor!"
            )

    # ==================== MODE 3: UPCOMING TRIO ====================
    elif concept_key == "upcoming_spotlight":
        for c in selection_pool:
            if len(selected) >= num_candidates:
                break
            if c["id"] in seen_ids:
                continue
            status = c.get("status", "")
            year = c.get("seasonYear") or 2026
            is_upcoming = c.get("is_upcoming") or status == "NOT_YET_RELEASED" or (isinstance(year, int) and year >= 2026)
            
            if is_upcoming:
                c["selection_category"] = "Upcoming Hype Pick"
                c["selection_reasoning"] = f"Upcoming Title (Release Status: '{status or 'NOT_YET_RELEASED'}', Year: {year})."
                c["excluded_in_run"] = excluded_candidates
                selected.append(c)
                seen_ids.add(c["id"])

        if len(selected) < num_candidates:
            raise ValueError(
                f"Upcoming Trio criteria failed: Found only {len(selected)}/{num_candidates} unreleased/upcoming titles in candidate pool!"
            )

    # ==================== OTHER CONCEPTS ====================
    elif concept_key == "anime_comparison":
        sorted_candidates = sorted(selection_pool, key=lambda x: x.get("average_score", 0), reverse=True)
        for c in sorted_candidates[:num_candidates]:
            if c["id"] not in seen_ids:
                c["selection_category"] = "Matchup Contender"
                c["selection_reasoning"] = f"Top tier powerhouse contender (Score: {c.get('average_score', 'N/A')}/10)."
                c["excluded_in_run"] = excluded_candidates
                selected.append(c)
                seen_ids.add(c["id"])
    else:
        # Default balanced mix: 1 top trending + 2 top rated
        sorted_by_trending = sorted(selection_pool, key=lambda x: x.get("trending_score", 0), reverse=True)
        sorted_by_score = sorted(selection_pool, key=lambda x: x.get("average_score", 0), reverse=True)
        
        for c in sorted_by_trending:
            if c["id"] not in seen_ids:
                c["selection_category"] = "Rising Trend"
                c["selection_reasoning"] = f"Top trending title with high current buzz index (Score: {c.get('average_score', 'N/A')}/10)."
                c["excluded_in_run"] = excluded_candidates
                selected.append(c)
                seen_ids.add(c["id"])
                break

        for c in sorted_by_score:
            if len(selected) >= num_candidates:
                break
            if c["id"] not in seen_ids:
                c["selection_category"] = "Must-Watch Masterpiece"
                c["selection_reasoning"] = f"Peak story and rating ({c.get('average_score', 'N/A')}/10)."
                c["excluded_in_run"] = excluded_candidates
                selected.append(c)
                seen_ids.add(c["id"])

    # Attach excluded candidates list to the first candidate for global reference
    if selected:
        selected[0]["all_excluded_candidates"] = excluded_candidates

    logger.info("=" * 60)
    logger.info(f"SELECTED {len(selected)} ANIME CANDIDATES FOR MODE '{concept_info['name']}':")
    for idx, item in enumerate(selected, 1):
        logger.info(f"  {idx}. [{item['selection_category']}] {item['title']} -> Reasoning: {item.get('selection_reasoning')}")
    logger.info("=" * 60)

    return selected, concept_key, concept_info

if __name__ == "__main__":
    titles, c_key, c_info = select_candidate_titles(3)
    output_path = config.OUTPUT_DIR / "selected_titles.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({"candidates": titles, "concept_key": c_key, "concept_info": c_info}, f, indent=2)
    logger.info(f"Saved selected candidates & concept to {output_path}")
