"""Google OAuth2 credential management for Gmail, Calendar & Drive.

Token + credentials stored in data_root. Scopes cover Gmail, Calendar, and Drive.
Run `secretary auth` to set up interactively.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive.readonly",
]

_DEFAULT_DATA_ROOT = Path("data")


def _token_path(data_root: Path | None = None) -> Path:
    return (data_root or _DEFAULT_DATA_ROOT) / "google_token.json"


def _creds_path(data_root: Path | None = None) -> Path:
    return (data_root or _DEFAULT_DATA_ROOT) / "google_credentials.json"


def get_credentials(data_root: Path | None = None) -> Credentials:
    """Load stored OAuth credentials. Refresh if expired."""
    from google.oauth2.credentials import Credentials as OAuthCreds
    from google.auth.transport.requests import Request

    token_file = _token_path(data_root)
    if not token_file.exists():
        raise FileNotFoundError(
            f"No Google token at {token_file}. Run `secretary auth` first."
        )

    creds = OAuthCreds.from_authorized_user_file(str(token_file), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_file.write_text(creds.to_json(), encoding="utf-8")
        except Exception as e:
            raise RuntimeError(
                f"Failed to refresh Google credentials: {e}. "
                "Run `secretary auth` to re-authenticate."
            ) from e
    return creds


def run_oauth_flow(data_root: Path | None = None) -> Credentials:
    """Run interactive browser-based OAuth flow."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds_file = _creds_path(data_root)
    if not creds_file.exists():
        raise FileNotFoundError(
            f"No credentials file at {creds_file}. "
            "Download from Google Cloud Console → APIs & Services → Credentials."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
    creds = flow.run_local_server(port=0)

    token_file = _token_path(data_root)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_gmail_service(data_root: Path | None = None):
    """Build an authenticated Gmail API v1 client.

    Args:
        data_root: Directory containing google_token.json. Defaults to data/.

    Returns:
        A googleapiclient Resource for the Gmail API.

    Raises:
        FileNotFoundError: If no OAuth token exists (run ``secretary auth``).
        RuntimeError: If the token is expired and cannot be refreshed.
    """
    from googleapiclient.discovery import build

    creds = get_credentials(data_root)
    return build("gmail", "v1", credentials=creds)


def build_calendar_service(data_root: Path | None = None):
    """Build an authenticated Google Calendar API v3 client.

    Args:
        data_root: Directory containing google_token.json. Defaults to data/.

    Returns:
        A googleapiclient Resource for the Calendar API.

    Raises:
        FileNotFoundError: If no OAuth token exists (run ``secretary auth``).
        RuntimeError: If the token is expired and cannot be refreshed.
    """
    from googleapiclient.discovery import build

    creds = get_credentials(data_root)
    return build("calendar", "v3", credentials=creds)


def build_drive_service(data_root: Path | None = None):
    """Build an authenticated Google Drive API v3 client.

    Args:
        data_root: Directory containing google_token.json. Defaults to data/.

    Returns:
        A googleapiclient Resource for the Drive API.

    Raises:
        FileNotFoundError: If no OAuth token exists (run ``secretary auth``).
        RuntimeError: If the token is expired and cannot be refreshed.
    """
    from googleapiclient.discovery import build

    creds = get_credentials(data_root)
    return build("drive", "v3", credentials=creds)
