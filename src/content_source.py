import json
import logging
import random
import re
import time
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

# AniList sits behind Cloudflare, and Jikan proxies MyAnimeList — both are known to
# reject requests carrying the default python-requests User-Agent as likely bot
# traffic, especially from datacenter/CI IP ranges like GitHub Actions runners.
# A realistic browser-style User-Agent avoids that class of block outright.
API_REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

# Local cache written after any successful remote fetch, used as a last-resort
# fallback when BOTH AniList and Jikan fail in the same run (previously there
# was no such safety net unless a separate, unrelated project happened to have
# recently populated data/processed/).
_ANIME_POOL_CACHE_FILE = "normalized_anime_pool_cache.json"


def _request_with_retry(method: str, url: str, max_retries: int = 3,
                         backoff_base: float = 2.0, **kwargs) -> requests.Response:
    """
    Thin retry wrapper around requests, with exponential backoff, for the
    transient failures (timeouts, connection resets, 5xx, and the occasional
    Cloudflare hiccup) that these third-party anime APIs throw constantly.
    Raises the last exception if every attempt fails.
    """
    kwargs.setdefault("headers", API_REQUEST_HEADERS)
    kwargs.setdefault("timeout", 10)
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt < max_retries:
                wait = backoff_base * attempt
                logger.warning(
                    f"[Request Retry] {method} {url} failed on attempt {attempt}/{max_retries} "
                    f"({e}). Retrying in {wait:.0f}s..."
                )
                time.sleep(wait)
    raise last_exc


def _save_anime_pool_cache(candidates: List[Dict[str, Any]]) -> None:
    """Persist a successful fetch so a future run has a genuine local fallback
    even if data/processed/ (from the separate Buzz Tracker project) is stale
    or missing entirely."""
    if not candidates:
        return
    try:
        cache_path = config.DATA_DIR / _ANIME_POOL_CACHE_FILE
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(candidates, f, indent=2)
        logger.info(f"[Anime Pool Cache] Saved {len(candidates)} candidate(s) to {cache_path}")
    except Exception as e:
        logger.warning(f"[Anime Pool Cache] Failed to write local fallback cache: {e}")

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
    """Look for local trend JSON files produced by Daily Anime Buzz Tracker, or
    fall back to this pipeline's own cache of its last successful remote fetch
    (written by _save_anime_pool_cache) if the Buzz Tracker hasn't run recently."""
    data_dir = config.DATA_DIR
    processed_dir = data_dir / "processed"
    search_paths = []
    
    if processed_dir.exists():
        search_paths.extend(list(processed_dir.glob("normalized_anime_*.json")))
    if data_dir.exists():
        search_paths.extend(list(data_dir.glob("daily_report_*.json")))

    cache_path = data_dir / _ANIME_POOL_CACHE_FILE
    if not search_paths and cache_path.exists():
        search_paths.append(cache_path)

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
        response = _request_with_retry(
            "POST",
            config.ANILIST_GRAPHQL_URL,
            json={"query": ANILIST_TRENDING_QUERY, "variables": variables},
        )
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
        if normalized:
            _save_anime_pool_cache(normalized)
        return normalized
    except Exception as e:
        logger.error(f"Error fetching from AniList API: {e}")
        return []

def fetch_anilist_upcoming(count: int = 50, page: int = 1) -> List[Dict[str, Any]]:
    """Fetch upcoming unreleased anime list directly from AniList GraphQL API with expanded pool size."""
    logger.info(f"Fetching upcoming anime from AniList GraphQL API (page={page}, count={count})...")
    variables = {"page": page, "perPage": count}
    try:
        response = _request_with_retry(
            "POST",
            config.ANILIST_GRAPHQL_URL,
            json={"query": ANILIST_UPCOMING_QUERY, "variables": variables},
        )
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
        response = _request_with_retry("GET", url)
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
        if normalized:
            _save_anime_pool_cache(normalized)
        return normalized
    except Exception as e:
        logger.error(f"Error fetching from Jikan API: {e}")
        return []

def fetch_jikan_upcoming(count: int = 50) -> List[Dict[str, Any]]:
    """Fallback: fetch upcoming/not-yet-aired anime from Jikan's seasons/upcoming
    endpoint. Used by the Upcoming Trio concept, which previously had no
    fallback at all when AniList was blocked (its single biggest recurring
    daily failure)."""
    logger.info(f"Fetching upcoming anime from Jikan REST API fallback (count={count})...")
    url = f"{config.JIKAN_API_BASE_URL}/seasons/upcoming"
    try:
        response = _request_with_retry("GET", url, params={"limit": min(count, 25)})
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
                "average_score": item.get("score") or 0.0,
                "popularity": item.get("popularity", 0) or 0,
                "trending_score": item.get("members", 0) or 0,
                "synopsis": item.get("synopsis", "") or "",
                "status": "NOT_YET_RELEASED",
                "seasonYear": item.get("year") or 2026,
                "is_upcoming": True,
                "source": "Jikan"
            })
        if normalized:
            _save_anime_pool_cache(normalized)
        return normalized
    except Exception as e:
        logger.error(f"Error fetching upcoming anime from Jikan API: {e}")
        return []

