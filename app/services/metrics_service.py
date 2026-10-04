"""The exposition, with the per-show gauge read from the database at scrape time.

A refresh timer would lag the API by up to its interval, and "metrics reconcile with
API state" (REQ-043) would then be true only on average.
"""

from prometheus_client import generate_latest

from app.core import metrics
from app.core.config import settings
from app.core.constants import LogEvent
from app.core.errors import DependencyError
from app.core.logging import get_logger
from app.db.session import acquire
from app.repositories import show_repo

logger = get_logger(__name__)


async def render() -> bytes:
    try:
        async with acquire(settings.readyz_timeout_seconds) as conn:
            available = await show_repo.available_by_show(conn, settings.gauge_max_shows)
    except DependencyError:
        # The counters are still worth serving when the database is not answering.
        logger.warning(LogEvent.METRICS_GAUGE_UNAVAILABLE)
    else:
        metrics.seats_available.clear()
        for show_id, count in available.items():
            metrics.seats_available.labels(show_id=str(show_id)).set(count)
    return generate_latest()
