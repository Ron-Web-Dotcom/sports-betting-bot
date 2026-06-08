"""
Base HTTP client shared across all data source adapters.

Uses httpx with connection pooling, retry logic, and consistent
timeout/header defaults so every source behaves the same way.
"""
import logging
import threading
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, retry_if_result

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

_client: httpx.Client | None = None
_client_lock = threading.Lock()


def get_client() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.Client(
                headers=_HEADERS,
                timeout=httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0),
                follow_redirects=True,
                max_redirects=3,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return _client


class _RetryOn5xx(Exception):
    """Raised internally to trigger tenacity retry on 5xx responses."""


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type((httpx.TransportError, _RetryOn5xx)),
    reraise=True,
)
def get_json(url: str, params: dict | None = None, headers: dict | None = None) -> dict | list | None:
    try:
        r = get_client().get(url, params=params, headers=headers)
        if r.status_code >= 500:
            raise _RetryOn5xx(f"HTTP {r.status_code} from {url}")
        r.raise_for_status()
        return r.json()
    except _RetryOn5xx:
        raise
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        # 429 = rate limit, 403 = blocked — debug only, not warnings (they spam logs)
        level = logging.DEBUG if code in (429, 403) else logging.WARNING
        logger.log(level, "HTTP %s from %s", code, url)
        return None
    except Exception as e:
        logger.error("GET %s failed: %s", url, e)
        return None


def get_html(url: str, params: dict | None = None) -> str | None:
    try:
        r = get_client().get(url, params=params)
        r.raise_for_status()
        return r.text
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        level = logging.DEBUG if code in (429, 403) else logging.WARNING
        logger.log(level, "GET HTML %s failed: %s", code, url)
        return None
    except Exception as e:
        logger.error("GET HTML %s failed: %s", url, e)
        return None
