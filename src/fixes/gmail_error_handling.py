"""Gmail API error handling and recovery.

Wraps gmail_search and gmail_read calls with:
- Exception handling for TaskGroup errors
- Draft validation before read attempts
- Stale draft ID cleanup
"""
import logging
from typing import Any
import asyncio

log = logging.getLogger(__name__)


class GmailErrorHandler:
    """Handles Gmail API errors with recovery strategies."""
    
    def __init__(self, max_retries: int = 3, backoff_base: float = 2.0):
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.stale_ids: set[str] = set()
    
    async def search_with_retry(self, search_func, query: str, **kwargs) -> Any:
        """Execute gmail_search with exponential backoff on TaskGroup errors."""
        for attempt in range(self.max_retries):
            try:
                result = await search_func(query, **kwargs) if asyncio.iscoroutinefunction(search_func) else search_func(query, **kwargs)
                return result
            except Exception as e:
                if "TaskGroup" in str(type(e)) or "unhandled" in str(e).lower():
                    if attempt < self.max_retries - 1:
                        wait = self.backoff_base ** attempt
                        log.warning(
                            f"TaskGroup error in gmail_search (attempt {attempt+1}/{self.max_retries}). "
                            f"Retrying in {wait}s... Error: {e}"
                        )
                        await asyncio.sleep(wait)
                        continue
                # Re-raise if all retries exhausted or different error
                log.error(f"Gmail search failed after {self.max_retries} retries: {e}")
                raise
    
    async def read_with_validation(self, read_func, message_id: str, **kwargs) -> Any:
        """Read draft/message after checking if it exists."""
        # Skip if ID is known to be stale
        if message_id in self.stale_ids:
            log.debug(f"Skipping read of stale draft ID: {message_id}")
            return None
        
        try:
            result = await read_func(message_id, **kwargs) if asyncio.iscoroutinefunction(read_func) else read_func(message_id, **kwargs)
            return result
        except Exception as e:
            error_str = str(e).lower()
            if "404" in error_str or "not found" in error_str:
                # Mark as stale for future attempts
                self.stale_ids.add(message_id)
                log.warning(f"Draft {message_id} returned 404. Marked as stale.")
                return None
            raise
    
    def get_stale_ids(self) -> set[str]:
        """Return all IDs identified as stale/deleted."""
        return self.stale_ids.copy()
    
    def clear_stale_cache(self):
        """Clear stale ID cache (e.g., for long-running processes)."""
        self.stale_ids.clear()


# Recommended usage in direct_tools.py:

# At top level:
# _GMAIL_ERROR_HANDLER = GmailErrorHandler()

# Wrapper for gmail_search:
# async def gmail_search_safe(query: str, max_results: int = 10):
#     return await _GMAIL_ERROR_HANDLER.search_with_retry(
#         gmail_search, query, max_results=max_results
#     )

# Wrapper for gmail_read:
# async def gmail_read_safe(message_id: str):
#     return await _GMAIL_ERROR_HANDLER.read_with_validation(
#         gmail_read, message_id
#     )
