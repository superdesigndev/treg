import json

from scripts import catalog_dump


def test_the_dump_is_canonical_json_of_every_loaded_catalog_field():
    text = catalog_dump.dump()
    doc = json.loads(text)
    assert text == catalog_dump.dump()
    assert {"by_id", "endpoints", "cost_views", "contracts", "adapters", "provider_meta"} <= doc.keys()
    assert set(doc["endpoints"]) <= doc["by_id"].keys() == doc["cost_views"].keys()


def test_compare_names_what_differs_and_fails(tmp_path, capsys):
    base = {"by_id": {"a.x": {"cost": 1}, "a.y": {}}, "fx": {"USD": 1.0}}
    candidate = {"by_id": {"a.x": {"cost": 2}, "a.y": {}}, "fx": {"USD": 1.0}}
    assert catalog_dump.compare(base, candidate) == ["first difference: by_id.a.x.cost", "by_id: 1 differ: a.x"]
    (tmp_path / "b.json").write_text(json.dumps(base))
    (tmp_path / "c.json").write_text(json.dumps(candidate))
    assert catalog_dump.main(["--compare", str(tmp_path / "b.json"), str(tmp_path / "c.json")]) == 1
    assert "first difference: by_id.a.x.cost" in capsys.readouterr().out
    assert catalog_dump.main(["--compare", str(tmp_path / "b.json"), str(tmp_path / "b.json")]) == 0
    assert "byte-identical" in capsys.readouterr().out


def test_a_declared_path_passes_and_any_other_difference_still_fails(tmp_path, capsys):
    base = {"by_id": {"a.x": {"cost": 1}, "a.y": {}}, "fx": {"USD": 1.0}}
    noted = {"by_id": {"a.x": {"cost": 1, "note": "n"}, "a.y": {"note": "m"}}, "fx": {"USD": 1.0}}
    body = tmp_path / "body.md"
    body.write_text("Why.\n\n```dump-declared\nby_id.*.note  # the loader serves a row's note\n```\n")
    assert catalog_dump.declared_paths(body.read_text()) == ["by_id.*.note"]
    for name, doc in (("b", base), ("c", noted), ("d", noted | {"fx": {"USD": 2.0}})):
        (tmp_path / f"{name}.json").write_text(json.dumps(doc))
    compare = ["--compare", str(tmp_path / "b.json")]
    assert catalog_dump.main([*compare, str(tmp_path / "c.json"), "--declared-in", str(body)]) == 0
    assert "declared by_id.*.note: 2 difference(s)" in capsys.readouterr().out
    assert catalog_dump.main([*compare, str(tmp_path / "c.json")]) == 1
    capsys.readouterr()
    assert catalog_dump.main([*compare, str(tmp_path / "d.json"), "--declared-in", str(body)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "declared by_id.*.note: 2 difference(s)", "first difference: fx.USD", "fx: 1 differ: USD"]


def test_a_dataclass_field_at_its_default_is_left_out():
    import dataclasses

    @dataclasses.dataclass
    class Row:
        id: str
        note: str = ""
        tags: list = dataclasses.field(default_factory=list)

    assert catalog_dump._plain(Row("a")) == {"id": "a"}
    assert catalog_dump._plain(Row("a", note="n", tags=["t"])) == {"id": "a", "note": "n", "tags": ["t"]}
    # a set value that falls back to its default is a missing key, so compare still names it
    old, new = (json.loads(json.dumps({"r": r}, default=catalog_dump._plain)) for r in (Row("a", note="n"), Row("a")))
    assert catalog_dump.first_difference(old, new) == "r.note"


def test_first_difference_walks_lists_and_names_a_missing_key():
    assert catalog_dump.first_difference({"a": [1, {"b": 2}]}, {"a": [1, {"b": 3}]}) == "a.1.b"
    assert catalog_dump.first_difference({"a": {"x": 1}}, {"a": {"x": 1, "y": 2}}) == "a.y"
    assert catalog_dump.first_difference({"a": [1]}, {"a": [1, 2]}) == "a"
    assert catalog_dump.first_difference({"a": 1}, {"a": 1}) is None


def test_the_dump_reads_another_catalog_directory(tmp_path):
    (tmp_path / "capabilities.yaml").write_text("platforms: {web: Web}\ncapabilities: {}\n")
    (tmp_path / "demo.yaml").write_text("provider: demo\nendpoints:\n  - {id: demo.x, platform: web}\n")
    assert catalog_dump.main([str(tmp_path / "out.json"), "--catalog", str(tmp_path)]) == 0
    assert list(json.loads((tmp_path / "out.json").read_text())["by_id"]) == ["demo.x"]
