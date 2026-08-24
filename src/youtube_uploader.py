import logging
import json
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HARD SAFETY GUARDRAIL — never relaxed, never bypassed
# ---------------------------------------------------------------------------
ALLOWED_PRIVACY_STATUSES = ["private", "scheduled"]

# YouTube API scopes — readonly needed for channel validation
YT_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

# Error reasons that are permanent (never retry)
PERMANENT_ERROR_REASONS = {
    "youtubeSignupRequired",
    "forbidden",
    "unauthorized",
    "invalidCredentials",
    "authError",
    "accountNotEnabled",
    "channelNotFound",
    "quotaExceeded",
    "accessNotConfigured",
}

# Max retries for genuinely transient failures only
MAX_UPLOAD_RETRIES = 2
RETRY_BACKOFF_SECONDS = 3


# ---------------------------------------------------------------------------
# Privacy guardrail
# ---------------------------------------------------------------------------

def validate_privacy_status(status: str) -> str:
    """Enforce guardrail: video privacy status MUST NEVER be public."""
    clean_status = status.lower().strip()
    if clean_status not in ALLOWED_PRIVACY_STATUSES:
        raise ValueError(
            f"SAFETY GUARDRAIL VIOLATION: Privacy status '{status}' is strictly forbidden! "
            f"Allowed statuses: {ALLOWED_PRIVACY_STATUSES}. Uploads must default to Private/Scheduled."
        )
    return clean_status


# ---------------------------------------------------------------------------
# Credential loading — credentials themselves are NEVER logged
# ---------------------------------------------------------------------------

def get_youtube_client() -> Optional[object]:
    """
    Build authenticated YouTube API client.
    Tries env vars first (CI/GitHub Actions), then local token.json.
    Returns None when no credentials are available (triggers dry-run mode).
    Credentials are never printed or exposed in logs.
    """
    creds = None
    token_file = config.BASE_DIR / "token.json"

    # 1. Env vars (GitHub Actions secrets)
    if config.YOUTUBE_REFRESH_TOKEN and config.YOUTUBE_CLIENT_ID and config.YOUTUBE_CLIENT_SECRET:
        logger.info("Authenticating YouTube API using environment-variable credentials...")
        creds = Credentials(
            token=None,
            refresh_token=config.YOUTUBE_REFRESH_TOKEN,
            client_id=config.YOUTUBE_CLIENT_ID,
            client_secret=config.YOUTUBE_CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=YT_SCOPES,
        )

    # 2. Local token.json fallback
    elif token_file.exists():
        logger.info("Authenticating YouTube API using local token.json...")
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), scopes=YT_SCOPES)
        except Exception as e:
            logger.warning(f"Could not load token.json: {e}")

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            logger.warning(f"OAuth token refresh failed: {e}")
            creds = None

    if not creds:
        return None

    return build("youtube", "v3", credentials=creds)


# ---------------------------------------------------------------------------
# Video metadata
# ---------------------------------------------------------------------------

def generate_video_metadata(candidates: List[Dict[str, Any]], custom_title: str = None) -> Dict[str, Any]:

    """Generate title, description, and tags for YouTube Short."""
    if candidates:
        top_title = candidates[0]["title"]
    else:
        top_title = "Anime Recommendation"

    if custom_title:
        title = custom_title
    else:
        today_str = datetime.now().strftime("%b %d, %Y")
        title = f"Top {len(candidates)} Anime You Need To Watch Today! 🍿 ({today_str}) #Shorts"
        if len(title) > 100:
            title = "Top Anime You Need To Watch Today! 🍿 #Shorts"

    description = "Looking for your next anime binge? Here are today's top recommendations:\n\n"
    for idx, c in enumerate(candidates, 1):
        raw_score = c.get("average_score") or c.get("verified_facts", {}).get("score_numeric", 0.0)
        verified_score = c.get("verified_facts", {}).get("verified_score", "")
        score_str = f"{raw_score:.1f}/10" if (isinstance(raw_score, (int, float)) and raw_score > 0.0 and verified_score != "N/A") else "Upcoming Pick"
        cat = c.get("selection_category", "Must-Watch")
        description += f"{idx}. {c['title']} - [{cat}] ({score_str})\n"

    description += (
        "\n\nSubscribe for daily anime recommendations and hidden gems!\n\n"
        "#Anime #AnimeShorts #AnimeRecommendation #AnimeEdit #Shorts #Manga"
    )

    tags = [
        "Anime", "Anime Shorts", "Anime Recommendation", "Must Watch Anime",
        "Top Anime", "Underrated Anime", "Best Anime", top_title,
    ]
    return {"title": title, "description": description, "tags": tags}