# Kitsu status vocabulary differs from AniList's ("current"/"finished"/"upcoming"/"tba"
# vs "RELEASING"/"FINISHED"/"NOT_YET_RELEASED") — normalize so downstream code (which
# was written against AniList's enum) behaves consistently regardless of source.
_KITSU_STATUS_MAP = {
    "current": "RELEASING",
    "finished": "FINISHED",
    "upcoming": "NOT_YET_RELEASED",
    "tba": "NOT_YET_RELEASED",
}

def fetch_kitsu_trending(count: int = 50, status_filter: str = None) -> List[Dict[str, Any]]:
    """
    Second fallback (independent of both AniList and Jikan): Kitsu's own anime
    listing, sorted by community size as a popularity proxy. Kitsu is not
    behind the same Cloudflare front as AniList and, empirically, has never
    failed in this pipeline's logs — it's already used successfully for
    per-title cover image lookups in visuals.py.
    status_filter, if given (e.g. "upcoming"), is passed through as Kitsu's
    own filter[status] value so this same function can also serve the
    Upcoming Trio concept, which previously had NO fallback at all if
    AniList was blocked.
    NOTE: Kitsu doesn't return genres/categories on this endpoint without an
    extra relationship fetch per title, so 'genres' comes back empty here.
    That's an acceptable tradeoff for an emergency last-resort source — genre-
    dependent concepts (e.g. Genre-Diverse Trio) simply won't get genre
    guarantees on a run that had to fall all the way back to this source.
    """
    logger.info(f"Fetching {'upcoming' if status_filter else 'top'} anime from Kitsu REST API (count={count})...")
    url = f"{config.KITSU_API_BASE_URL}/anime"
    try:
        # Kitsu implements the JSON:API spec strictly and returns 406 Not
        # Acceptable for a generic "Accept: application/json" header (the
        # default used for AniList/Jikan) — it requires the JSON:API media
        # type specifically. Override the shared default for this call only.
        kitsu_headers = dict(API_REQUEST_HEADERS)
        kitsu_headers["Accept"] = "application/vnd.api+json"
        kitsu_headers["Content-Type"] = "application/vnd.api+json"
        params = {"sort": "-userCount", "page[limit]": min(count, 20)}
        if status_filter:
            params["filter[status]"] = status_filter
        response = _request_with_retry(
            "GET", url,
            params=params,
            headers=kitsu_headers,
        )
        res_data = response.json()
        data_list = res_data.get("data", [])

        normalized = []
        for item in data_list:
            attrs = item.get("attributes", {}) or {}
            titles = attrs.get("titles", {}) or {}
            title_eng = titles.get("en") or titles.get("en_jp") or attrs.get("canonicalTitle")
            poster = attrs.get("posterImage", {}) or {}
            avg_rating = attrs.get("averageRating")
            start_date = attrs.get("startDate") or ""
            normalized.append({
                "id": item.get("id"),
                "title": title_eng,
                "title_romaji": attrs.get("canonicalTitle"),
                "cover_image": poster.get("large") or poster.get("original"),
                "genres": [],
                "average_score": (float(avg_rating) / 10.0) if avg_rating else 0.0,
                "popularity": attrs.get("userCount", 0),
                "trending_score": attrs.get("favoritesCount", 0),
                "synopsis": attrs.get("synopsis", ""),
                "status": _KITSU_STATUS_MAP.get(attrs.get("status"), "FINISHED"),
                "seasonYear": int(start_date[:4]) if start_date[:4].isdigit() else None,
                "is_upcoming": bool(status_filter),
                "source": "Kitsu"
            })
        if normalized:
            _save_anime_pool_cache(normalized)
        return normalized
    except Exception as e:
        logger.error(f"Error fetching from Kitsu API: {e}")
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
        # Previously this concept had ZERO fallback — a single AniList block
        # (its most common daily failure) aborted the entire pipeline outright
        # even though every other concept type falls back through Kitsu/Jikan.
        if not candidates:
            logger.warning("AniList upcoming fetch failed — falling back to Jikan seasons/upcoming.")
            candidates = fetch_jikan_upcoming(50)
        if not candidates:
            logger.warning("Jikan upcoming fetch failed — falling back to Kitsu (filter[status]=upcoming).")
            candidates = fetch_kitsu_trending(50, status_filter="upcoming")
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
            # Kitsu is tried before Jikan: it has never failed once in this
            # pipeline's run history (it already powers per-title cover
            # lookups reliably), whereas Jikan times out constantly. Jikan
            # stays as a final fallback in case Kitsu is ever the one having
            # a bad day instead.
            candidates = fetch_kitsu_trending(50)
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

    # Search pool expansion if uncooldowned pool is too small OR (for hidden_gems)
    # too few titles actually clear the 7.5+/non-mainstream bar. A pool can look
    # fine by raw count (e.g. 21 titles) while having almost none that qualify
    # for this specific concept — checking raw count alone missed that case.
    needs_expansion = len(uncooldowned_candidates) < num_candidates
    if not needs_expansion and concept_key == "hidden_gems":
        qualifying_count = sum(
            1 for c in uncooldowned_candidates if can_qualify_as_hidden_gem(c)[0]
        )
        if qualifying_count < num_candidates:
            needs_expansion = True
            logger.warning(
                f"Uncooldowned pool has {len(uncooldowned_candidates)} titles but only "
                f"{qualifying_count} qualify for Hidden Gems (score 7.5+, non-mainstream). Expanding pool..."
            )

    # Same class of bug as Hidden Gems above, but for Genre-Diverse Trio: a pool can look
    # fine by raw count while covering too few DISTINCT primary genres to ever satisfy the
    # "3 non-overlapping genres" requirement. Checking raw count alone missed that case and
    # caused a hard, unrecoverable crash (ValueError) later in this function with zero video
    # produced and zero email sent. Pre-check distinct genre coverage here so we expand the
    # pool BEFORE selection instead of failing after the fact.
    if not needs_expansion and concept_key == "genre_spotlight":
        distinct_genres = {(c.get("genres") or ["General"])[0] for c in uncooldowned_candidates}
        if len(distinct_genres) < num_candidates:
            needs_expansion = True
            logger.warning(
                f"Uncooldowned pool has {len(uncooldowned_candidates)} titles but only "
                f"{len(distinct_genres)} distinct primary genre(s) ({', '.join(distinct_genres)}). "
                "Expanding pool for Genre-Diverse Trio..."
            )

    if needs_expansion:
        logger.warning(f"Uncooldowned/qualifying pool low. Expanding AniList search pool (Page 2 & Jikan)...")
        extra_candidates = []
        if concept_key == "upcoming_spotlight":
            extra_candidates = fetch_anilist_upcoming(50, page=2)
            if not extra_candidates:
                extra_candidates = fetch_jikan_upcoming(50)
            if not extra_candidates:
                extra_candidates = fetch_kitsu_trending(50, status_filter="upcoming")
        else:
            extra_candidates = fetch_anilist_trending(50, page=2) + fetch_jikan_top(50)
            if not extra_candidates:
                # AniList page 2 and Jikan are exactly the two sources that
                # were failing on the days this branch actually triggers
                # (403 / 504) — Kitsu was only ever tried on the *initial*
                # fetch, never here on expansion, so a bad AniList/Jikan day
                # meant expansion silently did nothing.
                extra_candidates = fetch_kitsu_trending(50)

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

    # If still empty/insufficient, fall back to valid_candidates as absolute last resort — this keeps
    # genre_spotlight/hidden_gems/upcoming_spotlight's own downstream degradation logic (below) working
    # exactly as designed. The new guard further down (after mode selection) catches the specific case
    # this masked in practice: top_recommendations/character_spotlight/anime_comparison have no such
    # downstream logic of their own and would otherwise silently render a video using titles already
    # known to violate cooldown.
    used_last_resort_fallback = len(uncooldowned_candidates) < num_candidates
    if used_last_resort_fallback:
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
            # Even after the pre-check + pool expansion above, the pool still doesn't cover
            # enough distinct genres (can happen on a slow trending day). Previously this
            # raised an unhandled ValueError that crashed the ENTIRE pipeline before Phase 1
            # even finished — no video, no email report, nothing. That's strictly worse than
            # a slightly-less-diverse Genre-Diverse Trio, so relax the constraint instead of
            # aborting the whole day's run: fill remaining slots with the next best-scoring,
            # not-yet-selected titles regardless of genre overlap.
            logger.warning(
                f"Genre-Diverse Trio: only found {len(selected)}/{num_candidates} titles with fully "
                "distinct primary genres after pool expansion. Relaxing genre-overlap constraint to "
                "fill the remaining slot(s) rather than aborting the entire pipeline run."
            )
            for c in sorted_candidates:
                if len(selected) >= num_candidates:
                    break
                if c["id"] in seen_ids:
                    continue
                genres = c.get("genres", [])
                primary_genre = genres[0] if genres else "General"
                c["selection_category"] = "Genre-Diverse Pick"
                c["selection_reasoning"] = f"Primary Genre: '{primary_genre}' (genre pool exhausted; some overlap possible)."
                c["excluded_in_run"] = excluded_candidates
                selected.append(c)
                seen_ids.add(c["id"])

            if len(selected) < num_candidates:
                raise ValueError(
                    f"Genre-Diverse Trio criteria failed: Found only {len(selected)}/{num_candidates} "
                    "titles in candidate pool even after relaxing genre-overlap constraint (pool itself "
                    "is too small)."
                )

    # ==================== MODE 2: UNDERRATED TRIO ====================
    elif concept_key == "hidden_gems":
        sorted_candidates = sorted(selection_pool, key=lambda x: x.get("average_score", 0), reverse=True)

        # Try the strict 7.5 threshold first, then step down gracefully rather
        # than crashing the whole pipeline when the daily pool is thin (e.g.
        # after the 30-day title cooldown removes most high scorers). Each
        # step is logged explicitly so it's visible in the run log, not silent.
        SCORE_FALLBACK_STEPS = [7.5, 7.2, 7.0, 6.8]

        for step_idx, threshold in enumerate(SCORE_FALLBACK_STEPS):
            selected = []
            seen_ids = set()
            step_excluded = []

            for c in sorted_candidates:
                if len(selected) >= num_candidates:
                    break
                if c["id"] in seen_ids:
                    continue
                qualifies, reasoning = can_qualify_as_hidden_gem(c, score_threshold=threshold)
                if qualifies:
                    c["selection_category"] = "Underrated Hidden Gem"
                    c["selection_reasoning"] = reasoning
                    if threshold < 7.5:
                        c["selection_reasoning"] += f" [FALLBACK: threshold relaxed to {threshold}/10 due to thin daily pool]"
                    c["excluded_in_run"] = excluded_candidates
                    selected.append(c)
                    seen_ids.add(c["id"])
                else:
                    step_excluded.append((c["title"], reasoning))

            if len(selected) >= num_candidates:
                if threshold < 7.5:
                    logger.warning(
                        f"[Hidden Gems FALLBACK] Only found {num_candidates} qualifying titles after relaxing "
                        f"score threshold from 7.5 to {threshold}/10. Consider widening the AniList search pool "
                        f"or shortening the cooldown window if this keeps happening."
                    )
                break
            else:
                logger.info(
                    f"[Hidden Gems] Threshold {threshold}/10 yielded only {len(selected)}/{num_candidates} qualifying titles. "
                    f"{'Trying next fallback threshold...' if step_idx < len(SCORE_FALLBACK_STEPS) - 1 else ''}"
                )
                for t, r in step_excluded:
                    logger.info(f"[ContentSource EXCLUDE] '{t}' did not qualify for Underrated Trio: {r}")

        if len(selected) < num_candidates:
            raise ValueError(
                f"Underrated Trio criteria failed: Found only {len(selected)}/{num_candidates} qualifying underrated "
                f"titles even after relaxing the score threshold down to {SCORE_FALLBACK_STEPS[-1]}/10. "
                f"The candidate pool is genuinely too thin today (likely due to cooldown exclusions) — "
                f"widen the AniList/Jikan pool size or shorten ANIME_TITLE_COOLDOWN_DAYS."
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

    # top_recommendations, character_spotlight, and anime_comparison have no concept-specific
    # validation of their own (unlike genre_spotlight/hidden_gems/upcoming_spotlight above, which
    # already gracefully degrade or raise their own clear ValueError). If the pool was so thin that
    # selection had to fall back to valid_candidates (including cooldown-violating titles), these
    # modes would silently render a full video using already-featured titles — which the final
    # Supervisor QA Anime Title Cooldown check is guaranteed to catch and block anyway, after burning
    # a full ~3 minute render (script, TTS, FFmpeg) for nothing. Fail fast here instead.
    if used_last_resort_fallback and concept_key not in ("genre_spotlight", "hidden_gems", "upcoming_spotlight"):
        uncooldowned_ids = {u["id"] for u in uncooldowned_candidates}
        cooldown_violators = [c["title"] for c in selected if c["id"] not in uncooldowned_ids]
        if cooldown_violators:
            raise RuntimeError(
                f"Candidate pool exhausted for '{concept_key}': had to reuse cooldown-violating title(s) "
                f"({', '.join(cooldown_violators)}) to fill {num_candidates} picks — this would only get "
                "blocked at the final Supervisor QA gate after a full render. Widen ANIME_TITLE_COOLDOWN_DAYS, "
                "increase the fetched pool size, or wait for a day with fresher API data."
            )

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
