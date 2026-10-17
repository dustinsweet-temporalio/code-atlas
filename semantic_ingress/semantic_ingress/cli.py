# semantic_ingress/cli.py
"""
Command-line interface for the semantic ingress system.
"""
import dataclasses
import json
from datetime import datetime, timezone
import logging
import os
import re
import sys
from pathlib import Path

import click
import structlog

from semantic_ingress.config import IngestionConfig
from semantic_ingress.core import CostEstimator, GenerationOrchestrator
from semantic_ingress.core.disk_writer import DiskWriter
from semantic_ingress.core.folder_repair import FolderRepairOrchestrator
from semantic_ingress.core.llm_client import LLMClient
from semantic_ingress.preflight import EXIT_CODE as PREFLIGHT_EXIT_CODE
from semantic_ingress.preflight import PreflightError, PreflightResult, run_preflight
from semantic_ingress.report.gather import ReportError
from semantic_ingress.report.run import build_report
from semantic_ingress.storage import MongoStore

logger = structlog.get_logger()

DEFAULT_PREFIX = os.environ.get("DB_PREFIX", "").strip() or "semv2"
DEFAULT_MAX_WORKERS = int(os.environ.get("INDEX_MAX_WORKERS", "").strip() or 8)
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


def _preflight_or_exit(config: IngestionConfig, stopping: str = "indexing") -> PreflightResult:
    """Run the preflight checks; on failure print the fix and exit before any LLM spend."""
    print("=" * 80)
    print("PREFLIGHT")
    print("=" * 80)
    try:
        result = run_preflight(config)
    except PreflightError as e:
        print(f"❌ {e}")
        print(f"Stopped before {stopping}; nothing was spent or changed.")
        print("=" * 80 + "\n")
        sys.exit(PREFLIGHT_EXIT_CODE)
    for check, detail in result.checks:
        print(f"✓ {check:<18} {detail}")
    print("=" * 80 + "\n")
    return result