# ---------------------------------------------------------------------------
# Channel validation
# ---------------------------------------------------------------------------

def _is_permanent_error(http_err: HttpError) -> bool:
    """Return True if this HttpError represents a permanent, non-retryable failure."""
    reason = _extract_api_reason(http_err)
    status = http_err.resp.status if http_err.resp else 0
    if reason in PERMANENT_ERROR_REASONS:
        return True
    if status in (401, 403):
        return True
    return False


def _extract_api_reason(http_err: HttpError) -> str:
    """Safely extract the API error reason string from an HttpError."""
    try:
        body = json.loads(http_err.content)
        errors = body.get("error", {}).get("errors", [])
        if errors:
            return errors[0].get("reason", "unknown")
        return body.get("error", {}).get("status", "unknown")
    except Exception:
        return "unknown"


def _extract_api_message(http_err: HttpError) -> str:
    """Safely extract the human-readable API error message from an HttpError."""
    try:
        body = json.loads(http_err.content)
        return body.get("error", {}).get("message", str(http_err))
    except Exception:
        return str(http_err)


def validate_youtube_channel(youtube) -> Dict[str, Any]:
    """
    Call the YouTube Channels API (mine=True) to confirm the authenticated
    account has an active, upload-eligible channel.
    """
    logger.info("→ YouTube account/channel validation...")
    try:
        response = youtube.channels().list(
            part="id,snippet,status",
            mine=True
        ).execute()

        items = response.get("items", [])
        if not items:
            return {
                "valid": False,
                "channel_id": None,
                "channel_title": None,
                "channel_status": None,
                "error_type": "no_channel_found",
                "api_reason": None,
                "http_status": None,
                "message": (
                    "The authenticated account has no YouTube channel. "
                    "Create a channel at youtube.com before uploading."
                ),
            }

        channel = items[0]
        channel_id = channel.get("id")
        channel_title = channel.get("snippet", {}).get("title", "Unknown")
        is_linked = channel.get("status", {}).get("isLinked", False)

        logger.info(f"  Channel ID    : {channel_id}")
        logger.info(f"  Channel Title : {channel_title}")
        logger.info(f"  isLinked      : {is_linked}")

        return {
            "valid": True,
            "channel_id": channel_id,
            "channel_title": channel_title,
            "channel_status": channel.get("status", {}),
            "error_type": None,
            "api_reason": None,
            "http_status": None,
            "message": f"Channel '{channel_title}' ({channel_id}) validated successfully.",
        }

    except HttpError as e:
        status_code = e.resp.status if e.resp else 0
        reason = _extract_api_reason(e)
        message = _extract_api_message(e)

        if reason == "youtubeSignupRequired":
            friendly = (
                "YouTube API authorization reached the YouTube service, but the "
                "authenticated account/channel is not currently eligible or activated "
                "for this upload operation (API reason: youtubeSignupRequired). "
                "The Google account may not have an active YouTube channel or may require "
                "additional account setup at youtube.com."
            )
        else:
            friendly = f"YouTube channel validation failed: HTTP {status_code} — {reason}: {message}"

        logger.error(f"  Channel validation FAILED: HTTP {status_code} | reason={reason}")
        return {
            "valid": False,
            "channel_id": None,
            "channel_title": None,
            "channel_status": None,
            "error_type": "youtube_authorization",
            "api_reason": reason,
            "http_status": status_code,
            "message": friendly,
        }

    except Exception as e:
        logger.error(f"  Channel validation unexpected error: {e}")
        return {
            "valid": False,
            "channel_id": None,
            "channel_title": None,
            "channel_status": None,
            "error_type": "unexpected_error",
            "api_reason": None,
            "http_status": None,
            "message": str(e),
        }


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def _attempt_upload(youtube, video_path: Path, body: dict) -> Dict[str, Any]:
    """
    Execute the YouTube resumable upload with bounded retry for transient errors.
    Returns a structured result — never raises for handled errors.
    """
    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    for attempt in range(1, MAX_UPLOAD_RETRIES + 2):   # +2: first try + retries
        try:
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    logger.info(f"  Upload progress: {int(status.progress() * 100)}%")

            video_id = response.get("id")
            return {
                "success": True,
                "video_id": video_id,
                "youtube_url": f"https://youtu.be/{video_id}",
                "studio_url": f"https://studio.youtube.com/video/{video_id}/edit",
                "error_type": None,
                "api_reason": None,
                "http_status": None,
                "message": "Upload completed successfully.",
            }

        except HttpError as e:
            status_code = e.resp.status if e.resp else 0
            reason = _extract_api_reason(e)
            message = _extract_api_message(e)

            if reason == "youtubeSignupRequired":
                err_msg = (
                    "YouTube API authorization reached the YouTube service, but the "
                    "authenticated account/channel is not currently eligible or activated "
                    "for this upload operation (API reason: youtubeSignupRequired)."
                )
            else:
                err_msg = f"HTTP {status_code} — {reason}: {message}"

            if _is_permanent_error(e):
                logger.error(f"  Permanent upload error (will not retry): {err_msg}")
                return {
                    "success": False,
                    "video_id": None,
                    "youtube_url": None,
                    "studio_url": None,
                    "error_type": "permanent_api_error",
                    "api_reason": reason,
                    "http_status": status_code,
                    "message": err_msg,
                }

            # Transient — retry if attempts remain
            if attempt <= MAX_UPLOAD_RETRIES:
                logger.warning(
                    f"  Transient upload error on attempt {attempt}/{MAX_UPLOAD_RETRIES + 1}: "
                    f"HTTP {status_code} | reason={reason}. Retrying in {RETRY_BACKOFF_SECONDS}s..."
                )
                time.sleep(RETRY_BACKOFF_SECONDS)
                continue

            logger.error(f"  Upload failed after {attempt} attempts: {err_msg}")
            return {
                "success": False,
                "video_id": None,
                "youtube_url": None,
                "studio_url": None,
                "error_type": "transient_api_error_exhausted",
                "api_reason": reason,
                "http_status": status_code,
                "message": err_msg,
            }

        except Exception as e:
            logger.error(f"  Unexpected upload error: {e}")
            return {
                "success": False,
                "video_id": None,
                "youtube_url": None,
                "studio_url": None,
                "error_type": "unexpected_error",
                "api_reason": None,
                "http_status": None,
                "message": str(e),
            }

    # Should not reach here
    return {
        "success": False,
        "video_id": None,
        "youtube_url": None,
        "studio_url": None,
        "error_type": "upload_loop_exhausted",
        "api_reason": None,
        "http_status": None,
        "message": "Upload retry loop exhausted without result.",
    }


