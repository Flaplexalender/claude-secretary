"""Safe Gmail API wrappers with error handling and retry logic.

Wraps gmail operations in try-catch, validates draft existence, and
handles TaskGroup exceptions gracefully.
"""
import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)


async def safe_gmail_search(gmail_func, query: str, max_results: int = 10, retries: int = 3) -> list[Any]:
    """Wrapper around gmail_search with TaskGroup error handling.
    
    Catches TaskGroup exceptions and retries with exponential backoff.
    """
    for attempt in range(retries):
        try:
            result = await asyncio.to_thread(gmail_func, {"query": query, "max_results": max_results})
            return result
        except asyncio.TaskGroup as e:
            backoff = 2 ** attempt
            log.warning(f"Gmail TaskGroup error on attempt {attempt + 1}: {e}. Retrying in {backoff}s...")
            if attempt < retries - 1:
                await asyncio.sleep(backoff)
            else:
                log.error(f"Gmail search failed after {retries} attempts: {e}")
                raise
        except Exception as e:
            log.error(f"Unexpected error in gmail_search: {e}")
            raise
    return []


async def validate_draft_exists(gmail_read_func, draft_id: str) -> bool:
    """Check if a draft still exists before attempting to read it.
    
    Drafts can be deleted/archived, causing 404 errors. This validation
    prevents spurious errors.
    """
    try:
        # Attempt a lightweight metadata check instead of full read
        result = await asyncio.to_thread(gmail_read_func, {"message_id": draft_id})
        return result is not None
    except Exception as e:
        if "404" in str(e):
            log.debug(f"Draft {draft_id} not found (404)")
            return False
        log.warning(f"Error validating draft {draft_id}: {e}")
        return False


def cleanup_old_drafts(draft_list: list[dict], max_age_days: int = 30) -> list[dict]:
    """Filter out drafts older than max_age_days.
    
    Old/stale drafts often return 404. This cleanup prevents them from
    cluttering the cache.
    """
    from datetime import datetime, timedelta
    cutoff = datetime.now() - timedelta(days=max_age_days)
    result = []
    for draft in draft_list:
        try:
            date_str = draft.get("date", "")
            date = datetime.fromisoformat(date_str.replace("Z", "+00:00")) if date_str else datetime.now()
            if date >= cutoff:
                result.append(draft)
            else:
                log.debug(f"Skipping draft older than {max_age_days} days: {draft.get('id')}")
        except (ValueError, AttributeError):
            # If date parse fails, keep the draft (safe default)
            result.append(draft)
    return result