def _build_and_store_report(config: IngestionConfig, checks: PreflightResult, store: MongoStore,
                            repo_id: str, fresh: bool = False):
    """
    Build the Temporal report and store it as the repository's current report.

    On failure the last good report is kept, marked with the failure, and the
    error is raised to the caller.
    """
    source_root = config.source.root / repo_id
    inv_config = dataclasses.replace(config.llm, effort=config.report.investigate_effort)
    now = datetime.now(timezone.utc).isoformat()
    try:
        result = build_report(
            LLMClient(config.llm), LLMClient(inv_config), checks.pack, checks.rubric, store, repo_id,
            checks.docs.search if checks.docs else None,
            source_root if source_root.is_dir() else None,
            fresh=fresh,
        )
    except Exception as e:
        previous = store.get_report(repo_id) or {"repo_id": repo_id}
        store.put_report(repo_id, {**previous, "status": "failed", "error": str(e), "attempted_at": now})
        raise
    store.put_report(repo_id, {
        "repo_id": repo_id, "status": "ok", "error": None, "generated_at": now, "attempted_at": now,
        "commit_sha": result.data["commit"], "pack_version": checks.pack.version,
        "rubric_version": checks.rubric.version, "cost_usd": result.data["cost_usd"],
        "data": result.data, "markdown": result.markdown,
    })
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
    "--max-workers",
    type=int,
    default=DEFAULT_MAX_WORKERS,
    show_default=True,
    help="Maximum number of parallel LLM workers. Defaults to INDEX_MAX_WORKERS.",
)
@click.option(
    "--resume-cache",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Path to cached documents pickle file to skip regeneration.",
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
def sync(
    repo_path: Path,
    repo_id: str,
    prefix: str,
    output_dir: Path | None,
    dry_run: bool,
    max_workers: int,
    resume_cache: bool,
    force: bool,
    refresh_lens: bool,
    no_report: bool,
) -> None:
    """
    Synchronize repository state to the semantic index.
    
    Automatically detects full vs incremental mode by querying the index.
    Generates all artifacts in-memory and persists atomically.
    """
    log = logger.bind(mode="sync", repo_id=repo_id, prefix=prefix, dry_run=dry_run)
    log.info("starting_sync")
    
    try:
        # Initialize config
        config = IngestionConfig(
            repo_path=repo_path,
            output_dir=Path("/tmp"),  # Not used during generation
            repo_id=repo_id,
            prefix=prefix,
        )
        log.info("configuration_loaded", repo_id=config.repo_id)

        _require_api_key()
        preflight = _preflight_or_exit(config)

        # Initialize orchestrator and index store
        orchestrator = GenerationOrchestrator(config, max_workers=max_workers)
        if preflight.docs is not None:
            orchestrator.docs_search = preflight.docs.search
        agent_client = MongoStore(config.mongo, config.prefix)
        
        # Run unified sync
        log.info("starting_unified_sync")
        if resume_cache:
            import pickle
            from types import SimpleNamespace
            print(f"\n{'='*80}")
            print(f"RESUMING FROM CACHE: {resume_cache}")
            print(f"{'='*80}\n")
            with open(resume_cache, 'rb') as f:
                result = SimpleNamespace(
                    documents=pickle.load(f),
                    usage=SimpleNamespace(total_cost_usd=0),
                    failed_items=[],
                    delta=None,
                    current_snapshot=None,
                    reset_index=False,
                    lens_updates=[],
                )
            print(f"Loaded {len(result.documents)} documents from cache\n")
        else:
            result = orchestrator.sync(agent_client, dry_run=dry_run, force=force, refresh_lens=refresh_lens)
        
        # Display summary
        print(f"\nTotal cost: ${result.usage.total_cost_usd:.4f}")
        
        if dry_run:
            print("\n" + "=" * 80)
            print("DRY-RUN COMPLETE - No changes made to the index")
            print("=" * 80)
            if result.reset_index:
                print("Would replace the existing index (full re-index)")
            print(f"Would upsert {len(result.documents)} documents")
            if result.lens_updates:
                print(f"Would update the lens of {len(result.lens_updates)} unchanged files")
            if result.failed_items:
                print(f"Failed items: {len(result.failed_items)}")
            print()
            log.info("dry_run_complete")
            return
        
        # Persist to the index
        if result.documents:
            log.info("persisting_to_index", document_count=len(result.documents))
            print("\n" + "=" * 80)
            print("PERSISTING TO INDEX")
            print("=" * 80)
            
            try:
                # A full re-index replaces the old index only now, after generation
                # succeeded, so a failed run never leaves the repository unindexed.
                if result.reset_index:
                    print("Replacing the existing index (full re-index)...")
                    agent_client.reset_repository(repo_id)

                upsert_result = agent_client.upsert_documents_bulk(result.documents)
                if result.lens_updates:
                    updated = agent_client.update_file_lenses(repo_id, result.lens_updates)
                    print(f"Lens refreshed:  {updated} unchanged files")

                print(f"Total processed: {upsert_result['total_processed']}")
                print(f"Inserted:        {upsert_result['summary']['inserted']}")
                print(f"Updated:         {upsert_result['summary']['updated']}")
                print(f"No change:       {upsert_result['summary']['no_change']}")
                print(f"Errors:          {upsert_result['summary']['errors']}")
                print("=" * 80 + "\n")
                
                log.info("index_persistence_complete", **upsert_result)

                # Record how this index was built (layout version, model, commit).
                commit_sha = (
                    result.current_snapshot.commit_sha
                    if result.current_snapshot is not None
                    else result.documents[0].commit_sha
                )
                agent_client.put_index_meta(orchestrator.build_index_meta(
                    commit_sha, agent_client.lens_pack_versions(repo_id), agent_client.prompt_versions(repo_id)
                ))

                # Check for upsert errors
                if upsert_result['summary']['errors'] > 0:
                    print("⚠️  SYNC PARTIALLY FAILED")
                    print("Some documents failed to persist to the index.")
                    print("Re-run the sync command to retry failed operations.")
                    print()

                # ✅ VALIDATE INDEXES AFTER PERSISTENCE
                # Skip validation on full index (indexes don't exist yet)
                if result.delta is None:
                    # Full index - create indexes without validation
                    print("=" * 80)
                    print("CREATING TEXT INDEXES (FULL INDEX MODE)")
                    print("=" * 80)
                    orchestrator._create_indexes_without_validation(agent_client)
                    print("\n" + "=" * 80)
                    print("INDEX CREATION COMPLETE")
                    print("=" * 80 + "\n")
                else:
                    # Incremental - validate and ensure indexes
                    print("=" * 80)
                    print("VALIDATING TEXT INDEXES")
                    print("=" * 80)
                    orchestrator._validate_and_ensure_indexes(agent_client)
                    print("\n" + "=" * 80)
                    print("INDEX VALIDATION COMPLETE")
                    print("=" * 80 + "\n")
                    
                
            except Exception as e:
                log.error("index_persistence_failed", error=str(e))
                print(f"\n❌ Index persistence failed: {e}")
                print("No artifacts written to disk (index persistence must succeed first).")
                print("Re-run the sync command to retry.\n")
                # sys.exit(1)
        else:
            log.info("no_documents_to_persist")
            print("\nNo documents to persist (repository unchanged).")

        # Source mirroring
        log.info("starting_source_mirror")
        mirror_result = orchestrator._run_source_mirror(
            result.current_snapshot,
            result.delta,
            dry_run
        )

        # Temporal report (lens on): after persistence and the source mirror, which it reads.
        # It runs when the index changed or no good report exists; a failure never fails the index.
        if config.lens.enabled and not dry_run and not no_report:
            stored = agent_client.get_report(repo_id)
            if result.documents or result.lens_updates or not stored or stored.get("status") != "ok":
                print("=" * 80)
                print("TEMPORAL REPORT")
                print("=" * 80)
                try:
                    report_result = _build_and_store_report(config, preflight, agent_client, repo_id)
                    print(f"✓ Report stored in the index | cost ${report_result.usage.total_cost_usd:.4f}")
                except Exception as e:
                    log.error("report_failed", error=str(e))
                    print(f"⚠️  Temporal report failed ({e}). The index is complete; "
                          f"run ./scripts/report.sh {repo_id} to retry.")
                print("=" * 80 + "\n")
        
        # Optional disk output (AFTER successful persistence)
        if output_dir and result.documents:
            log.info("writing_artifacts_to_disk", output_dir=str(output_dir))
            print("=" * 80)
            print(f"WRITING ARTIFACTS TO DISK: {output_dir}")
            print("=" * 80)
            
            try:
                writer = DiskWriter(output_dir)
                writer.write_documents(result.documents)
                print(f"✓ Wrote {len(result.documents)} artifacts to disk")
                print("=" * 80 + "\n")
                log.info("disk_output_complete")
            except Exception as e:
                log.error("disk_write_failed", error=str(e))
                print(f"⚠️  Disk write failed: {e}")
                print("(index persistence succeeded, artifacts not written to disk)")
                print()
        
        # Final summary (including source mirror results)
        if result.failed_items or mirror_result.has_failures:
            print("\n" + "=" * 80)
            print("⚠️  SYNC COMPLETED WITH FAILURES")
            print("=" * 80)
            
            # Show semantic generation failures
            if result.failed_items:
                print(f"\n❌ SEMANTIC GENERATION FAILURES: {len(result.failed_items)}")
                print("-" * 80)
                for path, error in result.failed_items:
                    print(f"  • {path}")
                    print(f"    Error: {error}")
            
            # Show source mirror failures
            if mirror_result.has_failures:
                print(f"\n❌ SOURCE MIRROR FAILURES: {mirror_result.total_failures}")
                print("-" * 80)
                
                if mirror_result.failed_uploads:
                    print("Failed uploads:")
                    for path, error in mirror_result.failed_uploads:
                        print(f"  • {path}")
                        print(f"    Error: {error}")
                
                if mirror_result.failed_deletes:
                    print("\nFailed deletions:")
                    for path, error in mirror_result.failed_deletes:
                        print(f"  • {path}")
                        print(f"    Error: {error}")
            
            print()
            print("Re-run the sync command to retry failed operations.")
            print("The system will automatically resume from the current state.")
            print("=" * 80 + "\n")
            
            log.warning(
                "sync_completed_with_failures",
                semantic_failures=len(result.failed_items),
                mirror_failures=mirror_result.total_failures,
            )
            
            # Exit with error code if any failures occurred
            sys.exit(1)
        else:
            print("✓ Sync completed successfully (including source mirror)\n")
            log.info("sync_complete")
        
    except Exception as e:
        log.error("sync_failed", error=str(e))
        print(f"\n❌ Sync failed: {e}\n")
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
    Check the knowledge pack, Anthropic API, MongoDB and (lens on) the Temporal
    Docs MCP. Exits with code 2 on the first failure. `sync` runs this first.
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
    checks = _preflight_or_exit(config, stopping="the report")
    try:
        result = _build_and_store_report(config, checks, MongoStore(config.mongo, prefix), repo_id, fresh)
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
