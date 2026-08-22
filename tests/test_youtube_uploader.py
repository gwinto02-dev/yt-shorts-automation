"""
Phase 6 YouTube Uploader — Unit Tests

All YouTube API calls are mocked.
No real credentials are required or used.
No credentials are printed in any test output.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from googleapiclient.errors import HttpError

from src.youtube_uploader import (
    validate_privacy_status,
    validate_youtube_channel,
    upload_short_to_youtube,
    _is_permanent_error,
    _extract_api_reason,
    ALLOWED_PRIVACY_STATUSES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_http_error(status: int, reason: str, message: str = "error") -> HttpError:
    """Build a fake googleapiclient HttpError with the given status and reason body."""
    resp = MagicMock()
    resp.status = status
    content = json.dumps({
        "error": {
            "code": status,
            "message": message,
            "errors": [{"reason": reason, "message": message}],
        }
    }).encode("utf-8")
    return HttpError(resp=resp, content=content)


def _fake_video(tmp_path: Path) -> Path:
    """Create a tiny placeholder video file for tests that need one."""
    p = tmp_path / "final_short.mp4"
    p.write_bytes(b"\x00" * 64)
    return p


# ---------------------------------------------------------------------------
# Privacy guardrail tests
# ---------------------------------------------------------------------------

class TestPrivacyGuardrail(unittest.TestCase):
    """The privacy guardrail must be irremovable."""

    def test_private_allowed(self):
        """'private' is an accepted status."""
        self.assertEqual(validate_privacy_status("private"), "private")

    def test_private_case_insensitive(self):
        """Guard normalises case."""
        self.assertEqual(validate_privacy_status("PRIVATE"), "private")

    def test_scheduled_allowed(self):
        """'scheduled' is an accepted status."""
        self.assertEqual(validate_privacy_status("scheduled"), "scheduled")

    def test_public_rejected(self):
        """'public' must always raise ValueError — no exception."""
        with self.assertRaises(ValueError) as ctx:
            validate_privacy_status("public")
        self.assertIn("SAFETY GUARDRAIL VIOLATION", str(ctx.exception))

    def test_unlisted_rejected(self):
        """'unlisted' is not in the allowed list."""
        with self.assertRaises(ValueError):
            validate_privacy_status("unlisted")

    def test_allowed_statuses_never_include_public(self):
        """Guarantee public is never accidentally added to ALLOWED_PRIVACY_STATUSES."""
        self.assertNotIn("public", ALLOWED_PRIVACY_STATUSES)


# ---------------------------------------------------------------------------
# Permanent error detection
# ---------------------------------------------------------------------------

class TestPermanentErrorDetection(unittest.TestCase):

    def test_youtube_signup_required_is_permanent(self):
        err = _make_http_error(401, "youtubeSignupRequired")
        self.assertTrue(_is_permanent_error(err))

    def test_invalid_credentials_is_permanent(self):
        err = _make_http_error(401, "invalidCredentials")
        self.assertTrue(_is_permanent_error(err))

    def test_forbidden_is_permanent(self):
        err = _make_http_error(403, "forbidden")
        self.assertTrue(_is_permanent_error(err))

    def test_500_is_not_permanent(self):
        err = _make_http_error(500, "backendError")
        self.assertFalse(_is_permanent_error(err))

    def test_503_is_not_permanent(self):
        err = _make_http_error(503, "serviceUnavailable")
        self.assertFalse(_is_permanent_error(err))

    def test_reason_extraction(self):
        err = _make_http_error(401, "youtubeSignupRequired", "Signup required")
        self.assertEqual(_extract_api_reason(err), "youtubeSignupRequired")


# ---------------------------------------------------------------------------
# Channel validation tests
# ---------------------------------------------------------------------------

class TestChannelValidation(unittest.TestCase):

    def _make_youtube_with_channel(self, channel_id="UC123", title="Test Channel"):
        youtube = MagicMock()
        youtube.channels().list().execute.return_value = {
            "items": [{
                "id": channel_id,
                "snippet": {"title": title},
                "status": {"isLinked": True}
            }]
        }
        return youtube

    def test_valid_channel_passes(self):
        """A proper channel response marks validation as valid."""
        youtube = self._make_youtube_with_channel("UC_TEST", "My Anime Channel")
        res = validate_youtube_channel(youtube)
        self.assertTrue(res["valid"])
        self.assertEqual(res["channel_id"], "UC_TEST")
        self.assertEqual(res["channel_title"], "My Anime Channel")
        self.assertIsNone(res["error_type"])

    def test_empty_channel_list_fails(self):
        """An account with no channel is not valid."""
        youtube = MagicMock()
        youtube.channels().list().execute.return_value = {"items": []}
        res = validate_youtube_channel(youtube)
        self.assertFalse(res["valid"])
        self.assertEqual(res["error_type"], "no_channel_found")

    def test_youtube_signup_required_on_channel_check(self):
        """youtubeSignupRequired during channel check → valid=False with clear message."""
        youtube = MagicMock()
        youtube.channels().list().execute.side_effect = _make_http_error(
            401, "youtubeSignupRequired", "Sign up required"
        )
        res = validate_youtube_channel(youtube)
        self.assertFalse(res["valid"])
        self.assertEqual(res["api_reason"], "youtubeSignupRequired")
        self.assertEqual(res["http_status"], 401)
        self.assertIn("youtubeSignupRequired", res["message"])
        # Message must explain the situation without exposing secrets
        self.assertNotIn("token", res["message"].lower())

    def test_http_401_on_channel_check(self):
        """Generic HTTP 401 → valid=False."""
        youtube = MagicMock()
        youtube.channels().list().execute.side_effect = _make_http_error(
            401, "unauthorized"
        )
        res = validate_youtube_channel(youtube)
        self.assertFalse(res["valid"])
        self.assertEqual(res["http_status"], 401)


# ---------------------------------------------------------------------------
# upload_short_to_youtube integration tests
# ---------------------------------------------------------------------------

class TestUploadShortToYoutube(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.mkdtemp()
        self._video = Path(self._tmpdir) / "final_short.mp4"
        self._video.write_bytes(b"\x00" * 128)

    # ---- No credentials ------------------------------------------------

    @patch("src.youtube_uploader.get_youtube_client", return_value=None)
    def test_no_credentials_returns_dry_run(self, _mock):
        """Missing credentials → dry-run, no upload attempted, success=False."""
        res = upload_short_to_youtube(self._video, candidates=[], privacy_status="private")
        self.assertFalse(res["success"])
        self.assertFalse(res["authenticated"])
        self.assertFalse(res["upload_attempted"])
        self.assertEqual(res["error_type"], "no_credentials")
        # Dry-run must never report a real YouTube URL
        self.assertNotIn("youtu.be/", res.get("youtube_url", "") or "")

    # ---- youtubeSignupRequired -----------------------------------------

    @patch("src.youtube_uploader.get_youtube_client")
    def test_youtube_signup_required_blocks_upload(self, mock_client):
        """youtubeSignupRequired during channel validation blocks upload."""
        youtube = MagicMock()
        youtube.channels().list().execute.side_effect = _make_http_error(
            401, "youtubeSignupRequired"
        )
        mock_client.return_value = youtube

        res = upload_short_to_youtube(self._video, candidates=[], privacy_status="private")
        self.assertFalse(res["success"])
        self.assertTrue(res["authenticated"])
        self.assertFalse(res["channel_valid"])
        self.assertFalse(res["upload_attempted"])
        self.assertEqual(res["api_reason"], "youtubeSignupRequired")
        self.assertEqual(res["http_status"], 401)
        self.assertIsNone(res.get("youtube_url"))
        self.assertIn("youtubeSignupRequired", res["message"])
        self.assertTrue(res["video_preserved"])

    # ---- HTTP 401 permanent failure ------------------------------------

    @patch("src.youtube_uploader.get_youtube_client")
    def test_http_401_during_upload_is_not_retried(self, mock_client):
        """A 401 during the upload itself is treated as permanent — not retried."""
        youtube = MagicMock()
        # Channel check passes
        youtube.channels().list().execute.return_value = {
            "items": [{"id": "UC123", "snippet": {"title": "Test"}, "status": {"isLinked": True}}]
        }
        # Upload raises 401
        youtube.videos().insert().next_chunk.side_effect = _make_http_error(401, "unauthorized")
        mock_client.return_value = youtube

        res = upload_short_to_youtube(self._video, candidates=[], privacy_status="private")
        self.assertFalse(res["success"])
        self.assertTrue(res["upload_attempted"])
        self.assertEqual(res["http_status"], 401)
        # next_chunk called once (no retry on permanent errors)
        self.assertEqual(youtube.videos().insert().next_chunk.call_count, 1)

    # ---- Permanent authorization failure --------------------------------

    @patch("src.youtube_uploader.get_youtube_client")
    def test_permanent_auth_failure_never_reports_url(self, mock_client):
        """Failed authentication must NEVER report a YouTube URL."""
        youtube = MagicMock()
        youtube.channels().list().execute.side_effect = _make_http_error(403, "forbidden")
        mock_client.return_value = youtube

        res = upload_short_to_youtube(self._video, candidates=[], privacy_status="private")
        self.assertFalse(res["success"])
        self.assertIsNone(res.get("youtube_url"))
        self.assertIsNone(res.get("video_id"))

    # ---- Transient failure with retry ----------------------------------

    @patch("src.youtube_uploader.time.sleep", return_value=None)  # no real sleep in tests
    @patch("src.youtube_uploader.get_youtube_client")
    def test_transient_500_is_retried_then_succeeds(self, mock_client, _sleep):
        """A transient 500 is retried and succeeds on second attempt."""
        youtube = MagicMock()
        youtube.channels().list().execute.return_value = {
            "items": [{"id": "UC123", "snippet": {"title": "Test"}, "status": {"isLinked": True}}]
        }
        # First call: 500 transient; second: success
        youtube.videos().insert().next_chunk.side_effect = [
            _make_http_error(500, "backendError"),
            (None, {"id": "abc123"}),
        ]
        mock_client.return_value = youtube

        res = upload_short_to_youtube(self._video, candidates=[], privacy_status="private")
        self.assertTrue(res["success"])
        self.assertEqual(res["video_id"], "abc123")

    @patch("src.youtube_uploader.time.sleep", return_value=None)
    @patch("src.youtube_uploader.get_youtube_client")
    def test_transient_failure_exhausts_retries(self, mock_client, _sleep):
        """If all retries are exhausted the result is success=False."""
        youtube = MagicMock()
        youtube.channels().list().execute.return_value = {
            "items": [{"id": "UC123", "snippet": {"title": "Test"}, "status": {"isLinked": True}}]
        }
        youtube.videos().insert().next_chunk.side_effect = _make_http_error(503, "serviceUnavailable")
        mock_client.return_value = youtube

        res = upload_short_to_youtube(self._video, candidates=[], privacy_status="private")
        self.assertFalse(res["success"])
        self.assertEqual(res.get("error_type"), "transient_api_error_exhausted")

    # ---- Successful private upload -------------------------------------

    @patch("src.youtube_uploader.get_youtube_client")
    def test_successful_private_upload(self, mock_client):
        """A successful upload returns success=True, video_id, URL, privacy=private."""
        youtube = MagicMock()
        youtube.channels().list().execute.return_value = {
            "items": [{"id": "UC123", "snippet": {"title": "Test Channel"}, "status": {"isLinked": True}}]
        }
        youtube.videos().insert().next_chunk.return_value = (None, {"id": "yt_video_id_123"})
        mock_client.return_value = youtube

        res = upload_short_to_youtube(self._video, candidates=[], privacy_status="private")
        self.assertTrue(res["success"])
        self.assertTrue(res["authenticated"])
        self.assertTrue(res["channel_valid"])
        self.assertTrue(res["upload_attempted"])
        self.assertEqual(res["privacy_status"], "private")
        self.assertEqual(res["video_id"], "yt_video_id_123")
        self.assertIn("yt_video_id_123", res["youtube_url"])
        self.assertIn("yt_video_id_123", res["studio_url"])
        self.assertTrue(res["video_preserved"])
        self.assertIsNone(res["error_type"])

    # ---- Public upload rejected ----------------------------------------

    def test_public_upload_rejected_before_any_api_call(self):
        """Attempting public upload raises ValueError before any network call."""
        with self.assertRaises(ValueError) as ctx:
            upload_short_to_youtube(self._video, candidates=[], privacy_status="public")
        self.assertIn("SAFETY GUARDRAIL VIOLATION", str(ctx.exception))

    @patch("src.youtube_uploader.get_youtube_client")
    def test_failed_auth_cannot_return_success_true(self, mock_client):
        """No matter what, a failed channel validation must return success=False."""
        youtube = MagicMock()
        youtube.channels().list().execute.side_effect = _make_http_error(
            401, "youtubeSignupRequired"
        )
        mock_client.return_value = youtube

        res = upload_short_to_youtube(self._video, candidates=[], privacy_status="private")
        # Core invariant
        self.assertFalse(res["success"],
            "A failed account validation MUST NOT return success=True under any circumstances.")

    @patch("src.youtube_uploader.get_youtube_client")
    def test_failed_channel_validation_prevents_upload_attempt(self, mock_client):
        """Channel validation failure must prevent the upload from being attempted."""
        youtube = MagicMock()
        youtube.channels().list().execute.return_value = {"items": []}
        mock_client.return_value = youtube

        res = upload_short_to_youtube(self._video, candidates=[], privacy_status="private")
        self.assertFalse(res["upload_attempted"])
        # videos().insert() must NOT have been called
        youtube.videos().insert.assert_not_called()

    # ---- Video missing -------------------------------------------------

    @patch("src.youtube_uploader.get_youtube_client")
    def test_missing_video_file_returns_structured_failure(self, mock_client):
        """Missing video file → structured failure, no upload attempt."""
        youtube = MagicMock()
        mock_client.return_value = youtube
        missing = Path("non_existent_video_12345.mp4")
        res = upload_short_to_youtube(missing, candidates=[], privacy_status="private")
        self.assertFalse(res["success"])
        self.assertFalse(res["upload_attempted"])
        self.assertEqual(res["error_type"], "video_file_missing")


if __name__ == "__main__":
    unittest.main()
