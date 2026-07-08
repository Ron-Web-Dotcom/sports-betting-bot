"""
Post full W/L record + tracklist to Discord.

Usage:
    python scripts/post_record.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.workers.slip_tracker import post_record_to_discord

if __name__ == "__main__":
    post_record_to_discord()
    print("✅ Record posted to Discord.")
