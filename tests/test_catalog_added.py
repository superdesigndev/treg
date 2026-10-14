"""`added:` on every catalog tool row: the helper's line insert, the validator, the base guard and
the ingest carry (docs/context/architecture/catalog.md)."""

import subprocess
from datetime import UTC, datetime, timedelta

import yaml

from scripts import catalog_added as helper
from scripts import catalog_ingest as ingest
from scripts import catalog_validate as validator

CORE = """provider: example
# a hand-written comment the helper must keep
endpoints:
  - id: example.one
    method: GET
    path: /one   # inline comment
    verified: '2026-09-01'
    example_response: examples/example.one.json
  - id: example.two
    method: GET
    path: /two
  - id: example.three
    added: '2026-08-01'
    method: GET
    path: /three
"""


def test_helper_inserts_one_line_next_to_verified_or_after_id_and_keeps_the_rest():
    new, placed, unplaced = helper.insert_added(CORE, {"example.one": "2026-10-01",
                                                       "example.two": "2026-10-02",
                                                       "example.three": "2030-01-01"})
    assert placed == ["example.one", "example.two"] and unplaced == []
    assert new == CORE.replace(
        "    verified: '2026-09-01'\n", "    verified: '2026-09-01'\n    added: '2026-10-01'\n",
    ).replace("  - id: example.two\n", "  - id: example.two\n    added: '2026-10-02'\n")
    rows = {r["id"]: r for r in yaml.safe_load(new)["endpoints"]}
    assert rows["example.three"]["added"] == "2026-08-01"  # an existing date is never touched


def test_helper_extended_layout_and_rows_it_cannot_date():
    text = "provider: example\nendpoints:\n- id: example.x.a\n  tier: extended\n- id: example.x.b\n"
    new, placed, unplaced = helper.insert_added(text, {"example.x.a": "2026-10-01"})
    assert placed == ["example.x.a"] and unplaced == ["example.x.b"]
    assert "- id: example.x.a\n  added: '2026-10-01'\n  tier: extended\n" in new


def test_helper_writes_todays_utc_date_only_where_missing(tmp_path, monkeypatch, capsys):
    (tmp_path / "example.yaml").write_text(CORE)
    monkeypatch.setattr(helper, "CATALOG", tmp_path)
    assert helper.main(["--check"]) == 1
    assert helper.main([]) == 0
    today = datetime.now(UTC).date().isoformat()
    rows = {r["id"]: r["added"] for r in yaml.safe_load((tmp_path / "example.yaml").read_text())["endpoints"]}
    assert rows == {"example.one": today, "example.two": today, "example.three": "2026-08-01"}
    assert helper.main(["--check"]) == 0


def test_validator_requires_a_valid_past_added_date():
    future = (datetime.now(UTC).date() + timedelta(days=2)).isoformat()
    for value, ok in ((None, False), ("soon", False), ("2026-9-1", False), (future, False),
                      ("2026-09-01", True)):
        errors: list[str] = []
        validator.check_added({"added": value} if value else {}, "f.yaml:example.one", errors)
        assert (errors == []) is ok, (value, errors)
        if not ok:
            assert "example.one" in errors[0] and ("catalog_added.py" in errors[0] or "future" in errors[0])


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                   cwd=cwd, check=True, capture_output=True)


def test_base_guard_compares_by_id_across_files(tmp_path, monkeypatch):
    catalog = tmp_path / "src" / "treg" / "catalog"
    catalog.mkdir(parents=True)
    (catalog / "example.yaml").write_text(CORE.replace("    path: /two\n", "    path: /two\n    added: '2026-08-02'\n"))
    (catalog / "example.extended.yaml").write_text(
        "provider: example\nendpoints:\n- id: example.x.moved\n  added: '2026-07-28'\n")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    monkeypatch.setattr(helper, "ROOT", tmp_path)

    # promoted from extended to core with its date: passes; a brand-new id: passes
    (catalog / "example.extended.yaml").write_text("provider: example\nendpoints: []\n")
    (catalog / "example.yaml").write_text(
        (catalog / "example.yaml").read_text()
        + "  - id: example.x.moved\n    added: '2026-07-28'\n  - id: example.new\n    added: '2026-10-01'\n")
    assert helper.changed_dates("main", catalog) == []

    # a changed date fails and names the id; the label override passes
    (catalog / "example.yaml").write_text(
        (catalog / "example.yaml").read_text().replace("added: '2026-08-02'", "added: '2026-09-09'"))
    changes = helper.changed_dates("main", catalog)
    assert len(changes) == 1 and changes[0].startswith("example.two: added 2026-08-02")
    monkeypatch.setattr(helper, "CATALOG", catalog)
    assert helper.main(["--base", "main"]) == 1
    assert helper.main(["--base", "main", "--allow-change"]) == 0


def test_ingest_carries_added_by_id_even_when_the_route_moved(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "CATALOG", tmp_path)
    (tmp_path / "example.yaml").write_text(
        "provider: example\nendpoints:\n  - id: example.core\n    added: '2026-08-01'\n")
    (tmp_path / "example.extended.yaml").write_text(
        "provider: example\nendpoints:\n"
        "- id: example.x.same\n  method: GET\n  path: /same\n  verified: '2026-08-05'\n  added: '2026-08-02'\n"
        "- id: example.x.moved\n  method: GET\n  path: /old\n  added: '2026-08-03'\n")
    endpoints = [
        {"id": "example.x.same", "method": "GET", "path": "/same", "summary": "s"},
        {"id": "example.x.moved", "method": "GET", "path": "/new", "summary": "m"},
        {"id": "example.core", "method": "GET", "path": "/c", "summary": "c"},
        {"id": "example.x.fresh", "method": "GET", "path": "/f", "summary": "f"},
    ]
    ingest.carry_verification("example", endpoints)
    by_id = {e["id"]: e for e in endpoints}
    assert by_id["example.x.same"]["added"] == "2026-08-02"
    assert list(by_id["example.x.same"]).index("added") == list(by_id["example.x.same"]).index("verified") + 1
    assert by_id["example.x.moved"]["added"] == "2026-08-03"
    assert "verified" not in by_id["example.x.moved"]
    assert by_id["example.core"]["added"] == "2026-08-01"
    assert by_id["example.x.fresh"]["added"] == datetime.now(UTC).date().isoformat()
    assert list(by_id["example.x.fresh"])[:2] == ["id", "added"]
