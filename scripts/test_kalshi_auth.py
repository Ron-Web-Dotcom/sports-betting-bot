#!/usr/bin/env python3
"""Quick Kalshi auth check — prints balance if key is valid."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from src.apis.kalshi import _sign_request, _BASE
from src.apis.base import get_json

headers = _sign_request("GET", "/trade-api/v2/portfolio/balance")
if not headers:
    print("FAIL — could not build auth headers (check KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY)")
    sys.exit(1)

result = get_json(f"{_BASE}/portfolio/balance", headers=headers)
if result is None:
    print("FAIL — request returned None (auth rejected or network error)")
    sys.exit(1)

balance = result.get("balance", result)
print(f"OK — Kalshi auth working. Balance: {balance}")
