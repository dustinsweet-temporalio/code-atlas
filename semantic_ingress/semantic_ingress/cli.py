# semantic_ingress/cli.py
"""
Command-line interface for the semantic ingress system.
"""
import asyncio
import dataclasses
import json
import logging
import os
import re
import signal
import sys
from pathlib import Path

import click
import structlog

from semantic_ingress.config import IngestionConfig
from semantic_ingress.core import CostEstimator, GenerationOrchestrator
from semantic_ingress.core.folder_repair import FolderRepairOrchestrator
from semantic_ingress.preflight import EXIT_CODE as PREFLIGHT_EXIT_CODE
from semantic_ingress.preflight import PreflightError, PreflightResult, run_preflight
from semantic_ingress.report.gather import ReportError
from semantic_ingress.report.service import build_and_store_report
from semantic_ingress.storage import MongoStore

logger = structlog.get_logger()

DEFAULT_PREFIX = os.environ.get("DB_PREFIX", "").strip() or "semv2"
REPO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_repo_id(ctx: click.Context, param: click.Parameter, value: str) -> str:
    """Repo IDs name a database and a directory; the query side only accepts these characters."""
    if not REPO_ID_PATTERN.match(value):
        raise click.BadParameter(
            "use only letters, numbers, hyphens and underscores (no slashes, spaces or periods)"
        )
    return value


def _require_api_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        raise ValueError("ANTHROPIC_API_KEY is not set. Add it to your .env file.")


def _preflight_or_exit(config: IngestionConfig, stopping: str = "indexing",
                       check_temporal: bool = True) -> PreflightResult:
    """Run the preflight checks; on failure print the fix and exit before any LLM spend."""
    print("=" * 80)
    print("PREFLIGHT")
    print("=" * 80)
    try:
        result = run_preflight(config, check_temporal=check_temporal)
    except PreflightError as e:
        print(f"❌ {e}")
        print(f"Stopped before {stopping}; nothing was spent or changed.")
        print("=" * 80 + "\n")
        sys.exit(PREFLIGHT_EXIT_CODE)
    for check, detail in result.checks:
        print(f"✓ {check:<18} {detail}")
    print("=" * 80 + "\n")
    return result


@click.group()
@click.version_option()
def cli() -> None:
    """Semantic Code Ingress System."""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _print_plan(plan) -> None:
    """The mode header the in-process sync printed when it started generating."""
    print("\n" + "=" * 80)
    if plan.mode == "full":
        print(f"MODE: FULL INDEX ({plan.reason})")
    else:
        print("MODE: INCREMENTAL (comparing index vs Git state)")
    print("=" * 80 + "\n")
    for warning in plan.warnings:
        print(warning + "\n")
    if plan.mode == "incremental":
        print("Delta Summary:")
        print(f"  Added:     {plan.n_added:,} files")
        print(f"  Modified:  {plan.n_modified:,} files")
        print(f"  Deleted:   {plan.n_deleted:,} files")
        print(f"  Unchanged: {plan.n_unchanged:,} files")
        print()
    print(f"Files to summarise: {plan.n_files:,}"
          + (f" | lens refresh: {plan.n_lens_only:,} unchanged files" if plan.n_lens_only else ""))
    print()


