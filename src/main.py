"""CLI entrypoint for the Koku-to-Lago sync service."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

from src.bootstrap import bootstrap_lago
from src.config import settings
from src.koku_client import KokuClient
from src.lago_sync import LagoSync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def sync(start_date: date, end_date: date):
    """Run the sync for all configured providers over the given date range."""
    koku = KokuClient()
    lago = LagoSync()
    total_events = 0

    for provider in settings.providers:
        logger.info("Syncing %s from %s to %s", provider, start_date, end_date)
        try:
            data = koku.fetch_costs(provider, start_date, end_date)
            count = lago.sync_provider(provider, data)
            total_events += count
        except Exception:
            logger.exception("Failed to sync provider %s", provider)

    koku.close()
    logger.info("Sync complete. Total events pushed: %d", total_events)
    return total_events


def cli():
    """Parse CLI arguments and dispatch."""
    parser = argparse.ArgumentParser(
        prog="lago-sync",
        description="Sync Red Hat Cost Management data to Lago for itemized billing",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # sync command
    sync_parser = subparsers.add_parser("sync", help="Run a cost data sync to Lago")
    sync_parser.add_argument(
        "--start-date",
        type=date.fromisoformat,
        default=None,
        help="Start date (YYYY-MM-DD). Defaults to yesterday.",
    )
    sync_parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        default=None,
        help="End date (YYYY-MM-DD). Defaults to yesterday.",
    )

    # bootstrap command
    subparsers.add_parser("bootstrap", help="Provision Lago entities (metrics, plans, customer, subscriptions)")

    args = parser.parse_args()

    if args.command == "bootstrap":
        bootstrap_lago()
    elif args.command == "sync":
        yesterday = date.today() - timedelta(days=1)
        start = args.start_date or yesterday
        end = args.end_date or yesterday
        if start > end:
            logger.error("start-date must be <= end-date")
            sys.exit(1)
        sync(start, end)


if __name__ == "__main__":
    cli()
