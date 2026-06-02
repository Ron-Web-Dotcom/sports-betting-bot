"""
Base HTTP client shared across all data source adapters.

Uses httpx with connection pooling, retry logic, and consistent
timeout/header defaults so every source behaves the same way.
"""
import logging
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

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


def get_client() -> httpx.Client:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.Client(
            headers=_HEADERS,
            timeout=httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0),
            follow_redirects=True,
            max_redirects=3,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _client


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type(httpx.TransportError),
    reraise=True,
)
def get_json(url: str, params: dict | None = None, headers: dict | None = None) -> dict | list | None:
    try:
        r = get_client().get(url, params=params, headers=headers)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        logger.warning("HTTP %s from %s", e.response.status_code, url)
        return None
    except Exception as e:
        logger.error("GET %s failed: %s", url, e)
        return None


def get_html(url: str, params: dict | None = None) -> str | None:
    try:
        r = get_client().get(url, params=params)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.error("GET HTML %s failed: %s", url, e)
        return None