def _render_outcome(outcome, repo_id: str, log) -> int:
    """Print the end of the run as the in-process sync did; returns the exit code."""
    from semantic_ingress.temporal.client import usage_cost

    plan = outcome.plan
    usage = outcome.usage
    failed_count = len(outcome.failed) + outcome.failed_overflow

    print("\n" + "=" * 80)
    print(f"{'FULL' if plan.mode == 'full' else 'INCREMENTAL'} SYNC COMPLETE")
    print("=" * 80)
    print(f"Files processed:     {outcome.files_done:,}")
    print(f"Folders summarised:  {outcome.folders_generated:,}")
    print(f"Total Input Tokens:  {usage.input_tokens:,}")
    if usage.cache_read_tokens or usage.cache_write_tokens:
        print(f"Cache Read Tokens:   {usage.cache_read_tokens:,}")
        print(f"Cache Write Tokens:  {usage.cache_write_tokens:,}")
    print(f"Total Output Tokens: {usage.output_tokens:,}")
    print(f"Total Cost:          ${usage_cost(usage):.4f}")
    if outcome.continued_as_new_count:
        print(f"Workflow runs:       {outcome.continued_as_new_count + 1} (continued as new)")
    print("=" * 80)
    print(f"\nTotal cost: ${usage_cost(usage):.4f}")

    if outcome.dry_run:
        print("\n" + "=" * 80)
        print("DRY-RUN COMPLETE - No changes made to the index")
        print("=" * 80)
        if plan.reset_index:
            print("Would replace the existing index (full re-index)")
        if plan.n_deleted:
            print(f"Would delete the summaries of {plan.n_deleted:,} removed files")
        print(f"Would upsert {outcome.would_upsert or 0} documents")
        if outcome.would_update_lens:
            print(f"Would update the lens of {outcome.would_update_lens} unchanged files")
        if failed_count:
            print(f"Failed items: {failed_count}")
        print()
        log.info("dry_run_complete")
        return 0

    if outcome.deleted is not None:
        print("=" * 80)
        print(f"DELETED {plan.n_deleted} REMOVED FILES FROM INDEX")
        print("=" * 80)
        print(f"✓ Deleted {outcome.deleted.files_deleted} file summary artifacts")
        print(f"✓ Deleted {outcome.deleted.folders_deleted} orphaned folder summary artifacts")
        print("=" * 80 + "\n")
    elif outcome.delete_error:
        print(f"❌ Failed to delete the summaries of removed files: {outcome.delete_error}\n")

    print("\n" + "=" * 80)
    print("PERSISTING TO INDEX")
    print("=" * 80)
    if plan.reset_index:
        print("Replaced the existing index (full re-index)")
    if outcome.persist_error:
        log.error("index_persistence_failed", error=outcome.persist_error)
        print(f"\n❌ Index persistence failed: {outcome.persist_error}")
        print("Re-run the sync command to retry.\n")
    else:
        persist = outcome.persist
        if outcome.finalize is not None and outcome.finalize.lens_refreshed is not None:
            print(f"Lens refreshed:  {outcome.finalize.lens_refreshed} unchanged files")
        print(f"Total processed: {persist.processed}")
        print(f"Inserted:        {persist.inserted}")
        print(f"Updated:         {persist.updated}")
        print(f"No change:       {persist.no_change}")
        print(f"Errors:          {persist.errors}")
        print("=" * 80 + "\n")
        log.info("index_persistence_complete", **dataclasses.asdict(persist))

        if outcome.finalize is not None:
            print("=" * 80)
            print("CREATING TEXT INDEXES (FULL INDEX MODE)" if plan.mode == "full" else "VALIDATING TEXT INDEXES")
            print("=" * 80)
            for line in outcome.finalize.index_messages:
                print(line)
            print("\n" + "=" * 80)
            print("INDEX CREATION COMPLETE" if plan.mode == "full" else "INDEX VALIDATION COMPLETE")
            print("=" * 80 + "\n")
        elif outcome.finalize_error:
            print(f"❌ Recording index metadata and text indexes failed: {outcome.finalize_error}\n")

    mirror = outcome.mirror
    if mirror is not None and mirror.error:
        print(f"\n❌ Source mirror failed: {mirror.error}\n")

    report = outcome.report
    if report is not None and report.status == "ok":
        print(f"✓ Report stored in the index | cost ${report.cost_usd:.4f}")
        print("=" * 80 + "\n")
    elif report is not None and report.status == "failed":
        log.error("report_failed", error=report.error)
        print(f"⚠️  Temporal report failed ({report.error}). The index is complete; "
              f"run ./scripts/report.sh {repo_id} to retry.")
        print("=" * 80 + "\n")

    if outcome.disk_written is not None:
        print(f"✓ Wrote {outcome.disk_written} artifacts to disk\n")
    elif outcome.disk_error:
        log.error("disk_write_failed", error=outcome.disk_error)
        print(f"⚠️  Disk write failed: {outcome.disk_error}\n")

    mirror_failures = mirror.total_failures if mirror is not None else 0
    index_failed = bool(outcome.persist_error or outcome.finalize_error)
    if failed_count or mirror_failures or index_failed:
        print("\n" + "=" * 80)
        print("⚠️  SYNC COMPLETED WITH FAILURES")
        print("=" * 80)

        if failed_count:
            print(f"\n❌ SEMANTIC GENERATION FAILURES: {failed_count}")
            print("-" * 80)
            for item in outcome.failed:
                print(f"  • {item.path}")
                print(f"    Error: {item.error}")
            if outcome.failed_overflow:
                print(f"  ... and {outcome.failed_overflow} more")

        if index_failed:
            print("\n❌ INDEX WRITE FAILURES")
            print("-" * 80)
            for label, error in (("Persist", outcome.persist_error), ("Finalize", outcome.finalize_error)):
                if error:
                    print(f"  • {label}: {error}")

        if mirror_failures:
            print(f"\n❌ SOURCE MIRROR FAILURES: {mirror_failures}")
            print("-" * 80)
            if mirror.failed_uploads:
                print("Failed uploads:")
                for path, error in mirror.failed_uploads:
                    print(f"  • {path}")
                    print(f"    Error: {error}")
            if mirror.failed_deletes:
                print("\nFailed deletions:")
                for path, error in mirror.failed_deletes:
                    print(f"  • {path}")
                    print(f"    Error: {error}")
            if mirror.error:
                print(f"  • {mirror.error}")

        print()
        print("Re-run the sync command to retry failed operations.")
        print("The system will automatically resume from the current state.")
        print("=" * 80 + "\n")
        log.warning("sync_completed_with_failures", semantic_failures=failed_count, mirror_failures=mirror_failures)
        return 1

    print("✓ Sync completed successfully (including source mirror)\n")
    log.info("sync_complete")
    return 0


