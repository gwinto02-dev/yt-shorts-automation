import logging
import os
import json
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request

import config

logger = logging.getLogger(__name__)

# HARD SAFETY GUARDRAIL: Only private or scheduled uploads are allowed!
ALLOWED_PRIVACY_STATUSES = ["private", "scheduled"]

def validate_privacy_status(status: str) -> str:
    """Enforce guardrail: video privacy status MUST NEVER be public."""
    clean_status = status.lower().strip()
    if clean_status not in ALLOWED_PRIVACY_STATUSES:
        raise ValueError(
            f"SAFETY GUARDRAIL VIOLATION: Privacy status '{status}' is strictly forbidden! "
            f"Allowed statuses: {ALLOWED_PRIVACY_STATUSES}. Uploads must default to Private/Scheduled."
        )
    return clean_status

def get_youtube_client():
    """Build YouTube API client from environment variables or token.json file."""
    creds = None
    token_file = config.BASE_DIR / "token.json"
    
    # 1. Try environment variables (for GitHub Actions runner)
    if config.YOUTUBE_REFRESH_TOKEN and config.YOUTUBE_CLIENT_ID and config.YOUTUBE_CLIENT_SECRET:
        logger.info("Authenticating YouTube API using environment refresh token...")
        creds = Credentials(
            token=None,
            refresh_token=config.YOUTUBE_REFRESH_TOKEN,
            client_id=config.YOUTUBE_CLIENT_ID,
            client_secret=config.YOUTUBE_CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/youtube.upload"]
        )
    # 2. Try local token.json
    elif token_file.exists():
        logger.info("Authenticating YouTube API using local token.json...")
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), scopes=["https://www.googleapis.com/auth/youtube.upload"])
        except Exception as e:
            logger.warning(f"Could not load token.json: {e}")
            
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            logger.warning(f"Could not refresh OAuth token: {e}")
            creds = None

    if not creds:
        return None
        
    return build("youtube", "v3", credentials=creds)

def generate_video_metadata(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Generate title, description, and tags for YouTube Short."""
    if candidates:
        titles_str = ", ".join([c["title"] for c in candidates[:3]])
        top_title = candidates[0]["title"]
    else:
        titles_str = "Must-Watch Titles"
        top_title = "Anime Recommendation"

    today_str = datetime.now().strftime("%b %d, %Y")
    
    title = f"Top {len(candidates)} Anime You Need To Watch Today! 🍿 ({today_str}) #Shorts"
    # Ensure title length <= 100 chars
    if len(title) > 100:
        title = f"Top Anime You Need To Watch Today! 🍿 #Shorts"

    description = (
        f"Looking for your next anime binge? Here are today's top recommendations:\n\n"
    )
    
    for idx, c in enumerate(candidates, 1):
        score = c.get("average_score", "N/A")
        cat = c.get("selection_category", "Must-Watch")
        description += f"{idx}. {c['title']} - [{cat}] (Rating: {score}/10)\n"
        
    description += (
        "\n\nSubscribe for daily anime recommendations and hidden gems!\n\n"
        "#Anime #AnimeShorts #AnimeRecommendation #AnimeEdit #Shorts #Manga"
    )
    
    tags = [
        "Anime", "Anime Shorts", "Anime Recommendation", "Must Watch Anime",
        "Top Anime", "Underrated Anime", "Best Anime", top_title
    ]
    
    return {
        "title": title,
        "description": description,
        "tags": tags
    }

def upload_short_to_youtube(video_path: Path, candidates: List[Dict[str, Any]], privacy_status: str = "private") -> Dict[str, Any]:
    """
    Uploads vertical video to YouTube Data API v3.
    Enforces Private/Scheduled guardrail.
    """
    valid_privacy = validate_privacy_status(privacy_status or config.YOUTUBE_PRIVACY_STATUS)
    
    if not video_path.exists():
        raise FileNotFoundError(f"Video file to upload not found: {video_path}")
        
    youtube = get_youtube_client()
    metadata = generate_video_metadata(candidates)
    
    if not youtube:
        logger.warning(
            "YouTube API credentials not configured (YOUTUBE_REFRESH_TOKEN missing). "
            "Skipping live upload and returning mock/dry-run response for testing."
        )
        return {
            "status": "dry_run_success",
            "video_id": "DRY_RUN_ID",
            "video_url": "https://youtu.be/DRY_RUN_ID",
            "studio_url": "https://studio.youtube.com/video/DRY_RUN_ID/edit",
            "privacy_status": valid_privacy,
            "title": metadata["title"],
            "uploaded_at": datetime.now().isoformat()
        }

    logger.info(f"Uploading {video_path.name} to YouTube with Privacy Status: '{valid_privacy}'...")

    body = {
        "snippet": {
            "title": metadata["title"],
            "description": metadata["description"],
            "tags": metadata["tags"],
            "categoryId": "24"  # Entertainment / Film & Animation
        },
        "status": {
            "privacyStatus": valid_privacy,
            "selfDeclaredMadeForKids": False
        }
    }

    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4")
    
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            logger.info(f"Upload progress: {int(status.progress() * 100)}%")

    video_id = response.get("id")
    video_url = f"https://youtu.be/{video_id}"
    studio_url = f"https://studio.youtube.com/video/{video_id}/edit"

    logger.info("=" * 60)
    logger.info("YOUTUBE UPLOAD SUCCESSFUL!")
    logger.info(f"  Video ID: {video_id}")
    logger.info(f"  Privacy Status: {valid_privacy}")
    logger.info(f"  YouTube Studio Review URL: {studio_url}")
    logger.info("=" * 60)

    return {
        "status": "success",
        "video_id": video_id,
        "video_url": video_url,
        "studio_url": studio_url,
        "privacy_status": valid_privacy,
        "title": metadata["title"],
        "uploaded_at": datetime.now().isoformat()
    }

if __name__ == "__main__":
    vid = config.OUTPUT_DIR / "final_short.mp4"
    res = upload_short_to_youtube(vid, candidates=[])
    print("Upload Result:", res)
