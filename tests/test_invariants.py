"""
Invariant guard tests — these enforce the hard rules that must NEVER be broken.
Any future change that breaks one of these tests is wrong, not the test.

Hard rules:
  - CONF_FLOOR >= 0.765, EV_FLOOR >= 0.005
  - BetResult: CASHED/DEAD only — no push, void, draw
  - No Polymarket — Kalshi only
  - slips:active never auto-expires (no TTL)
  - Settlement scores window matches the open-picks window
  - Sleep window consistent: 3 <= hour < 5 ET across all workers
"""
import ast
import re
from pathlib import Path
import pytest

ROOT = Path(__file__).parent.parent


# ── Floor constants ────────────────────────────────────────────────────────────

def test_pick_gate_conf_floor():
    """MIN_CONFIDENCE in pick_gate must be >= 0.765."""
    from src.engines.pick_gate import MIN_CONFIDENCE
    assert MIN_CONFIDENCE >= 0.765, (
        f"MIN_CONFIDENCE={MIN_CONFIDENCE} violates CONF_FLOOR >= 0.765"
    )


def test_pick_gate_ev_floor():
    """MIN_EV_PCT_GATE must be >= 0.005."""
    from src.engines.pick_gate import MIN_EV_PCT_GATE
    assert MIN_EV_PCT_GATE >= 0.005, (
        f"MIN_EV_PCT_GATE={MIN_EV_PCT_GATE} violates EV_FLOOR >= 0.005"
    )


def test_prediction_market_conf_floor():
    """_CONF_FLOOR in prediction_market_worker must resolve to >= 0.765.
    It can be a literal or imported from picks_worker.CONF_FLOOR."""
    src = (ROOT / "src/workers/prediction_market_worker.py").read_text()
    m = re.search(r"_CONF_FLOOR\s*=\s*([\d.]+)", src)
    if m:
        val = float(m.group(1))
        assert val >= 0.765, f"_CONF_FLOOR={val} violates CONF_FLOOR >= 0.765"
    else:
        # Imported from picks_worker — check source constant
        assert "CONF_FLOOR" in src, "_CONF_FLOOR not defined or imported"
        from src.workers.picks_worker import CONF_FLOOR
        assert CONF_FLOOR >= 0.765, f"picks_worker CONF_FLOOR={CONF_FLOOR} violates >= 0.765"


def test_prediction_market_ev_floor():
    """_EV_FLOOR in prediction_market_worker must resolve to >= 0.005."""
    src = (ROOT / "src/workers/prediction_market_worker.py").read_text()
    m = re.search(r"_EV_FLOOR\s*=\s*([\d.]+)", src)
    if m:
        val = float(m.group(1))
        assert val >= 0.005, f"_EV_FLOOR={val} violates EV_FLOOR >= 0.005"
    else:
        assert "EV_FLOOR" in src, "_EV_FLOOR not defined or imported"
        from src.workers.picks_worker import EV_FLOOR
        assert EV_FLOOR >= 0.005, f"picks_worker EV_FLOOR={EV_FLOOR} violates >= 0.005"


# ── No Polymarket ──────────────────────────────────────────────────────────────

def test_no_polymarket_in_source():
    """No source file may reference Polymarket."""
    hits = []
    for f in ROOT.glob("src/**/*.py"):
        text = f.read_text(errors="replace")
        if re.search(r"polymarket", text, re.IGNORECASE):
            hits.append(str(f.relative_to(ROOT)))
    assert not hits, f"Polymarket references found (Kalshi only): {hits}"


# ── BetResult: no push/void/draw ──────────────────────────────────────────────

def test_settlement_totals_exact_line_is_lost():
    """Exact total score = lost, never push."""
    from src.workers.settlement_worker import _determine_result
    # Build a minimal pick-like object inline to avoid import coupling
    class _FakePick:
        selection = "Over 221.5"
        market = "totals"
        american_odds_at_gen = -110
        units = 1.0
    score = {
        "completed": True, "status": "", "push": False,
        "winner": None, "total_scored": 221.5,
        "home_score": 110.75, "away_score": 110.75, "home_team": "Lakers",
    }
    result = _determine_result(_FakePick(), None, score)
    assert result not in ("push", "void", "draw"), (
        f"Exact total should be 'lost', got {result!r}"
    )
    assert result.value.lower() in ("lost", "dead") if hasattr(result, "value") else str(result).lower() in ("lost", "dead"), (
        f"Expected lost/dead for exact total, got {result!r}"
    )


