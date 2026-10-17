import dataclasses
import uuid
from pathlib import Path

import pygit2
import pytest
import pytest_asyncio
from temporalio.testing import WorkflowEnvironment

from semantic_ingress.config import IngestionConfig, SourceStoreConfig
from semantic_ingress.core.git_adapter import GitAdapter
from semantic_ingress.preflight import PreflightResult
from semantic_ingress.temporal.activities import IndexActivities
from semantic_ingress.temporal.constants import task_queue
from semantic_ingress.temporal.models import SyncParams, SyncWorkflowInput
from semantic_ingress.temporal.worker import build_worker
from semantic_ingress.temporal.workflow import SyncWorkflow
from tests.temporal.fakes import FakeLLM, FakeStaging, FakeStore


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def env():
    """One time-skipping Temporal test server for the whole session."""
    environment = await WorkflowEnvironment.start_time_skipping()
    yield environment
    await environment.shutdown()


def commit_files(repo_path: Path, files: dict[str, str | None], message: str = "commit") -> str:
    """Write (or, for None, delete) files and commit them. Creates the repository if needed."""
    if not (repo_path / ".git").exists():
        repo_path.mkdir(parents=True, exist_ok=True)
        pygit2.init_repository(str(repo_path))
    repo = pygit2.Repository(str(repo_path))
    for name, content in files.items():
        target = repo_path / name
        if content is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    sig = pygit2.Signature("Test", "test@example.com")
    parents = [] if repo.head_is_unborn else [repo.head.target]
    return str(repo.create_commit("HEAD", sig, sig, message, tree, parents))


@dataclasses.dataclass
class Harness:
    """Real activities over fakes (store, staging, LLM) and a real git repository."""

    repo_path: Path
    source_dir: Path
    store: FakeStore
    staging: FakeStaging
    llm: FakeLLM
    repo_id: str

    def config(self) -> IngestionConfig:
        config = IngestionConfig(repo_path=self.repo_path, output_dir=self.source_dir, prefix="semv2",
                                 repo_id=self.repo_id)
        config.lens = dataclasses.replace(config.lens, enabled=False)
        config.source = SourceStoreConfig(root=self.source_dir)
        return config

    def activities(self) -> IndexActivities:
        config = self.config()
        return IndexActivities(config, PreflightResult(), self.store, self.staging, self.llm,
                               git=GitAdapter(config.repo_path, config.file_filter))

    def params(self, **over) -> SyncParams:
        base = dict(repo_id=self.repo_id, prefix="semv2", repo_path=str(self.repo_path), lens_enabled=False,
                    no_report=True)
        base.update(over)
        return SyncParams(**base)

    async def run(self, env, window_size: int = 5, **over):
        params = self.params(window_size=window_size, **over)
        worker, executor = build_worker(env.client, task_queue(self.repo_id), self.activities().all(), window_size)
        try:
            async with worker:
                return await env.client.execute_workflow(
                    SyncWorkflow.run, SyncWorkflowInput(params=params),
                    id=f"sync-{self.repo_id}-{uuid.uuid4().hex[:6]}", task_queue=task_queue(self.repo_id),
                )
        finally:
            executor.shutdown(wait=False)


@pytest.fixture
def harness(tmp_path) -> Harness:
    return Harness(
        repo_path=tmp_path / "repo",
        source_dir=tmp_path / "source",
        store=FakeStore(),
        staging=FakeStaging(),
        llm=FakeLLM(),
        repo_id=f"demo{uuid.uuid4().hex[:6]}",
    )