@cli.command()
@click.option(
    "--repo-path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Path to the local Git repository.",
)
@click.option(
    "--repo-id",
    type=str,
    required=True,
    callback=_validate_repo_id,
    help="Name for this repository in the index (letters, numbers, - and _).",
)
@click.option(
    "--prefix",
    type=str,
    default=DEFAULT_PREFIX,
    show_default=True,
    help="Database name prefix (must start with 'sem'). Defaults to DB_PREFIX.",
)
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Optional: Write artifacts to disk after successful sync (for debugging).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview changes without modifying the index.",
)
@click.option(
    "--window-size",
    type=click.IntRange(min=1),
    default=None,
    help="File or folder summaries generated at a time. Defaults to INDEX_WINDOW_SIZE (5).",
)
@click.option(
    "--max-workers",
    type=click.IntRange(min=1),
    default=None,
    hidden=True,
    help="Deprecated alias of --window-size.",
)
@click.option(
    "--resume-cache",
    type=click.Path(path_type=Path),
    default=None,
    hidden=True,
    help="Removed: re-run sync and the Temporal workflow resumes.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Full re-index: regenerate everything and replace the existing index.",
)
@click.option(
    "--refresh-lens",
    is_flag=True,
    default=False,
    help="Regenerate the Temporal lens of every file with the installed pack (summaries unchanged).",
)
@click.option(
    "--no-report",
    is_flag=True,
    default=False,
    help="Skip the Temporal report that normally follows indexing when the lens is on.",
)
@click.option(
    "--can-after",
    type=click.IntRange(min=1),
    default=None,
    hidden=True,
    help="Testing: continue the workflow as new after N completed items.",
)
def sync(
    repo_path: Path,
    repo_id: str,
    prefix: str,
    output_dir: Path | None,
    dry_run: bool,
    window_size: int | None,
    max_workers: int | None,
    resume_cache: Path | None,
    force: bool,
    refresh_lens: bool,
    no_report: bool,
    can_after: int | None,
) -> None:
    """
    Synchronize repository state to the semantic index.

    Runs as a Temporal workflow (sync-<repo-id>) with a worker embedded in this
    process. Automatically detects full vs incremental mode by querying the index.
    Re-running after a crash or Ctrl-C resumes the same workflow.
    """
    if resume_cache is not None:
        raise click.UsageError("--resume-cache was removed; re-run sync and the Temporal workflow resumes")

    from temporalio.client import WorkflowFailureError

    from semantic_ingress.temporal.client import start_or_attach
    from semantic_ingress.temporal.constants import workflow_id
    from semantic_ingress.temporal.models import SyncParams

    log = logger.bind(mode="sync", repo_id=repo_id, prefix=prefix, dry_run=dry_run)
    log.info("starting_sync")

    try:
        config = IngestionConfig(
            repo_path=repo_path,
            output_dir=Path("/tmp"),  # Not used during generation
            repo_id=repo_id,
            prefix=prefix,
        )
        log.info("configuration_loaded", repo_id=config.repo_id)
        if max_workers is not None and window_size is None:
            print("ℹ️  --max-workers is deprecated; use --window-size.")
        if os.environ.get("INDEX_MAX_WORKERS", "").strip() and not os.environ.get("INDEX_WINDOW_SIZE", "").strip():
            print("ℹ️  INDEX_MAX_WORKERS is no longer used; set INDEX_WINDOW_SIZE (default 5) instead.")
        window = window_size or max_workers or config.temporal.window_size

        _require_api_key()
        preflight = _preflight_or_exit(config)

        params = SyncParams(
            repo_id=repo_id,
            prefix=prefix,
            repo_path=str(repo_path),
            dry_run=dry_run,
            force=force,
            refresh_lens=refresh_lens,
            no_report=no_report,
            output_dir=str(output_dir) if output_dir else None,
            window_size=window,
            lens_enabled=config.lens.enabled,
            test_can_after_items=can_after,
        )
        ui_link = config.temporal.workflow_url(workflow_id(repo_id))

        def interrupted(signum, frame):
            # The workflow lives on the server; exit at once rather than wait for the
            # worker's in-flight activities (they time out and are retried on resume).
            print(f"\nInterrupted. The workflow keeps running on the server; re-run the same command "
                  f"to resume (UI: {ui_link})", flush=True)
            os._exit(130)

        signal.signal(signal.SIGINT, interrupted)
        print(f"Window: {window} summaries at a time (Temporal {config.temporal.address})")
        run = asyncio.run(start_or_attach(config, preflight, params, on_plan=_print_plan))
        exit_code = _render_outcome(run.outcome, repo_id, log)
    except WorkflowFailureError as e:
        cause = e.cause
        while getattr(cause, "cause", None) is not None:
            cause = cause.cause
        message = getattr(cause, "message", None) or str(cause or e)
        log.error("sync_failed", error=message)
        print(f"\n❌ Sync failed: {message}\n")
        sys.exit(1)
    except Exception as e:
        log.error("sync_failed", error=str(e))
        print(f"\n❌ Sync failed: {e}\n")
        sys.exit(1)
    sys.exit(exit_code)