def test_settlement_spreads_exact_is_lost():
    """Exact spread cover = lost, never push."""
    from src.workers.settlement_worker import _determine_result
    from tests.workers.test_settlement_comprehensive import _make_pick
    pick = _make_pick(selection="Lakers -5.0")
    pick.market = "spreads"
    score = {
        "completed": True, "status": "", "push": False,
        "winner": "Lakers", "home_score": 110.0, "away_score": 105.0,
        "home_team": "Lakers", "total_scored": 215.0,
    }
    result = _determine_result(pick, "Lakers", score)
    assert result not in ("push", "void", "draw"), (
        f"Exact spread should be 'lost', got {result!r}"
    )


def test_prop_engine_exact_line_is_lost():
    """Prop outcome logic: when actual == line it must produce 'lost', not 'push'."""
    # Check the source directly — the outcome assignment is pure logic, no DB needed
    src = (ROOT / "src/engines/prop_engine.py").read_text()
    # Find the else branch after the line comparison
    m = re.search(r"actual_value\s*[<>]=?\s*line.*?else:\s*\n\s*outcome\s*=\s*['\"](\w+)['\"]",
                  src, re.DOTALL)
    if m:
        outcome = m.group(1)
        assert outcome != "push", (
            f"prop_engine sets outcome='{outcome}' when actual==line — must be 'lost'"
        )
    else:
        # Fallback: ensure 'push' doesn't appear as an outcome value
        for line in src.splitlines():
            if 'outcome' in line and '= "push"' in line:
                pytest.fail(f"prop_engine still sets outcome='push': {line.strip()}")


def test_summary_engine_no_push_in_settled():
    """summary_engine settled_rows must not include push results."""
    src = (ROOT / "src/engines/summary_engine.py").read_text()
    assert '"push"' not in src or 'settled_rows' not in src.split('"push"')[0].split('\n')[-1], \
        "summary_engine settled_rows still includes 'push' — ROI will be wrong"
    # Stronger: ensure the settled_rows line doesn't have push
    for line in src.splitlines():
        if "settled_rows" in line and "push" in line:
            pytest.fail(f"settled_rows includes push: {line.strip()}")


def test_portfolio_engine_no_push_in_filter():
    """portfolio_engine Pick.result filter must not include push."""
    src = (ROOT / "src/engines/portfolio_engine.py").read_text()
    for line in src.splitlines():
        if "result.in_" in line and "push" in line:
            pytest.fail(f"portfolio_engine result filter includes push: {line.strip()}")


# ── slips:active must never have a TTL ────────────────────────────────────────

def test_slip_tracker_no_expire_on_slip_key():
    """save_slip must not call r.expire on the slips:active hash (only persist is allowed)."""
    src = (ROOT / "src/workers/slip_tracker.py").read_text()
    in_save_slip = False
    for line in src.splitlines():
        if "def save_slip(" in line:
            in_save_slip = True
        elif in_save_slip and line.startswith("def "):
            break
        elif in_save_slip and "r.expire" in line and "_SLIP_KEY" in line:
            pytest.fail(
                f"save_slip calls r.expire on _SLIP_KEY — this wipes all slips after TTL: {line.strip()}"
            )


# ── Settlement scores window matches open-picks window ────────────────────────

def test_settlement_fetch_scores_window():
    """fetch_scores days_from must be >= the open-picks query window (14 days)."""
    src = (ROOT / "src/workers/settlement_worker.py").read_text()
    m = re.search(r"fetch_scores\(\s*\w+\s*,\s*days_from=(\d+)", src)
    assert m, "fetch_scores call not found in settlement_worker.py"
    days = int(m.group(1))
    assert days >= 14, (
        f"fetch_scores days_from={days} is less than the 14-day open-picks window — "
        "picks from days 4-14 will never settle"
    )


# ── Sleep window consistency ───────────────────────────────────────────────────

def test_sleep_window_consistency():
    """All workers that gate on sleep time must use 3 <= hour < 5."""
    pattern = re.compile(r"([\d]+)\s*<=?\s*(?:et\.hour|now\.hour|hour)\s*<\s*([\d]+)")
    files_to_check = [
        "src/workers/odds_worker.py",
        "runner.py",
    ]
    for rel in files_to_check:
        src = (ROOT / rel).read_text()
        for m in pattern.finditer(src):
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo in (2, 3, 4) and hi in (5, 6):
                assert lo == 3 and hi == 5, (
                    f"{rel}: sleep window is {lo}–{hi}, expected 3–5 "
                    f"(near: {src[max(0,m.start()-30):m.end()+30]!r})"
                )


# ── expiration_engine imports et_now ──────────────────────────────────────────