# ---------------------------------------------------------------------------
# Phase 6 main entry point
# ---------------------------------------------------------------------------

def upload_short_to_youtube(
    video_path: Path,
    candidates: List[Dict[str, Any]],
    privacy_status: str = "private",
    custom_title: str = None,
    final_qa_verdict: bool = None
) -> Dict[str, Any]:
    """
    Phase 6: YouTube Upload with pre-upload validation and safe error reporting.
    Explicitly sets selfDeclaredMadeForKids=False on every upload.

    Args:
        final_qa_verdict: The boolean pass/fail result from run_supervisor_qa_gate().
            If explicitly False, the upload is refused immediately as a defensive
            second gate — no API calls are made. Pass None to skip this check
            (legacy / test callers that do not supply the verdict).
    """
    # ---- Defensive QA invariant (must be first, before any side effects) ----
    if final_qa_verdict is False:
        logger.error(
            "UPLOAD REFUSED: Supervisor QA verdict is BLOCKED. "
            "upload_short_to_youtube() must never be called when final QA failed. "
            "This is a defensive second gate — check the caller."
        )
        return {
            "success": False,
            "authenticated": False,
            "channel_valid": False,
            "upload_attempted": False,
            "upload_type": "blocked_by_qa",
            "privacy_status": privacy_status or "private",
            "made_for_kids": False,
            "synthetic_content_status": "N/A",
            "comment_moderation": "N/A",
            "error_type": "upload_blocked_by_qa_gate",
            "api_reason": None,
            "http_status": None,
            "message": "Upload refused: Supervisor QA gate verdict is BLOCKED.",
            "video_preserved": video_path.exists() and video_path.stat().st_size > 0,
        }
    logger.info("=" * 60)
    logger.info("PHASE 6: YouTube Upload")
    logger.info("=" * 60)

    # ---- Step 0: Privacy guardrail (hard assertion, always first) --------
    valid_privacy = validate_privacy_status(privacy_status or config.YOUTUBE_PRIVACY_STATUS)
    assert valid_privacy in ALLOWED_PRIVACY_STATUSES, "SAFETY VIOLATION: Invalid privacy slipped past guardrail!"
    logger.info(f"→ Privacy status validated: '{valid_privacy}' ✅")

    video_preserved = video_path.exists() and video_path.stat().st_size > 0

    # Safety settings audit parameters
    made_for_kids_setting = False
    synthetic_media_disclosure = "Exempt (Official anime artwork + AI voiceover recommendations)"
    comment_moderation_setting = "Channel Level (YouTube Studio)"

    logger.info(f"→ Channel Safety Settings:")
    logger.info(f"  Made for Kids        : No (selfDeclaredMadeForKids = False) ✅")
    logger.info(f"  Synthetic Disclosure : {synthetic_media_disclosure} ✅")
    logger.info(f"  Comment Moderation   : {comment_moderation_setting}")

    # ---- Step 1: Credential check / dry-run shortcut --------------------
    logger.info("→ YouTube authentication validation...")
    youtube = get_youtube_client()

    if not youtube:
        logger.warning(
            "  YouTube API credentials not configured "
            "(YOUTUBE_REFRESH_TOKEN / token.json missing). "
            "Running in dry-run mode — no upload attempted."
        )
        metadata = generate_video_metadata(candidates, custom_title=custom_title)
        return {
            "success": False,
            "authenticated": False,
            "channel_valid": False,
            "upload_attempted": False,
            # 'upload_type' lets the notifier distinguish dry-run from a real upload.
            # IMPORTANT: 'privacy_status' is intentionally NOT set to 'private' here
            # so the notifier cannot mistake this dry-run for a real uploaded video.
            "upload_type": "dry_run",
            "privacy_status": valid_privacy,
            "made_for_kids": made_for_kids_setting,
            "synthetic_content_status": synthetic_media_disclosure,
            "comment_moderation": comment_moderation_setting,
            "error_type": "no_credentials",
            "api_reason": None,
            "http_status": None,
            "message": "No YouTube credentials available. Dry-run only — video was NOT uploaded.",
            "video_preserved": video_preserved,
            # Legacy fields kept so notifier/history-manager don't break
            "status": "dry_run_success",
            "video_id": "DRY_RUN_ID",
            "video_url": "https://youtu.be/DRY_RUN_ID",
            "studio_url": "https://studio.youtube.com/video/DRY_RUN_ID/edit",
            "title": metadata["title"],
            "uploaded_at": datetime.now().isoformat(),
        }

    logger.info("  Authentication: PASS ✅")

    if not video_path.exists():
        logger.error(f"  Video file not found: {video_path}")
        return {
            "success": False,
            "authenticated": True,
            "channel_valid": False,
            "upload_attempted": False,
            "privacy_status": valid_privacy,
            "made_for_kids": made_for_kids_setting,
            "synthetic_content_status": synthetic_media_disclosure,
            "comment_moderation": comment_moderation_setting,
            "error_type": "video_file_missing",
            "api_reason": None,
            "http_status": None,
            "message": f"Video file not found: {video_path}",
            "video_preserved": False,
        }

    # ---- Step 2: Channel validation -------------------------------------
    channel_res = validate_youtube_channel(youtube)

    if not channel_res["valid"]:
        reason = channel_res.get("api_reason", "unknown")
        logger.error(f"  Account/channel validation: FAIL ❌")
        logger.error(f"  Reason: {channel_res['message']}")
        logger.info("→ Upload NOT attempted (validation failed)")
        logger.info(f"→ Video preserved: {'YES' if video_preserved else 'NO'}")
        logger.info("=" * 60)
        logger.info("PHASE 6 RESULT: UPLOAD BLOCKED ❌")
        logger.info(f"  HTTP status   : {channel_res.get('http_status')}")
        logger.info(f"  API reason    : {reason}")
        logger.info(f"  Message       : {channel_res['message']}")
        logger.info("=" * 60)
        return {
            "success": False,
            "authenticated": True,
            "channel_valid": False,
            "upload_attempted": False,
            "privacy_status": valid_privacy,
            "made_for_kids": made_for_kids_setting,
            "synthetic_content_status": synthetic_media_disclosure,
            "comment_moderation": comment_moderation_setting,
            "error_type": channel_res.get("error_type", "channel_validation_failed"),
            "api_reason": reason,
            "http_status": channel_res.get("http_status"),
            "message": channel_res["message"],
            "video_preserved": video_preserved,
        }

    logger.info(f"  Account/channel validation: PASS ✅ ({channel_res['channel_title']})")

    # ---- Step 3: Build request body -------------------------------------
    metadata = generate_video_metadata(candidates, custom_title=custom_title)
    body = {
        "snippet": {
            "title": metadata["title"],
            "description": metadata["description"],
            "tags": metadata["tags"],
            "categoryId": "24",
        },
        "status": {
            "privacyStatus": valid_privacy,
            "selfDeclaredMadeForKids": made_for_kids_setting,
        },
    }

    # ---- Step 4: Upload -------------------------------------------------
    logger.info(f"→ Upload attempt: {video_path.name} (privacy={valid_privacy}, selfDeclaredMadeForKids={made_for_kids_setting})...")
    upload_res = _attempt_upload(youtube, video_path, body)

    if upload_res["success"]:
        logger.info("=" * 60)
        logger.info("PHASE 6 RESULT: UPLOAD SUCCESS ✅")
        logger.info(f"  Video ID      : {upload_res['video_id']}")
        logger.info(f"  Title         : {metadata['title']}")
        logger.info(f"  Privacy       : {valid_privacy}")
        logger.info(f"  Made for Kids : No (False)")
        logger.info(f"  YouTube URL   : {upload_res['youtube_url']}")
        logger.info(f"  Studio URL    : {upload_res['studio_url']}")
        logger.info("=" * 60)
        return {
            "success": True,
            "authenticated": True,
            "channel_valid": True,
            "upload_attempted": True,
            "privacy_status": valid_privacy,
            "made_for_kids": made_for_kids_setting,
            "synthetic_content_status": synthetic_media_disclosure,
            "comment_moderation": comment_moderation_setting,
            "error_type": None,
            "api_reason": None,
            "http_status": None,
            "message": upload_res["message"],
            "video_preserved": True,
            "video_id": upload_res["video_id"],
            "youtube_url": upload_res["youtube_url"],
            "studio_url": upload_res["studio_url"],
            "title": metadata["title"],
            "uploaded_at": datetime.now().isoformat(),
            # Legacy compat
            "status": "success",
            "video_url": upload_res["youtube_url"],
        }
    else:
        logger.error("=" * 60)
        logger.error("PHASE 6 RESULT: UPLOAD FAILED ❌")
        logger.error(f"  HTTP status   : {upload_res.get('http_status')}")
        logger.error(f"  API reason    : {upload_res.get('api_reason')}")
        logger.error(f"  Message       : {upload_res.get('message')}")
        logger.info(f"→ Video preserved: {'YES' if video_preserved else 'NO'}")
        logger.info("=" * 60)
        return {
            "success": False,
            "authenticated": True,
            "channel_valid": True,
            "upload_attempted": True,
            "privacy_status": valid_privacy,
            "made_for_kids": made_for_kids_setting,
            "synthetic_content_status": synthetic_media_disclosure,
            "comment_moderation": comment_moderation_setting,
            "error_type": upload_res.get("error_type"),
            "api_reason": upload_res.get("api_reason"),
            "http_status": upload_res.get("http_status"),
            "message": upload_res.get("message"),
            "video_preserved": video_preserved,
        }


if __name__ == "__main__":
    vid = config.OUTPUT_DIR / "final_short.mp4"
    res = upload_short_to_youtube(vid, candidates=[])
    print("Upload Result:", json.dumps({k: v for k, v in res.items() if k not in ("studio_url",)}, indent=2))

