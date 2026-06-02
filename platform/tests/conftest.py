"""Shared fixtures for platform tests."""
import pytest
import os

os.environ.setdefault("USE_SQLITE", "true")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ODDS_API_KEY", "test-key")
os.environ.setdefault("DISCORD_BOT_TOKEN", "")
os.environ.setdefault("DISCORD_GUILD_ID", "0")
