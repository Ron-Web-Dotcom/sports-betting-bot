"""
Base HTTP client shared across all data source adapters.

Uses httpx with connection pooling, retry logic, and consistent
timeout/header defaults so every source behaves the same way.

Proxy routing (Decodo residential):
  - Enabled when DECODO_PROXY_URL is set in .env
  - Rotates across 10 sticky endpoints (ports 10001-10010) per request
  - Bypassed for proper keyed APIs (Odds API, Kalshi, OpenAI, Sleeper)
"""
import itertools
import logging
import threading
from urllib.parse import urlparse

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

# Proxy port pool — Decodo sticky endpoints 10001–10010
_PROXY_PORTS = list(range(10001, 10011))
_port_cycle  = itertools.cycle(_PROXY_PORTS)   # round-robin across all 10 ports
_cycle_lock  = threading.Lock()

# Direct client (no proxy)
_direct_client: httpx.Client | None = None
_direct_lock = threading.Lock()

# Per-(proxy_base_url, port) proxy clients — one persistent client per port
_proxy_clients: dict[tuple, httpx.Client] = {}
_proxy_lock = threading.Lock()


def _next_port() -> int:
    """Thread-safe round-robin port selection across 10001–10010."""
    with _cycle_lock:
        return next(_port_cycle)


def _get_direct_client() -> httpx.Client:
    global _direct_client
    with _direct_lock:
        if _direct_client is None or _direct_client.is_closed:
            _direct_client = httpx.Client(
                headers=_HEADERS,
                timeout=httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0),
                follow_redirects=True,
                max_redirects=3,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return _direct_client


def _get_proxy_client(proxy_base_url: str) -> httpx.Client:
    """Round-robin across ports 10001–10010, one persistent client per port."""
    port = _next_port()
    key  = (proxy_base_url, port)
    with _proxy_lock:
        if key not in _proxy_clients or _proxy_clients[key].is_closed:
            _proxy_clients[key] = httpx.Client(
                headers=_HEADERS,
                timeout=httpx.Timeout(connect=8.0, read=25.0, write=5.0, pool=5.0),
                follow_redirects=True,
                max_redirects=3,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                proxy=f"{proxy_base_url}:{port}",
            )
        return _proxy_clients[key]


def _should_proxy(url: str) -> bool:
    """Return True if this URL should be routed through the proxy."""
    from src.core.config import DECODO_PROXY_URL, PROXY_BYPASS_HOSTS
    if not DECODO_PROXY_URL:
        return False
    host = urlparse(url).netloc.split(":")[0]
    return host not in PROXY_BYPASS_HOSTS


def get_client(url: str = "") -> httpx.Client:
    """Return the appropriate client for the given URL (proxied or direct)."""
    if url and _should_proxy(url):
        from src.core.config import DECODO_PROXY_URL
        return _get_proxy_client(DECODO_PROXY_URL)
    return _get_direct_client()


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
        r = get_client(url).get(url, params=params, headers=headers)
        if r.status_code >= 500:
            raise _RetryOn5xx(f"HTTP {r.status_code} from {url}")
        r.raise_for_status()
        return r.json()
    except _RetryOn5xx:
        raise
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        level = logging.DEBUG if code in (429, 403) else logging.WARNING
        logger.log(level, "HTTP %s from %s", code, url)
        return None
    except Exception as e:
        logger.error("GET %s failed: %s", url, e)
        return None


def get_html(url: str, params: dict | None = None) -> str | None:
    try:
        r = get_client(url).get(url, params=params)
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
