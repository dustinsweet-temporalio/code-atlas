"""One test per failure seen in the 2026-10-16 calibration runs, plus the rel pinning rules."""
from semantic_ingress.lens.models import FileLens
from semantic_ingress.lens.normalize import REL0_PREFIX, normalize

CODES = {"effect.http", "effect.llm", "reliab.retry", "hazard.io", "hazard.env", "flaky.api",
         "tmp.wf_def", "tmp.act_def", "tmp.act_options"}
IDS = {"X-RETRY-HAND", "P-LLM-ACTIVITY", "E-AO-STC", "E-RP-MAX1", "E-ENT-NOCAN", "S-PY-SLEEP"}


def lens(**over) -> FileLens:
    base = dict(rel=2, cov="full", kind=["ACT"], sig=["effect.http:stripe@L10-20"], sym=[],
                c=["X-RETRY-HAND"], f=[], n="Calls the payment API and retries on failure.")
    base.update(over)
    return FileLens(**base)


def test_clean_record_passes_unchanged():
    r = normalize(lens(), CODES, IDS)
    assert r.problems == [] and not r.retry
    assert r.lens == lens()


def test_narrative_slightly_over_cap_is_accepted():
    r = normalize(lens(n=" ".join(["word"] * 120)), CODES, IDS)  # cap 110, under +20%
    assert not r.retry and any("narrative" in p for p in r.problems)


def test_narrative_far_over_cap_asks_for_one_retry():
    r = normalize(lens(n=" ".join(["word"] * 140)), CODES, IDS)  # > 110 * 1.2
    assert r.retry


def test_invented_concept_id_is_dropped():
    r = normalize(lens(c=["X-RETRY-HAND", "P-SIG-VS-UPDATE"]), CODES, IDS)
    assert r.lens.c == ["X-RETRY-HAND"]


def test_duplicate_finding_is_kept_once():
    r = normalize(lens(kind=["TWF"], rel=3, sig=["tmp.wf_def@L1-9"],
                       f=["E-AO-STC@L29", "E-AO-STC@L62", "E-RP-MAX1@L48"]), CODES, IDS)
    assert r.lens.f == ["E-AO-STC@L29", "E-RP-MAX1@L48"]


def test_hazard_on_plain_adapter_is_dropped():
    r = normalize(lens(sig=["effect.llm:anthropic@L1-5", "hazard.env@L63"]), CODES, IDS)
    assert r.lens.sig == ["effect.llm:anthropic@L1-5"]


def test_hazard_kept_in_orchestration_code():
    r = normalize(lens(kind=["ORCH"], rel=3, sig=["hazard.io@L5"]), CODES, IDS)
    assert r.lens.sig == ["hazard.io@L5"]


def test_signal_without_anchor_dropped_at_rel_2():
    r = normalize(lens(sig=["effect.http@L3", "flaky.api:payer EHR"]), CODES, IDS)
    assert r.lens.sig == ["effect.http@L3"]


def test_unknown_signal_code_dropped():
    r = normalize(lens(sig=["effect.http@L3", "made.up@L4"]), CODES, IDS)
    assert r.lens.sig == ["effect.http@L3"]


def test_findings_cleared_on_non_temporal_file():
    r = normalize(lens(f=["E-AO-STC@L1"]), CODES, IDS)
    assert r.lens.f == []


def test_rel_pinned_for_workflow_and_activity_code():
    assert normalize(lens(kind=["TWF"], rel=2, sig=["tmp.wf_def@L1"]), CODES, IDS).lens.rel == 3
    assert normalize(lens(kind=["TACT"], rel=3, sig=["tmp.act_def@L1"]), CODES, IDS).lens.rel == 2
    assert normalize(lens(kind=["TACT", "ORCH"], rel=2, sig=["tmp.act_def@L1"]), CODES, IDS).lens.rel == 3


def test_rel_0_is_reformatted_and_emptied():
    r = normalize(lens(rel=0, kind=["NONE"], c=["X-RETRY-HAND"], n="Just a package marker file with version."), CODES, IDS)
    assert r.lens.n.startswith(REL0_PREFIX)
    assert len(r.lens.n[len(REL0_PREFIX):].split()) <= 8
    assert r.lens.sig == [] and r.lens.c == []


def test_symbols_cleared_below_rel_2():
    r = normalize(lens(rel=1, sig=[], sym=[{"n": "f", "k": "ACT"}]), CODES, IDS)
    assert r.lens.sym == []
