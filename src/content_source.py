import json
import logging
import random
import re
from pathlib import Path
from typing import List, Dict, Any, Tuple

import requests
import config
from src.history_manager import (
    is_concept_allowed_by_history,
    get_recent_concept_angles,
    record_concept_usage,
    is_anime_title_allowed_by_history,
    record_anime_titles_usage
)
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

# Internal Angle Variants per Concept Type to Prevent Formulaic Repetition
CONCEPT_ANGLES = {
    "hidden_gems": {
        "OVERLOOKED_REASON": {
            "key": "OVERLOOKED_REASON",
            "label": "Why These Got Overlooked",
            "instruction": "Focus on the real-world reasons these series flew under the radar when they aired (such as stacked seasonal competition, minimal Western marketing, or obscure licensing/streaming platforms)."
        },
        "STANDOUT_ELEMENT": {
            "key": "STANDOUT_ELEMENT",
            "label": "What Makes Them Worth Watching Despite Low Visibility",
            "instruction": "Focus heavily on the single standout element that elevates each series above mainstream filler — whether it's jaw-dropping animation craft, an unbeatable plot twist, or a uniquely written protagonist."
        },
        "MAINSTREAM_CONTRAST": {
            "key": "MAINSTREAM_CONTRAST",
            "label": "How These Compare to What's Popular Instead",
            "instruction": "Frame these picks by contrasting them with typical mainstream genre tropes — explain how these shows subvert cliches and offer a far richer experience than standard popular hits (without naming/reviewing specific mainstream titles)."
        },
        "INSIDER_FACT": {
            "key": "INSIDER_FACT",
            "label": "A Specific Detail Insider Fans Know",
            "instruction": "Frame the recommendations around concrete insider details — such as veteran animation staff who left major studios to work on them, legendary manga origins, or dedicated passion-project production histories."
        }
    },
    "top_recommendations": {
        "UNMATCHED_PAYOFF": {
            "key": "UNMATCHED_PAYOFF",
            "label": "Unmatched Narrative Payoff",
            "instruction": "Focus on why these titles deliver unmatched storytelling payoffs and zero wasted episodes for viewers seeking peak narrative quality."
        },
        "ANIMATION_CRAFT": {
            "key": "ANIMATION_CRAFT",
            "label": "Visual & Audio Craft Benchmark",
            "instruction": "Focus on the technical mastery of the animation studios, fight choreography, soundtrack design, and cinematic presentation."
        },
        "GENRE_GOLD_STANDARD": {
            "key": "GENRE_GOLD_STANDARD",
            "label": "Genre-Defining Gold Standard",
            "instruction": "Frame these shows as absolute benchmarks of their respective genres that set the standard for every anime that followed."
        },
        "IRRESISTIBLE_BINGE": {
            "key": "IRRESISTIBLE_BINGE",
            "label": "Impossible to Stop Bingeing",
            "instruction": "Focus on the irresistible momentum, cliffhangers, and pacing that make it impossible to stop watching after episode one."
        }
    },
    "genre_spotlight": {
        "PALATE_CLEANSER": {
            "key": "PALATE_CLEANSER",
            "label": "The Ultimate Genre Switch-Up",
            "instruction": "Frame these 3 shows as the perfect palate cleanser trio for anime burnout — switching seamlessly across totally distinct tones and worlds."
        },
        "BEST_IN_CLASS": {
            "key": "BEST_IN_CLASS",
            "label": "Peak Representatives of 3 Genres",
            "instruction": "Highlight how each show represents the absolute gold standard of its specific genre."
        },
        "MOOD_BASED": {
            "key": "MOOD_BASED",
            "label": "Match Your Viewing Mood",
            "instruction": "Frame the recommendations by viewing mood — what to watch when you want adrenaline, intense intrigue, or emotional depth."
        }
    },
    "upcoming_spotlight": {
        "MANGA_ARC_HYPE": {
            "key": "MANGA_ARC_HYPE",
            "label": "Upcoming Manga Arc Milestones",
            "instruction": "Focus on the specific confirmed manga/light novel story arc being adapted and why fans of the source material are hyped."
        },
        "STUDIO_STAFF_TALENT": {
            "key": "STUDIO_STAFF_TALENT",
            "label": "Studio & Staff Pedigree",
            "instruction": "Focus on the animation studio and director credentials behind these upcoming releases."
        },
        "PREMISE_INTRIGUE": {
            "key": "PREMISE_INTRIGUE",
            "label": "Hooking Unreleased Story Premises",
            "instruction": "Focus strictly on the high-concept story premises and character hooks that make these upcoming releases stand out."
        }
    },
    "character_spotlight": {
        "PROTAGONIST_GROWTH": {
            "key": "PROTAGONIST_GROWTH",
            "label": "Unforgettable Protagonist Journeys",
            "instruction": "Focus on the psychological depth, growth, and compelling flaws of the lead characters."
        },
        "BADASS_MOMENTS": {
            "key": "BADASS_MOMENTS",
            "label": "Iconic Badass Character Arcs",
            "instruction": "Focus on iconic screen presence, tactical intelligence, and memorable high-stakes character moments."
        }
    },
    "anime_comparison": {
        "THEMATIC_RIVALRY": {
            "key": "THEMATIC_RIVALRY",
            "label": "Thematic & Style Head-to-Head",
            "instruction": "Compare how these powerhouse series tackle similar themes or genres with completely different narrative philosophies."
        },
        "DECISION_GUIDE": {
            "key": "DECISION_GUIDE",
            "label": "Which One Should You Watch First?",
            "instruction": "Provide a clear decision guide for viewers torn between top-tier heavyweights."
        }
    }
}

