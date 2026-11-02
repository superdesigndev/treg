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
    assert catalog_dump.compare(base, candidate) == ["by_id: 1 differ: a.x"]
    (tmp_path / "b.json").write_text(json.dumps(base))
    (tmp_path / "c.json").write_text(json.dumps(candidate))
    assert catalog_dump.main(["--compare", str(tmp_path / "b.json"), str(tmp_path / "c.json")]) == 1
    assert catalog_dump.main(["--compare", str(tmp_path / "b.json"), str(tmp_path / "b.json")]) == 0
    assert "byte-identical" in capsys.readouterr().out
