import logging
from typing import Dict, Any, Tuple

logger = logging.getLogger(__name__)

# Absolute Popularity Baseline Thresholds
# Any anime exceeding either threshold is MAINSTREAM and CANNOT be categorized as underrated / hidden gem.
MAL_MEMBERS_UNDERRATED_MAX = 250000
ANILIST_POPULARITY_UNDERRATED_MAX = 100000

def get_item_popularity_metrics(item: Dict[str, Any]) -> Dict[str, Any]:
    """Extract absolute popularity metrics from candidate dictionary."""
    anilist_pop = item.get("popularity", 0)
    trending_score = item.get("trending_score", 0)
    
    # If source is Jikan, trending_score / popularity stores member count
    mal_members = item.get("mal_members") or item.get("members")
    if not mal_members and item.get("source") == "Jikan":
        mal_members = item.get("trending_score", 0)

    return {
        "anilist_popularity": anilist_pop,
        "mal_members": mal_members or 0,
        "source": item.get("source", "Unknown")
    }

def is_mainstream_anime(item: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Returns (is_mainstream: bool, reason: str).
    Checks absolute popularity baseline signals rather than daily relative mention counts.
    """
    metrics = get_item_popularity_metrics(item)
    anilist_pop = metrics["anilist_popularity"]
    mal_members = metrics["mal_members"]
    title = item.get("title", "Unknown Title")

    if anilist_pop > ANILIST_POPULARITY_UNDERRATED_MAX:
        reason = f"Title '{title}' is MAINSTREAM (AniList Popularity: {anilist_pop:,} > threshold {ANILIST_POPULARITY_UNDERRATED_MAX:,})."
        return True, reason

    if mal_members > MAL_MEMBERS_UNDERRATED_MAX:
        reason = f"Title '{title}' is MAINSTREAM (MAL Members: {mal_members:,} > threshold {MAL_MEMBERS_UNDERRATED_MAX:,})."
        return True, reason

    reason = f"Title '{title}' qualifies as relatively obscure/hidden (AniList Pop: {anilist_pop:,}, MAL Members: {mal_members:,})."
    return False, reason

def can_qualify_as_hidden_gem(item: Dict[str, Any], score_threshold: float = 7.5) -> Tuple[bool, str]:
    """
    Evaluates whether candidate item can qualify for 'hidden_gems' / 'underrated' concept.
    Rule: Must have High Rating (average_score >= score_threshold) AND Must NOT be Mainstream.
    `score_threshold` defaults to 7.5 but can be lowered by a controlled fallback
    when the daily candidate pool doesn't have enough 7.5+ titles clear of cooldown.
    """
    score = item.get("average_score", 0.0)
    title = item.get("title", "Unknown Title")

    if score < score_threshold:
        return False, f"Title '{title}' score ({score}/10) is below hidden gem threshold ({score_threshold}/10)."

    is_mainstream, reason = is_mainstream_anime(item)
    if is_mainstream:
        logger.info(f"[PopularityFilter EXCLUDE] {reason}")
        return False, reason

    logger.info(f"[PopularityFilter QUALIFIED] Title '{title}' (Score: {score}/10) qualifies for Hidden Gems concept.")
    return True, f"Qualifies for Hidden Gems (Score: {score}/10, not mainstream)."