def test_expiration_engine_imports_et_now():
    """expiration_engine must import et_now before using it."""
    src = (ROOT / "src/engines/expiration_engine.py").read_text()
    assert "et_now" in src, "et_now not found in expiration_engine.py"
    # Ensure it's imported, not just called
    import_lines = [l for l in src.splitlines() if "import" in l and "et_now" in l]
    assert import_lines, "et_now is used but never imported in expiration_engine.py"


# ── action_network: no wrong default sport ────────────────────────────────────

def test_action_network_no_default_sport():
    """get_consensus must not fall back to NBA for unknown sport keys."""
    src = (ROOT / "src/apis/action_network.py").read_text()
    assert 'SPORT_MAP.get(sport_key, "NBA")' not in src, (
        "action_network.get_consensus uses 'NBA' as default — "
        "unknown sport keys silently query NBA consensus"
    )


# ── line_movement_engine: cutoff must be used ────────────────────────────────

def test_line_movement_cutoff_used():
    """detect_movements must filter snapshots by cutoff, not ignore it."""
    src = (ROOT / "src/engines/line_movement_engine.py").read_text()
    # Find detect_movements body — cutoff must appear in a filter/comparison after its definition
    fn_start = src.find("def detect_movements(")
    fn_src = src[fn_start:fn_start + 800]
    cutoff_def = fn_src.find("cutoff =")
    assert cutoff_def != -1, "cutoff not defined in detect_movements"
    after_def = fn_src[cutoff_def:]
    # cutoff_str or cutoff must appear in a filter context after its definition
    assert "cutoff_str" in after_def or (
        after_def.count("cutoff") > 1
    ), "cutoff is defined but never used to filter snapshots in detect_movements"


# ── HardRock entry: 2-pick cap and 77% floor for all markets ─────────────────

def test_hardrock_entry_cap_is_2():
    """HardRock entry must cap at exactly 2 picks, never 5."""
    src = (ROOT / "src/workers/picks_worker.py").read_text()
    assert 'if len(entry) == 5:' not in src, (
        "HardRock entry cap is still 5 — must be 2"
    )
    assert 'if len(entry) == 2:' in src, (
        "HardRock entry cap of 2 not found in picks_worker.py"
    )


def test_hardrock_conf_floor_all_markets():
    """CONF_FLOOR must apply to all markets, not just h2h (no 0.65 fallback)."""
    src = (ROOT / "src/workers/picks_worker.py").read_text()
    for line in src.splitlines():
        if '_min_prob' in line and '0.65' in line:
            pytest.fail(
                f"picks_worker uses 0.65 floor for non-h2h markets — all markets must use CONF_FLOOR (0.765): {line.strip()}"
            )


def test_hardrock_paused_until_july_10():
    """generate_hardrock_day/night must be gated until July 10, 2026."""
    src = (ROOT / "src/workers/picks_worker.py").read_text()
    assert '2026, 7, 10' in src, (
        "HardRock pause until July 10, 2026 not found in picks_worker.py"
    )


# ── Kalshi (prediction market) entry guards ───────────────────────────────────

def test_kalshi_entry_cap_is_1():
    """Kalshi _build_entry must cap at max_picks=1."""
    src = (ROOT / "src/workers/prediction_market_worker.py").read_text()
    # Every call site (not the def) must pass max_picks=1
    for line in src.splitlines():
        stripped = line.strip()
        if "_build_entry(" in stripped and "max_picks" in stripped and not stripped.startswith("def "):
            assert "max_picks=1" in stripped, (
                f"Kalshi _build_entry called with wrong max_picks: {stripped}"
            )
    # Default in the signature must also be 1
    import re as _re
    m = _re.search(r"def _build_entry\([^)]*max_picks\s*=\s*(\d+)", src)
    if m:
        assert int(m.group(1)) == 1, (
            f"Kalshi _build_entry default max_picks={m.group(1)}, must be 1"
        )


def test_kalshi_conf_floor_no_65_fallback():
    """Kalshi prediction_market_worker must not use 0.65 as a confidence fallback."""
    src = (ROOT / "src/workers/prediction_market_worker.py").read_text()
    for line in src.splitlines():
        if "0.65" in line and "conf" in line.lower():
            pytest.fail(
                f"prediction_market_worker uses 0.65 conf floor: {line.strip()}"
            )


def test_kalshi_uses_imported_conf_floor():
    """Kalshi must import and use CONF_FLOOR from picks_worker, not hardcode a lower value."""
    src = (ROOT / "src/workers/prediction_market_worker.py").read_text()
    assert "CONF_FLOOR" in src, "CONF_FLOOR not referenced in prediction_market_worker.py"
    assert "_CONF_FLOOR" in src or "CONF_FLOOR" in src, (
        "Kalshi entry does not gate on CONF_FLOOR"
    )
