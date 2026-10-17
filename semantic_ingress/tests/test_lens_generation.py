from pathlib import Path

import pytest

from semantic_ingress.lens.models import FileLens, FileLensRecord, RepoLens
from semantic_ingress.lens.pack import load_pack, repo_view
from semantic_ingress.lens.repo_pass import _check
from semantic_ingress.lens.rollup import compact_rollup, compute_rollup, max_rel
from semantic_ingress.lens.tier0 import scan

PACK_PATH = Path(__file__).resolve().parents[2] / "knowledge" / "temporal-pack.yaml"


@pytest.fixture(scope="module")
def pack():
    return load_pack(PACK_PATH)


def lens(rel, kind=("ACT",), sig=(), c=()):
    return FileLens(rel=rel, cov="full", kind=list(kind), sig=list(sig), sym=[], c=list(c), f=[], n="x")


def test_rollup_sums_files_and_child_rollups():
    child = compute_rollup({"a/x.py": lens(3, ("ORCH",), ["effect.http@L1"], ["X-RETRY-HAND"])}, [], "0.1.0")
    parent = compute_rollup(
        {"y.py": lens(0, ("NONE",)), "wf.py": lens(3, ("TWF",), ["tmp.wf_def@L1", "effect.http@L9"])},
        [child], "0.1.0",
    )
    assert parent.files == 3 and parent.rel_counts == [1, 0, 0, 2]
    assert {e.key: e.files for e in parent.signal_counts} == {"effect.http": 2, "tmp.wf_def": 1}
    assert parent.core_files == ["a/x.py", "wf.py"]
    assert parent.temporal_files == 1
    assert max_rel(parent) == 3
    assert compact_rollup(parent).startswith("files 3 rel3:2 rel2:0 rel1:0 rel0:1 temporal:1")


def test_tier0_scan_reads_manifests_and_import_lines_only(pack):
    facts = scan(pack, {
        "requirements.txt": "temporalio==1.9\ncelery==5\n",
        "app/wf.py": "from temporalio import workflow\n",
        "app/tasks.py": "# we used to run this on celery\nimport os\n",  # comment only: not evidence
        "dags/etl.py": "from airflow import DAG\n",
        "README.md": "Uses airflow and temporalio.",  # not code, not a manifest
    })
    assert facts.temporal_counts == {"python": 1}
    assert facts.temporal_manifests == ["requirements.txt"]
    assert facts.adjacent == {"celery": ["requirements.txt"], "airflow": ["dags/etl.py"]}


def _repo_lens(**over):
    base = dict(status="full", archetype="service", detected=[], processes=[], anti=[], n="x", cites=[])
    base.update(over)
    return RepoLens(**base)


def test_repo_check_enforces_facts_paths_gates_and_citations(pack):
    _, ids = repo_view(pack)
    facts = scan(pack, {"dags/etl.py": "from airflow import DAG\n"})
    out, corrections = _check(
        _repo_lens(
            processes=[
                {"name": "ETL", "entry": "dags/etl.py", "files": ["dags/etl.py", "nope.py"],
                 "signals": [], "concepts": ["X-ADJ-ORCHESTRATOR", "MADE-UP"], "narrative": "n"},
                {"name": "Ghost", "entry": "ghost.py", "files": [], "signals": [], "concepts": [], "narrative": "n"},
            ],
            anti=[{"id": "G-LATENCY", "evidence": ["dags/etl.py"]}, {"id": "X-CRON", "evidence": []}],
            cites=[{"ref": "V-DURABLE", "supports": "s"}, {"ref": "https://example.com/x", "supports": "s"},
                   {"ref": "https://docs.temporal.io/ok", "supports": "s"}],
        ),
        pack, ids, facts, known_paths={"dags/etl.py"}, urls={"https://docs.temporal.io/ok"},
    )
    assert out.status == "adjacent"  # no Temporal files, airflow present
    assert [d.name for d in out.detected] == ["airflow"]
    assert [p.name for p in out.processes] == ["ETL"]
    assert out.processes[0].files == ["dags/etl.py"]
    assert out.processes[0].concepts == ["X-ADJ-ORCHESTRATOR"]
    assert [a.id for a in out.anti] == ["G-LATENCY"]
    assert [c.ref for c in out.cites] == ["V-DURABLE", "https://docs.temporal.io/ok"]
    assert corrections


def test_repo_check_raises_status_when_temporal_files_exist(pack):
    _, ids = repo_view(pack)
    facts = scan(pack, {"wf.py": "from temporalio import workflow\n"})
    out, _ = _check(_repo_lens(status="none"), pack, ids, facts, {"wf.py"}, set())
    assert out.status == "partial"


class LensFakeGenerator:
    def __init__(self):
        self.with_lens: list[str] = []

    def generate_folder_summary(self, path, files, folders):
        from semantic_ingress.core.llm_client import LLMUsage
        from semantic_ingress.models import FolderSemanticSummary
        return FolderSemanticSummary(path=path, summary="s"), LLMUsage(input_tokens=0, output_tokens=0)

    def generate_folder_summary_with_lens(self, path, files, folders, file_lines, folder_lines):
        self.with_lens.append(path)
        summary, usage = self.generate_folder_summary(path, files, folders)
        return summary, usage, "Multi-step work happens in wf.py."


def test_folder_lens_written_only_where_something_is_relevant(pack):
    from tests.test_incremental_folders import _file, _orchestrator

    orch = _orchestrator()
    orch.pack = pack
    for path, rec in {"core/wf.py": lens(3, ("ORCH",)), "docs/a.md": lens(0, ("NONE",))}.items():
        orch._file_lenses[path] = FileLensRecord(**rec.model_dump(), mode="conv", lang=None, pack_version=pack.version)
    gen = LensFakeGenerator()
    orch._generate_folder_artifacts(gen, {"core/wf.py": _file("a"), "docs/a.md": _file("b")})

    assert sorted(gen.with_lens) == [".", "core"]  # docs/ has nothing rel >= 2: plain summary
    assert orch._folder_rollups["docs"].n == ""
    assert orch._folder_rollups[""].files == 2
    assert orch._folder_rollups[""].n == "Multi-step work happens in wf.py."
