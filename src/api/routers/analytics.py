"""Analytics endpoints — ROI, CLV, performance by dimension."""
from fastapi import APIRouter, Query
from src.engines.self_improvement_engine import compute_roi_by_dimension, get_top_performing_categories
from src.engines.clv_engine import aggregate_clv, clv_by_dimension
from src.engines.portfolio_engine import get_performance_stats

router = APIRouter()

PERIODS = ["daily", "weekly", "monthly", "lifetime"]
DIMENSIONS = ["sport", "market", "best_book", "units"]


@router.get("/performance")
def performance(period: str = Query("weekly", enum=PERIODS)):
    return get_performance_stats(period)


@router.get("/roi")
def roi_by_dimension(
    dimension: str = Query("sport", enum=DIMENSIONS),
    period: str = Query("lifetime", enum=PERIODS),
):
    return compute_roi_by_dimension(dimension, period)


@router.get("/clv")
def clv(period: str = Query("weekly", enum=PERIODS)):
    return aggregate_clv(period)


@router.get("/clv/dimension")
def clv_dimension(
    dimension: str = Query("sport", enum=DIMENSIONS),
    period: str = Query("lifetime", enum=PERIODS),
):
    return clv_by_dimension(dimension, period)


@router.get("/top-categories")
def top_categories(limit: int = Query(10, le=50)):
    return get_top_performing_categories(limit)