@cli.command()
@click.option(
    "--prefix",
    type=str,
    default=DEFAULT_PREFIX,
    show_default=True,
    help="Database name prefix (must start with 'sem'). Defaults to DB_PREFIX.",
)
def worker(prefix: str) -> None:
    """
    Run the indexing worker for syncs started from the web UI (the compose `worker`
    service). It runs each such sync until it finishes, and resumes running ones
    after a restart. Syncs started with `sync` run in that command instead.
    """
    from semantic_ingress.temporal.service import serve

    # As PID 1 in a container, the process gets no default SIGTERM handling. Exit at once:
    # the workflows live on the server, and in-flight activities are retried on restart.
    signal.signal(signal.SIGTERM, lambda signum, frame: os._exit(0))
    try:
        asyncio.run(serve(prefix))
    except KeyboardInterrupt:
        pass


@cli.command()
@click.option(
    "--repo-path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Path to the local Git repository.",
)
@click.option(
    "--repo-id",
    type=str,
    required=True,
    callback=_validate_repo_id,
    help="Name for this repository in the index (letters, numbers, - and _).",
)
@click.option(
    "--prefix",
    type=str,
    default=DEFAULT_PREFIX,
    show_default=True,
    help="Database name prefix (must start with 'sem'). Defaults to DB_PREFIX.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview changes without modifying the source store.",
)
def source_mirror(
    repo_path: Path,
    repo_id: str,
    prefix: str,
    dry_run: bool,
) -> None:
    """
    Force an exact local mirror of repository files.
    
    Compares the repository vs the source store, uploads missing/changed files,
    deletes extra files. Does NOT perform semantic generation.
    """
    log = logger.bind(mode="source_mirror", repo_id=repo_id, prefix=prefix, dry_run=dry_run)
    log.info("starting_source_mirror_only")
    
    try:
        # Initialize config
        config = IngestionConfig(
            repo_path=repo_path,
            output_dir=Path("/tmp"),  # Not used
            repo_id=repo_id,
            prefix=prefix,
        )
        log.info("configuration_loaded", repo_id=config.repo_id)
        
        # Initialize Git adapter and get snapshot
        from semantic_ingress.core.git_adapter import GitAdapter
        from semantic_ingress.core.source_mirror_orchestrator import SourceMirrorOrchestrator
        
        git = GitAdapter(config.repo_path, config.file_filter)
        snapshot = git.get_snapshot()
        
        log.info("snapshot_acquired", file_count=len(snapshot.files))
        
        # Initialize mirror orchestrator
        mirror_orchestrator = SourceMirrorOrchestrator(config)
        
        # Run exact mirror
        result = mirror_orchestrator.mirror_exact(snapshot, dry_run=dry_run)
        
        # Display summary
        if dry_run:
            print("✓ Source mirror preview completed successfully\n")
        else:
            if result.has_failures:
                print("\n" + "=" * 80)
                print("⚠️  SOURCE MIRROR COMPLETED WITH FAILURES")
                print("=" * 80)
                
                if result.failed_uploads:
                    print("\n❌ Failed uploads:")
                    for path, error in result.failed_uploads:
                        print(f"  • {path}")
                        print(f"    Error: {error}")
                
                if result.failed_deletes:
                    print("\n❌ Failed deletions:")
                    for path, error in result.failed_deletes:
                        print(f"  • {path}")
                        print(f"    Error: {error}")
                
                print("\n" + "=" * 80 + "\n")
                log.warning("source_mirror_completed_with_failures", failures=result.total_failures)
                sys.exit(1)
            else:
                print("✓ Source mirror completed successfully\n")
        
        log.info("source_mirror_complete")
        
    except Exception as e:
        log.error("source_mirror_failed", error=str(e))
        print(f"\n❌ Source mirror failed: {e}\n")
        sys.exit(1)

