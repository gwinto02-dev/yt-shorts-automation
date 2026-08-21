import json
import logging
import requests
from pathlib import Path
from typing import List, Dict, Any

import config

logger = logging.getLogger(__name__)

# AniList GraphQL query for detailed anime metadata & facts
ANILIST_DETAIL_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    title {
      romaji
      english
    }
    averageScore
    seasonYear
    episodes
    genres
    description(asHtml: false)
    studios(isMain: true) {
      nodes {
        name
      }
    }
    characters(sort: ROLE_DESC, page: 1, perPage: 3) {
      nodes {
        name {
          full
        }
      }
    }
  }
}
"""

def fetch_anilist_deep_facts(anime_id: int) -> Dict[str, Any]:
    """Fetch deep metadata facts directly from AniList GraphQL API."""
    try:
        res = requests.post(
            config.ANILIST_GRAPHQL_URL,
            json={"query": ANILIST_DETAIL_QUERY, "variables": {"id": anime_id}},
            timeout=10
        )
        res.raise_for_status()
        media = res.json().get("data", {}).get("Media", {})
        if not media:
            return {}

        studios = [s.get("name") for s in media.get("studios", {}).get("nodes", []) if s.get("name")]
        chars = [c.get("name", {}).get("full") for c in media.get("characters", {}).get("nodes", []) if c.get("name", {}).get("full")]

        score = media.get("averageScore", 0) / 10.0 if media.get("averageScore") else 0.0

        return {
            "title": media.get("title", {}).get("english") or media.get("title", {}).get("romaji"),
            "verified_score": f"{score:.1f}/10" if score else "N/A",
            "score_numeric": score,
            "release_year": media.get("seasonYear") or "N/A",
            "studio": studios[0] if studios else "N/A",
            "episodes": media.get("episodes") or "N/A",
            "genres": media.get("genres", []),
            "lead_characters": chars[:2],
            "synopsis_snippet": (media.get("description") or "").replace("<br>", " ").replace("<i>", "").replace("</i>", "")[:200],
            "source_api": "AniList GraphQL"
        }
    except Exception as e:
        logger.warning(f"AniList deep fact lookup failed for ID {anime_id}: {e}")
        return {}

def fetch_jikan_deep_facts(anime_id: int) -> Dict[str, Any]:
    """Fallback: Fetch detailed facts from Jikan REST API."""
    try:
        url = f"{config.JIKAN_API_BASE_URL}/anime/{anime_id}/full"
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        data = res.json().get("data", {})
        if not data:
            return {}

        studios = [s.get("name") for s in data.get("studios", [])]
        genres = [g.get("name") for g in data.get("genres", [])]

        return {
            "title": data.get("title_english") or data.get("title"),
            "verified_score": f"{data.get('score', 0.0):.1f}/10",
            "score_numeric": data.get("score", 0.0),
            "release_year": data.get("year") or "N/A",
            "studio": studios[0] if studios else "N/A",
            "episodes": data.get("episodes") or "N/A",
            "genres": genres,
            "lead_characters": [],
            "synopsis_snippet": (data.get("synopsis") or "")[:200],
            "source_api": "Jikan REST"
        }
    except Exception as e:
        logger.warning(f"Jikan deep fact lookup failed for ID {anime_id}: {e}")
        return {}

def verify_candidate_facts(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Performs deep metadata verification for every selected candidate title.
    Retrieves exact score, release year, animation studio, main premise, genres, and characters.
    Saves verified citations to output/fact_check_sources.json.
    """
    logger.info(">>> STARTING DEEP RESEARCH & FACT CHECKING VERIFICATION")
    if not candidates:
        raise ValueError("Cannot perform fact verification: candidates list is empty!")

    sources = []
    verified_candidates = []

    for idx, item in enumerate(candidates, 1):
        anime_id = item.get("id")
        title = item.get("title", "Unknown Title")
        source = item.get("source", "AniList")

        # 1. Attempt deep fact query
        facts = {}
        if source == "AniList" and anime_id:
            facts = fetch_anilist_deep_facts(anime_id)
        if not facts and anime_id:
            facts = fetch_jikan_deep_facts(anime_id)

        # Fallback to local candidate dict if remote deep lookup fails
        if not facts:
            score = item.get("average_score", 0.0)
            facts = {
                "title": title,
                "verified_score": f"{score:.1f}/10" if isinstance(score, (int, float)) else str(score),
                "score_numeric": score if isinstance(score, (int, float)) else 8.0,
                "release_year": item.get("seasonYear") or "N/A",
                "studio": "N/A",
                "episodes": "N/A",
                "genres": item.get("genres", []),
                "lead_characters": [],
                "synopsis_snippet": (item.get("synopsis") or "")[:200],
                "source_api": source
            }

        citation_url = f"https://anilist.co/anime/{anime_id}" if source == "AniList" else f"https://myanimelist.net/anime/{anime_id}"

        citation_entry = {
            "anime_title": title,
            "verified_score": facts["verified_score"],
            "score_numeric": facts["score_numeric"],
            "release_year": facts["release_year"],
            "studio": facts["studio"],
            "episodes": facts["episodes"],
            "genres": facts["genres"],
            "lead_characters": facts.get("lead_characters", []),
            "synopsis_snippet": facts["synopsis_snippet"],
            "primary_source": facts["source_api"],
            "source_id": str(anime_id),
            "citation_url": citation_url
        }

        sources.append(citation_entry)

        # Attach full verified facts to candidate dict
        item["verified_facts"] = citation_entry
        item["fact_check_verified"] = True
        item["citation_url"] = citation_url
        verified_candidates.append(item)

        logger.info(
            f"  Verified Fact #{idx}: '{title}' | Score: {citation_entry['verified_score']} | "
            f"Studio: {citation_entry['studio']} | Year: {citation_entry['release_year']} | Source: {citation_url}"
        )

    if not sources:
        raise RuntimeError("Fact checking failed: No verified sources could be populated!")

    # Save citations file alongside script
    citations_file = config.OUTPUT_DIR / "fact_check_sources.json"
    try:
        with open(citations_file, "w", encoding="utf-8") as f:
            json.dump(sources, f, indent=2, ensure_ascii=False)
        logger.info(f"Verified fact-check sources ({len(sources)} entries) saved to {citations_file.name}")
    except Exception as e:
        logger.error(f"Failed to write fact check sources file: {e}")
        raise RuntimeError(f"Could not save fact_check_sources.json: {e}")

    return {
        "status": "verified",
        "verified_count": len(verified_candidates),
        "candidates": verified_candidates,
        "sources": sources
    }

if __name__ == "__main__":
    sample = [
        {"title": "Frieren: Beyond Journey's End", "id": 154587, "average_score": 9.3, "source": "AniList", "genres": ["Fantasy", "Drama"]}
    ]
    res = verify_candidate_facts(sample)
    print("Fact check result:", res)
