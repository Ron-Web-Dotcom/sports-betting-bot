"""
One-time record reset — run once to start fresh at 0W-0L.

Usage:
    python scripts/reset_record.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.workers.slip_tracker import reset_record

if __name__ == "__main__":
    reset_record()
    print("✅ Record reset to 0W-0L (all-time and weekly).")
    print("   First W/L will count from today's day and night entries.")