@cli.command()
@click.option(
    "--repo-path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Path to the local Git repository.",
)
@click.option(
    "--repo-id",
    type=str,
    required=True,
    callback=_validate_repo_id,
    help="Name for this repository in the index (letters, numbers, - and _).",
)
@click.option(
    "--prefix",
    type=str,
    default=DEFAULT_PREFIX,
    show_default=True,
    help="Database name prefix (must start with 'sem'). Defaults to DB_PREFIX.",
)
@click.option(
    "--folder",
    type=str,
    required=True,
    help="Target folder path to repair (absolute or repo-relative).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview changes without modifying the index.",
)
def repair_folders(
    repo_path: Path,
    repo_id: str,
    prefix: str,
    folder: str,
    dry_run: bool,
) -> None:
    """
    Regenerate folder summaries for a folder and all its ancestors.

    Rebuilds them from the structured file summaries stored in the index (`content`),
    which indexes built by this version of `sync` have. To rebuild everything, run
    `sync --force` instead.
    """
    log = logger.bind(mode="repair_folders", repo_id=repo_id, prefix=prefix, folder=folder, dry_run=dry_run)
    log.info("starting_folder_repair")
    
    try:
        # Initialize config
        config = IngestionConfig(
            repo_path=repo_path,
            output_dir=Path("/tmp"),  # Not used
            repo_id=repo_id,
            prefix=prefix,
        )
        
        _require_api_key()

        # Initialize orchestrator and index store
        orchestrator = FolderRepairOrchestrator(config)
        agent_client = MongoStore(config.mongo, config.prefix)
        
        # Run repair
        result = orchestrator.repair(folder, agent_client, dry_run=dry_run)
        
        # Display summary
        if result["dry_run"]:
            print("✓ Repair preview completed successfully\n")
        else:
            print("✓ Repair completed successfully\n")
        
        log.info("repair_complete", **result)
        
    except Exception as e:
        log.error("repair_failed", error=str(e))
        print(f"\n❌ Repair failed: {e}\n")
        sys.exit(1)

