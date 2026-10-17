"""Lens state shown in the UI and listings (code_atlas/utils/formatters/lens.py)."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "lens", Path(__file__).resolve().parents[1] / "utils" / "formatters" / "lens.py"
)
lens = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lens)


class FakeDb(dict):
    def __getitem__(self, name):
        return FakeCollection(self.get(name))


class FakeCollection:
    def __init__(self, doc):
        self.doc = doc

    def find_one(self, _query):
        return self.doc


def info(meta):
    return lens.index_lens_info(FakeDb({"index_meta": meta}))


def test_no_index_meta_is_off():
    assert info(None)["lens"] == "off"


def test_lens_disabled_is_off():
    assert info({"lens_enabled": False, "lens_pack_versions": [{"version": None, "files": 9}]})["lens"] == "off"


def test_lens_enabled_but_no_file_has_one_yet_is_partial():
    i = info({"lens_enabled": True, "pack": {"version": "0.1.0"}, "lens_pack_versions": [{"version": None, "files": 9}]})
    assert i["lens"] == "partial" and i["pack_version"] == "0.1.0"
    assert lens.describe_lens_info(i).startswith("Temporal lens: partial (pack 0.1.0")


def test_some_files_without_lens_is_partial():
    i = info({"lens_enabled": True, "lens_pack_versions": [{"version": None, "files": 2}, {"version": "0.1.0", "files": 7}]})
    assert i["lens"] == "partial"


def test_two_versions_is_mixed():
    i = info({"lens_enabled": True, "lens_pack_versions": [{"version": "0.1.0", "files": 1}, {"version": "0.1.1", "files": 15}]})
    assert i["lens"] == "mixed"
    assert [v["version"] for v in i["pack_versions"]] == ["0.1.0", "0.1.1"]


def test_uniform_is_on():
    i = info({"lens_enabled": True, "pack": {"version": "0.1.1"}, "lens_pack_versions": [{"version": "0.1.1", "files": 16}]})
    assert i["lens"] == "on"
    assert lens.describe_lens_info(i) == "Temporal lens: on (pack 0.1.1)"


def test_summary_prompt_versions_are_passed_through_for_the_mixed_marker():
    meta = {"prompt_versions": [{"version": None, "files": 72}, {"version": 2, "files": 38}, {"version": 3, "files": 0}]}
    assert info(meta)["prompt_versions"] == [{"version": None, "files": 72}, {"version": 2, "files": 38}]
    assert info(None)["prompt_versions"] == []