def select_concept_angle(concept_key: str, days: int = 7) -> Dict[str, Any]:
    """Selects an angle variant for the concept, avoiding angles used within the last `days` days."""
    all_angles = CONCEPT_ANGLES.get(concept_key, {})
    if not all_angles:
        return {
            "key": "DEFAULT",
            "label": "General Overview",
            "instruction": "Provide a balanced, engaging recommendation highlighting key story elements."
        }
    
    recent_angle_keys = get_recent_concept_angles(concept_key, days=days)
    avail_keys = [k for k in all_angles.keys() if k not in recent_angle_keys]
    
    if not avail_keys:
        logger.info(f"[Concept Angle] All angles for '{concept_key}' used recently in last {days} days. Resetting angle pool.")
        avail_keys = list(all_angles.keys())

    chosen_key = random.choice(avail_keys)
    angle_info = all_angles[chosen_key]
    logger.info(f"[Concept Angle Selection] Concept '{concept_key}' -> Selected Angle: '{angle_info['label']}' ({chosen_key})")
    return angle_info

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
    Also selects an angle variant that avoids recently used angles for that concept.
    Returns (concept_key, concept_details_dict).
    """
    available_keys = list(CONCEPT_TYPES.keys())
    allowed_keys = [k for k in available_keys if is_concept_allowed_by_history(k, days=config.CONCEPT_COOLDOWN_DAYS)]
    
    if not allowed_keys:
        logger.warning("All concept types used in last 5 days! Resetting pool to all concept types.")
        allowed_keys = available_keys

    selected_key = random.choice(allowed_keys)
    concept_info = dict(CONCEPT_TYPES[selected_key])
    
    selected_angle = select_concept_angle(selected_key)
    concept_info["selected_angle"] = selected_angle
    concept_info["angle_key"] = selected_angle["key"]
    concept_info["angle_label"] = selected_angle["label"]
    concept_info["angle_instruction"] = selected_angle["instruction"]

    logger.info(f"[Concept Selection] Selected concept: '{selected_key}' ({concept_info['name']}) with Angle: '{selected_angle['label']}'")
    
    record_concept_usage(selected_key, angle_key=selected_angle["key"])
    return selected_key, concept_info

def select_candidate_titles(num_candidates: int = 3, concept_key: str = None) -> Tuple[List[Dict[str, Any]], str, Dict[str, Any]]:
    """
    Selects candidate titles tailored to today's Short concept type.
    Enforces 30-day anime title cooldown filtering and search pool expansion.
    """
    if not concept_key:
        concept_key, concept_info = select_today_concept()
    else:
        base_info = CONCEPT_TYPES.get(concept_key, CONCEPT_TYPES["top_recommendations"])
        concept_info = dict(base_info)
        selected_angle = select_concept_angle(concept_key)
        concept_info["selected_angle"] = selected_angle
        concept_info["angle_key"] = selected_angle["key"]
        concept_info["angle_label"] = selected_angle["label"]
        concept_info["angle_instruction"] = selected_angle["instruction"]

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

    # Immediately record selected anime titles to title_history.json for 30-day cooldown (Phase 1)
    if selected:
        record_anime_titles_usage(selected, concept_type=concept_key)

    return selected, concept_key, concept_info

if __name__ == "__main__":
    titles, c_key, c_info = select_candidate_titles(3)
    output_path = config.OUTPUT_DIR / "selected_titles.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({"candidates": titles, "concept_key": c_key, "concept_info": c_info}, f, indent=2)
    logger.info(f"Saved selected candidates & concept to {output_path}")