@cli.command()
@click.option(
    "--prefix",
    type=str,
    default=DEFAULT_PREFIX,
    show_default=True,
    help="Database name prefix (must start with 'sem'). Defaults to DB_PREFIX.",
)
def preflight(prefix: str) -> None:
    """
    Check the knowledge pack, Anthropic API, MongoDB, the Temporal server and
    (lens on) the Temporal Docs MCP. Exits with code 2 on the first failure.
    `sync` runs this first.
    """
    config = IngestionConfig(repo_path=Path("."), output_dir=Path("/tmp"), prefix=prefix)
    try:
        _require_api_key()
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(PREFLIGHT_EXIT_CODE)
    _preflight_or_exit(config)
    print("✓ Preflight passed")


@cli.command()
@click.option(
    "--repo-id",
    type=str,
    required=True,
    callback=_validate_repo_id,
    help="Name of the indexed repository.",
)
@click.option(
    "--prefix",
    type=str,
    default=DEFAULT_PREFIX,
    show_default=True,
    help="Database name prefix (must start with 'sem'). Defaults to DB_PREFIX.",
)
@click.option(
    "--out-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("/reports"),
    show_default=True,
    help="Where to write <repo-id>/temporal-report.md and .json.",
)
@click.option(
    "--fresh",
    is_flag=True,
    default=False,
    help="Investigate every process again instead of reusing process cards whose files are unchanged.",
)
def report(repo_id: str, prefix: str, out_dir: Path, fresh: bool) -> None:
    """
    Write the Temporal report for an indexed repository: per process, whether it
    suits Temporal (with a design, or why not), and an evaluation of existing
    Temporal code. Uses only the index, the mirrored source, the knowledge pack,
    the report rubric and the Temporal Docs MCP.
    """
    config = IngestionConfig(repo_path=Path("."), output_dir=Path("/tmp"), repo_id=repo_id, prefix=prefix)
    # The report always needs the pack and the docs MCP, whatever LENS_ENABLED says.
    config.lens = dataclasses.replace(config.lens, enabled=True)
    try:
        _require_api_key()
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(PREFLIGHT_EXIT_CODE)
    checks = _preflight_or_exit(config, stopping="the report", check_temporal=False)
    try:
        result = build_and_store_report(config, checks, MongoStore(config.mongo, prefix), repo_id, fresh)
    except ReportError as e:
        print(f"\n❌ {e}\n")
        sys.exit(1)
    except Exception as e:
        logger.error("report_failed", error=str(e))
        print(f"\n❌ Report failed: {e}\n")
        sys.exit(1)

    target = out_dir / repo_id
    target.mkdir(parents=True, exist_ok=True)
    (target / "temporal-report.md").write_text(result.markdown)
    (target / "temporal-report.json").write_text(json.dumps(result.data, indent=2, default=str))
    print(f"\n✓ Report written: {repo_id}/temporal-report.md (and .json), and stored in the index "
          f"| cost ${result.usage.total_cost_usd:.4f}\n")


