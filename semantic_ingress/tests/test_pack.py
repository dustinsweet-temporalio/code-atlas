import copy
from pathlib import Path

import pytest
import yaml

from semantic_ingress.lens.pack import PackError, load_pack, validate

PACK_PATH = Path(__file__).resolve().parents[2] / "knowledge" / "temporal-pack.yaml"


@pytest.fixture(scope="module")
def pack():
    return load_pack(PACK_PATH)


def test_repository_pack_is_valid(pack):
    assert pack.id == "temporal-lens-pack"
    assert pack.version
    assert len(pack.hash) == 16


def test_views_are_byte_stable(pack):
    # The view is the cached system-prompt prefix: rebuilding must not change a byte.
    again = load_pack(PACK_PATH)
    for mode, lang in [("conv", None), ("eval", "python"), ("eval", "java")]:
        assert pack.view(mode, lang)[0] == again.view(mode, lang)[0]


def test_conflict_and_stale_entries_are_excluded(pack):
    excluded = {i for i, e in pack.entries.items() if e.get("v") in ("conflict", "stale")}
    assert excluded  # the pack does have some
    _, _, conv_ids = pack.view("conv", None)
    for lang in ["python", "typescript", "go", "java", "dotnet", "ruby", "rust"]:
        _, _, eval_ids = pack.view("eval", lang)
        assert not excluded & (conv_ids | eval_ids)


def test_conversion_view_has_no_temporal_codes_or_review_checks(pack):
    text, codes, ids = pack.view("conv", None)
    assert not any(c.startswith("tmp.") for c in codes)
    assert not any(i.startswith(("E-", "S-")) for i in ids)
    assert "X-RETRY-HAND" in ids and "G-LATENCY" in ids


def test_evaluation_view_includes_only_the_files_language(pack):
    _, _, py_ids = pack.view("eval", "python")
    assert any(i.startswith("S-PY-") for i in py_ids)
    assert not any(i.startswith("S-JAVA-") for i in py_ids)
    assert "E-AO-STC" in py_ids


def test_file_mode_detection(pack):
    assert pack.file_mode("wf.py", "from temporalio import workflow\n") == "eval"
    assert pack.file_mode("wf.py", "import requests\n") == "conv"
    assert pack.file_mode("Wf.java", "import io.temporal.workflow.Workflow;") == "eval"
    assert pack.file_mode("w.ts", "import { proxyActivities } from '@temporalio/workflow';") == "eval"
    # Non-code files never get the review checks, even when they mention the SDK.
    assert pack.file_mode("README.md", "pip install temporalio") == "conv"
    # Mentions outside import lines (strings, comments, docs) do not count.
    assert pack.file_mode("t.py", 'assert f("wf.py", "from temporalio import workflow")\n') == "conv"
    assert pack.file_mode("t.py", "# ported from temporalio samples\n") == "conv"
    assert pack.file_mode("w.go", 'import (\n\t"context"\n\t"go.temporal.io/sdk/workflow"\n)\n') == "eval"
    assert pack.file_mode("w.rb", "class OrderWorkflow < Temporalio::Workflow::Definition\nend\n") == "eval"
    assert pack.file_mode("W.cs", "using Temporalio.Workflows;\n") == "eval"


def test_validation_reports_problems(pack):
    data = copy.deepcopy(pack.data)
    data["checks"].append(dict(data["checks"][0]))  # duplicate id
    data["gates"][0]["on"] = ["no.such.code"]
    del data["meta"]["version"]
    problems = validate(data)
    assert any("duplicate id" in p for p in problems)
    assert any("unknown signal code" in p for p in problems)
    assert any("meta.version" in p for p in problems)


def test_missing_pack_raises(tmp_path):
    with pytest.raises(PackError, match="not found"):
        load_pack(tmp_path / "nope.yaml")


def test_invalid_pack_raises(tmp_path):
    bad = tmp_path / "pack.yaml"
    bad.write_text(yaml.safe_dump({"meta": {"id": "x"}}))
    with pytest.raises(PackError, match="invalid"):
        load_pack(bad)
