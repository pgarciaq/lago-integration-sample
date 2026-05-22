"""CLI entrypoint for the Koku-to-Lago sync service."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

from src.bootstrap import bootstrap_lago
from src.config import ConfigError, load_config, AppConfig
from src.koku_client import KokuClient
from src.lago_sync import LagoSync
from src.reconcile import reconcile_month
from src.state import SyncState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _check_billing_boundary(end_date: date, force: bool) -> bool:
    """Warn if syncing near a billing period close (last day of month, last hour of day).

    Lago finalizes billing periods at month-end in UTC. Pushing events very
    close to the boundary risks them landing in the wrong period.
    Returns True if safe to proceed, False if should abort.
    """
    from datetime import datetime as dt, timezone as tz
    now = dt.now(tz=tz.utc)
    today = now.date()

    if end_date.month != today.month or end_date.year != today.year:
        return True

    next_month = date(today.year + (1 if today.month == 12 else 0), (today.month % 12) + 1, 1)
    days_until_close = (next_month - today).days

    if days_until_close <= 1 and now.hour >= 22:
        if force:
            logger.warning(
                "Syncing within 2 hours of billing period close (month-end UTC). "
                "Events may land in the next billing period."
            )
            return True
        else:
            logger.error(
                "Refusing to sync: within 2 hours of billing period close (month-end UTC). "
                "Events pushed now may land in the next billing period. "
                "Use --force to override, or wait until the new period starts."
            )
            return False
    return True


def sync(
    config: AppConfig,
    start_date: date,
    end_date: date,
    force: bool = False,
    dry_run: bool = False,
    force_resend: bool = False,
):
    """Run the sync for all providers/customers over the given date range."""
    if not dry_run and not _check_billing_boundary(end_date, force):
        return 0

    koku = KokuClient(config)
    lago = LagoSync(config, force_resend=force_resend)
    state = SyncState(config.state_db_path)
    org_id = config.cost_management.org_id
    total_events = 0
    total_failed = 0

    for provider in config.providers_needed():
        group_by = config.group_by_for_provider(provider)
        customers = config.customers_for_provider(provider)

        if not customers:
            logger.info("No customers configured for %s, skipping", provider)
            continue

        # Check state tracking
        if not force and not dry_run:
            all_synced = all(
                all(
                    state.is_synced(org_id, c.external_id, provider, start_date + timedelta(days=d))
                    for d in range((end_date - start_date).days + 1)
                )
                for c in customers
            )
            if all_synced:
                logger.info(
                    "Provider %s already synced for %s to %s, skipping (use --force to override)",
                    provider, start_date, end_date,
                )
                continue

        logger.info("Syncing %s from %s to %s (%d customers)", provider, start_date, end_date, len(customers))
        try:
            data, _meta = koku.fetch_costs(provider, start_date, end_date, group_by)

            if not data:
                logger.warning(
                    "No data returned from Cost Management for %s (%s to %s). "
                    "Check that data has been processed for this date range.",
                    provider, start_date, end_date,
                )
                continue

            result = lago.sync_provider(provider, data, customers, dry_run=dry_run, state=state)
            total_events += result.events_sent
            total_failed += result.events_failed

            # Record state per customer per date (only on actual push, not dry_run)
            if not dry_run and result.events_sent > 0:
                current = start_date
                while current <= end_date:
                    for c in customers:
                        state.mark_synced(org_id, c.external_id, provider, current, result.events_sent)
                    current += timedelta(days=1)

        except Exception:
            logger.exception("Failed to sync provider %s", provider)

    koku.close()
    state.close()

    if dry_run:
        logger.info("[DRY RUN] Complete. Would have pushed %d events total.", total_events)
        return 0
    else:
        logger.info("Sync complete. Events pushed: %d, failed: %d", total_events, total_failed)
        if total_failed > 0:
            logger.error(
                "%d events failed to send. Re-run with --force to retry. "
                "Check Lago connectivity and API key.",
                total_failed,
            )
            return 1
        if total_events == 0:
            logger.warning("No events were generated. Check date range and customer filters.")
        return 0


def validate(config: AppConfig):
    """Validate connectivity and configuration against both APIs."""
    import httpx

    errors = []
    warnings = []

    print("\n  Validating configuration...\n")

    # 1. Check Lago connectivity
    print("  [1/4] Lago API connectivity...")
    try:
        resp = httpx.get(
            f"{config.lago.api_url.rstrip('/')}/api/v1/billable_metrics?per_page=1",
            headers={"Authorization": f"Bearer {config.lago.api_key}"},
            timeout=15.0,
        )
        if resp.status_code == 200:
            print("        OK — connected to Lago")
        elif resp.status_code == 401:
            errors.append("Lago API key is invalid (401 Unauthorized)")
        else:
            errors.append(f"Lago returned unexpected status: {resp.status_code}")
    except httpx.HTTPError as e:
        errors.append(f"Cannot reach Lago at {config.lago.api_url}: {e}")

    # 2. Check Cost Management connectivity
    print("  [2/4] Cost Management API connectivity...")
    koku = KokuClient(config)
    try:
        data, _meta = koku.fetch_costs(
            list(config.providers_needed())[0] if config.providers_needed() else "openshift",
            date.today() - timedelta(days=7),
            date.today() - timedelta(days=7),
            ["cluster"] if "openshift" in config.providers_needed() else ["account"],
        )
        print(f"        OK — connected to Cost Management ({len(data)} time buckets)")
        if not data:
            warnings.append("Cost Management returned no data for last week. Data may not be processed yet.")
    except Exception as e:
        errors.append(f"Cannot fetch from Cost Management: {e}")
    finally:
        koku.close()

    # 3. Check Lago entities exist
    print("  [3/4] Lago plan and charges...")
    try:
        resp = httpx.get(
            f"{config.lago.api_url.rstrip('/')}/api/v1/plans/cloud_cost_passthrough",
            headers={"Authorization": f"Bearer {config.lago.api_key}"},
            timeout=15.0,
        )
        if resp.status_code == 200:
            plan = resp.json().get("plan", {})
            charges = plan.get("charges", [])
            print(f"        OK — plan exists with {len(charges)} charge(s)")
            if len(charges) == 0:
                warnings.append("Plan has no charges. Run 'lago-sync bootstrap' to create them.")
        elif resp.status_code == 404:
            errors.append("Plan 'cloud_cost_passthrough' not found. Run 'lago-sync bootstrap' first.")
        else:
            warnings.append(f"Could not verify plan: {resp.status_code}")
    except httpx.HTTPError as e:
        warnings.append(f"Could not check Lago plan: {e}")

    # 4. Check subscriptions
    print("  [4/4] Customer subscriptions...")
    missing_subs = []
    for customer_config in config.customers:
        for resource in customer_config.resources:
            sub_id = f"{customer_config.external_id}_{resource.provider}"
            try:
                resp = httpx.get(
                    f"{config.lago.api_url.rstrip('/')}/api/v1/subscriptions/{sub_id}",
                    headers={"Authorization": f"Bearer {config.lago.api_key}"},
                    timeout=10.0,
                )
                if resp.status_code != 200:
                    missing_subs.append(sub_id)
            except httpx.HTTPError:
                missing_subs.append(sub_id)

    if missing_subs:
        warnings.append(f"{len(missing_subs)} subscription(s) not found: {', '.join(missing_subs[:5])}. Run 'lago-sync bootstrap'.")
    else:
        print("        OK — all subscriptions exist")

    # Summary
    print()
    if errors:
        print(f"  ERRORS ({len(errors)}):")
        for e in errors:
            print(f"    ✗ {e}")
    if warnings:
        print(f"  WARNINGS ({len(warnings)}):")
        for w in warnings:
            print(f"    ! {w}")
    if not errors and not warnings:
        print("  All checks passed. Ready to sync.")
    print()

    return len(errors) == 0


def reconcile(config: AppConfig, month: str):
    """Run reconciliation for a given month."""
    results = reconcile_month(config, month)

    if not results:
        logger.info("No data to reconcile for %s", month)
        return

    print(f"\n{'='*70}")
    print(f"  Reconciliation Report: {month}")
    print(f"{'='*70}\n")

    status_labels = {
        "ok": "OK",
        "mismatch": "MISMATCH",
        "warning": "WARNING",
        "no_data": "NO DATA",
        "lago_unavailable": "LAGO N/A",
    }
    for r in results:
        status_icon = status_labels.get(r["status"], r["status"].upper())
        lago_col = f"${r['lago_total']:>12.2f}" if r.get("lago_total") is not None else "         N/A"
        print(f"  [{status_icon:>8s}] {r['customer_id']:30s} | {r['provider']:10s} | CM: ${r['cost_management_total']:>10.2f} | Lago: {lago_col}")
        if "message" in r:
            print(f"             {r['message']}")

    print(f"\n{'='*70}\n")


def _month_to_date_range(month: str) -> tuple[date, date]:
    """Convert YYYY-MM to (first_day, last_day) tuple."""
    try:
        start = date.fromisoformat(f"{month}-01")
    except ValueError:
        logger.error("Invalid month format: '%s'. Expected YYYY-MM (e.g., 2024-01).", month)
        sys.exit(1)
    if start.month == 12:
        end = date(start.year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(start.year, start.month + 1, 1) - timedelta(days=1)
    return start, end


def cli():
    """Parse CLI arguments and dispatch."""
    parser = argparse.ArgumentParser(
        prog="lago-sync",
        description="Sync Red Hat Cost Management data to Lago for itemized billing.",
        epilog="Documentation: https://github.com/pgarciaq/lago-integration-sample",
    )
    parser.add_argument(
        "--config", "-c",
        default=None,
        help="Path to config.yaml (default: $LAGO_SYNC_CONFIG or ./config.yaml)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # bootstrap command
    bootstrap_parser = subparsers.add_parser(
        "bootstrap",
        help="Provision Lago entities (metrics, plan, charges, customers, subscriptions)",
    )
    bootstrap_parser.add_argument(
        "--update", action="store_true",
        help="Update existing charges if config changed (e.g., invoice_group_by). "
             "Deletes and recreates charges with new pricing_group_keys.",
    )

    # validate command
    subparsers.add_parser(
        "validate",
        help="Check connectivity and configuration (run before first sync)",
    )

    # sync command
    sync_parser = subparsers.add_parser("sync", help="Sync cost data from Cost Management to Lago")
    sync_parser.add_argument("--month", type=str, default=None, help="Month to sync (YYYY-MM). Overrides start/end.")
    sync_parser.add_argument("--start-date", type=date.fromisoformat, default=None, help="Start date (YYYY-MM-DD).")
    sync_parser.add_argument("--end-date", type=date.fromisoformat, default=None, help="End date (YYYY-MM-DD).")
    sync_parser.add_argument("--force", action="store_true", help="Re-sync even if already synced (uses same transaction IDs; duplicates are idempotent).")
    sync_parser.add_argument(
        "--force-resend", action="store_true",
        help="Generate fresh transaction IDs and push events again. USE WITH CAUTION: "
             "this WILL create duplicate charges in Lago. Only use when you need to "
             "replace data after a billing period reset or subscription recreation.",
    )
    sync_parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be sent without actually pushing events to Lago.",
    )

    # reconcile command
    reconcile_parser = subparsers.add_parser("reconcile", help="Compare Cost Management totals vs Lago usage")
    reconcile_parser.add_argument("--month", type=str, required=True, help="Month to reconcile (YYYY-MM).")

    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)
    except ConfigError as e:
        logger.error("Configuration error:\n  %s", str(e).replace("\n", "\n  "))
        sys.exit(1)

    if args.command == "bootstrap":
        bootstrap_lago(config, update=args.update)

    elif args.command == "validate":
        success = validate(config)
        if not success:
            sys.exit(1)

    elif args.command == "sync":
        if args.month:
            start, end = _month_to_date_range(args.month)
        elif args.start_date and args.end_date:
            start, end = args.start_date, args.end_date
        elif args.start_date or args.end_date:
            logger.error("Both --start-date and --end-date are required when not using --month.")
            sys.exit(1)
        else:
            yesterday = date.today() - timedelta(days=1)
            start, end = yesterday, yesterday

        if start > end:
            logger.error("start-date (%s) must be <= end-date (%s)", start, end)
            sys.exit(1)

        if args.force_resend:
            logger.warning(
                "WARNING: --force-resend generates new transaction IDs. "
                "This WILL create duplicate charges if events already exist in Lago. "
                "Only use after resetting the Lago billing period."
            )
        exit_code = sync(config, start, end, force=args.force or args.force_resend, dry_run=args.dry_run, force_resend=args.force_resend)
        sys.exit(exit_code)

    elif args.command == "reconcile":
        reconcile(config, args.month)


if __name__ == "__main__":
    cli()