@cli.command()
@click.option(
    "--repo-path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Path to the local Git repository.",
)
def estimate(repo_path: Path) -> None:
    """Estimate token cost for semantic generation (local only, no LLM calls)."""
    log = logger.bind(mode="estimate", repo_path=str(repo_path))
    log.info("starting_estimation")
    
    try:
        config = IngestionConfig(
            repo_path=repo_path,
            output_dir=Path("/tmp"),  # Not used in estimate mode
            prefix="semv2",  # Dummy prefix for estimate mode (not used)
        )
        log.info("configuration_loaded", repo_id=config.repo_id)
        
        # Initialize cost estimator
        estimator = CostEstimator(config)
        
        # Generate estimate
        cost_estimate = estimator.estimate()
        
        # Format and print report
        report = estimator.format_report(cost_estimate)
        print(report)
        
        log.info("estimation_complete", total_cost_usd=f"${cost_estimate.total_cost_usd:.4f}")
    except Exception as e:
        log.error("estimation_failed", error=str(e))
        sys.exit(1)

@cli.command()
@click.option(
    "--repo-path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Path to the local Git repository.",
)
@click.option(
    "--repo-id",
    type=str,
    required=True,
    callback=_validate_repo_id,
    help="Name for this repository in the index (letters, numbers, - and _).",
)
@click.option(
    "--prefix",
    type=str,
    default=DEFAULT_PREFIX,
    show_default=True,
    help="Database name prefix (must start with 'sem'). Defaults to DB_PREFIX.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview changes without modifying the index.",
)
def dir_list_only(
    repo_path: Path,
    repo_id: str,
    prefix: str,
    dry_run: bool,
) -> None:
    """
    Generate and persist dir_list.txt document only.
    
    Useful for adding a directory listing to an existing index.
    Does NOT regenerate any semantic summaries.
    """
    log = logger.bind(mode="dir_list_only", repo_id=repo_id, prefix=prefix, dry_run=dry_run)
    log.info("starting_dir_list_generation")
    
    try:
        # Initialize config
        config = IngestionConfig(
            repo_path=repo_path,
            output_dir=Path("/tmp"),  # Not used
            repo_id=repo_id,
            prefix=prefix,
        )
        log.info("configuration_loaded", repo_id=config.repo_id)
        
        # Initialize orchestrator and git adapter
        orchestrator = GenerationOrchestrator(config)
        agent_client = MongoStore(config.mongo, config.prefix)
        
        # Get current snapshot
        snapshot = orchestrator.git.get_snapshot()
        log.info("snapshot_acquired", file_count=len(snapshot.files))
        
        # Generate dir_list document
        dir_list_doc = orchestrator._generate_dir_list_document(snapshot)
        log.info("dir_list_document_created", path=dir_list_doc.path)
        
        # Display summary
        print("\n" + "=" * 80)
        print("DIR_LIST GENERATION COMPLETE")
        print("=" * 80)
        print(f"Repository:  {repo_id}")
        print(f"Commit SHA:  {snapshot.commit_sha}")
        print(f"File count:  {len(snapshot.files)}")
        print("=" * 80 + "\n")
        
        if dry_run:
            print("✓ Dir list preview completed successfully (DRY-RUN)")
            print(f"Would upsert document to repo_summaries collection\n")
            log.info("dir_list_dry_run_complete")
            return
        
        # Persist to the index
        print("Persisting dir_list document to the index...")
        
        try:
            result = agent_client.upsert_documents_bulk([dir_list_doc])
            
            print(f"Total processed: {result['total_processed']}")
            print(f"Inserted:        {result['summary']['inserted']}")
            print(f"Updated:         {result['summary']['updated']}")
            print(f"No change:       {result['summary']['no_change']}")
            print(f"Errors:          {result['summary']['errors']}")
            print("=" * 80 + "\n")
            
            log.info("dir_list_persistence_complete", **result)
            
            if result['summary']['errors'] > 0:
                print("⚠️  Dir list persistence completed with errors")
                print("Re-run the command to retry.\n")
                sys.exit(1)
            else:
                print("✓ Dir list document persisted successfully\n")
        
        except Exception as e:
            log.error("dir_list_persistence_failed", error=str(e))
            print(f"\n❌ Index persistence failed: {e}\n")
            sys.exit(1)
            
    except Exception as e:
        log.error("dir_list_generation_failed", error=str(e))
        print(f"\n❌ Dir list generation failed: {e}\n")
        sys.exit(1)

def main() -> int:
    """Main entry point."""
    cli()
    return 0
