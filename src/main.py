"""CLI entrypoint for the Koku-to-Lago sync service."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

from src.bootstrap import bootstrap_lago
from src.config import AppConfig, load_config
from src.koku_client import KokuClient
from src.lago_sync import LagoSync
from src.reconcile import reconcile_month
from src.state import SyncState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def sync(config: AppConfig, start_date: date, end_date: date, force: bool = False):
    """Run the sync for all providers/customers over the given date range."""
    koku = KokuClient(config)
    lago = LagoSync(config)
    state = SyncState()
    org_id = config.cost_management.org_id
    total_events = 0

    for provider in config.providers_needed():
        group_by = config.group_by_for_provider(provider)
        customers = config.customers_for_provider(provider)

        if not customers:
            logger.info("No customers configured for %s, skipping", provider)
            continue

        # Check state tracking (skip if all customers already synced for this range)
        if not force:
            all_synced = all(
                all(
                    state.is_synced(org_id, c.external_id, provider, start_date + timedelta(days=d))
                    for d in range((end_date - start_date).days + 1)
                )
                for c in customers
            )
            if all_synced:
                logger.info("Provider %s already synced for %s to %s, skipping (use --force to override)",
                           provider, start_date, end_date)
                continue

        logger.info("Syncing %s from %s to %s (%d customers)", provider, start_date, end_date, len(customers))
        try:
            data, _meta = koku.fetch_costs(provider, start_date, end_date, group_by)
            count = lago.sync_provider(provider, data, customers)
            total_events += count

            # Mark all dates as synced for all customers
            for c in customers:
                current = start_date
                while current <= end_date:
                    state.mark_synced(org_id, c.external_id, provider, current, count)
                    current += timedelta(days=1)

        except Exception:
            logger.exception("Failed to sync provider %s", provider)

    koku.close()
    state.close()
    logger.info("Sync complete. Total events pushed: %d", total_events)
    return total_events


def reconcile(config: AppConfig, month: str):
    """Run reconciliation for a given month."""
    results = reconcile_month(config, month)

    if not results:
        logger.info("No data to reconcile for %s", month)
        return

    print(f"\n{'='*70}")
    print(f"  Reconciliation Report: {month}")
    print(f"{'='*70}\n")

    for r in results:
        status_icon = "OK" if r["status"] == "ok" else "WARNING" if r["status"] == "warning" else "NO DATA"
        print(f"  [{status_icon}] {r['customer_id']:30s} | {r['provider']:10s} | ${r['cost_management_total']:>12.2f}")
        if "message" in r:
            print(f"         {r['message']}")

    print(f"\n{'='*70}\n")


def _month_to_date_range(month: str) -> tuple[date, date]:
    """Convert YYYY-MM to (first_day, last_day) tuple."""
    start = date.fromisoformat(f"{month}-01")
    if start.month == 12:
        end = date(start.year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(start.year, start.month + 1, 1) - timedelta(days=1)
    return start, end


def cli():
    """Parse CLI arguments and dispatch."""
    parser = argparse.ArgumentParser(
        prog="lago-sync",
        description="Sync Red Hat Cost Management data to Lago for itemized billing",
    )
    parser.add_argument(
        "--config", "-c",
        default=None,
        help="Path to config.yaml (default: LAGO_SYNC_CONFIG env var or ./config.yaml)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # bootstrap command
    subparsers.add_parser("bootstrap", help="Provision Lago entities (metrics, plan, charges, customers, subscriptions)")

    # sync command
    sync_parser = subparsers.add_parser("sync", help="Run a cost data sync to Lago")
    sync_parser.add_argument("--month", type=str, default=None, help="Month to sync (YYYY-MM). Overrides start/end.")
    sync_parser.add_argument("--start-date", type=date.fromisoformat, default=None, help="Start date (YYYY-MM-DD).")
    sync_parser.add_argument("--end-date", type=date.fromisoformat, default=None, help="End date (YYYY-MM-DD).")
    sync_parser.add_argument("--force", action="store_true", help="Re-sync even if already synced.")

    # reconcile command
    reconcile_parser = subparsers.add_parser("reconcile", help="Compare Cost Management totals vs Lago usage")
    reconcile_parser.add_argument("--month", type=str, required=True, help="Month to reconcile (YYYY-MM).")

    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)

    if args.command == "bootstrap":
        bootstrap_lago(config)

    elif args.command == "sync":
        if args.month:
            start, end = _month_to_date_range(args.month)
        elif args.start_date and args.end_date:
            start, end = args.start_date, args.end_date
        else:
            yesterday = date.today() - timedelta(days=1)
            start, end = yesterday, yesterday

        if start > end:
            logger.error("start-date must be <= end-date")
            sys.exit(1)

        sync(config, start, end, force=args.force)

    elif args.command == "reconcile":
        reconcile(config, args.month)


if __name__ == "__main__":
    cli()
