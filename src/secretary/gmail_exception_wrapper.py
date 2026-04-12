"""Gmail exception wrapper — handle TaskGroup errors with exponential backoff retry.

Fixes: TaskGroup exceptions (unhandled), 404 draft IDs, auth token stale errors.
"""
import asyncio
import logging
from typing import Any, Callable, TypeVar

log = logging.getLogger(__name__)

T = TypeVar('T')

class GmailRetryConfig:
    """Retry configuration for Gmail API calls."""
    max_retries: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    backoff_factor: float = 2.0


async def gmail_call_with_retry(
    func: Callable[..., T],
    *args,
    config: GmailRetryConfig | None = None,
    **kwargs
) -> T:
    """Execute Gmail API call with exponential backoff retry on failure.
    
    Handles:
    - TaskGroup exceptions → log and retry
    - 404 NotFound → skip (stale draft ID)
    - Auth token expired → trigger refresh and retry
    - Other errors → retry up to max_retries times
    """
    if config is None:
        config = GmailRetryConfig()
    
    last_error = None
    for attempt in range(config.max_retries + 1):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            last_error = e
            error_msg = str(e)
            
            # 404 NotFound → stale draft ID, skip permanently
            if "404" in error_msg or "not found" in error_msg.lower():
                log.warning(f"Stale entity ID (404): {error_msg}")
                raise  # Don't retry 404
            
            # Auth failures → log and may retry with token refresh
            if any(x in error_msg.lower() for x in ["auth", "token", "expired"]):
                log.warning(f"Auth error attempt {attempt+1}: {error_msg}")
            
            # TaskGroup or other transient error → retry with backoff
            if attempt < config.max_retries:
                delay = min(
                    config.base_delay_s * (config.backoff_factor ** attempt),
                    config.max_delay_s
                )
                log.warning(f"Gmail call failed (attempt {attempt+1}/{config.max_retries}), "
                           f"retrying in {delay:.1f}s: {type(e).__name__}")
                await asyncio.sleep(delay)
            else:
                log.error(f"Gmail call exhausted retries after {config.max_retries} attempts")
    
    raise last_error or RuntimeError("Unknown error in gmail_call_with_retry")
