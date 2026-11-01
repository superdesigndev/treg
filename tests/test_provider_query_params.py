"""A provider-wide query parameter is declared once at the file header and reaches every endpoint's
`input.queryParams`, so a caller reading any row learns where it goes (query, not body)."""

from treg.domain.catalog import store as cs

ROW = ("  - id: {id}\n    platform: web\n    tier: {tier}\n    method: POST\n    path: /v1/run/{id}\n"
       "    summary: Run\n    input:\n      body: {{q: {{type: string, required: true}}}}\n{extra}")


def _write(tmp_path, name, header, rows):
    (tmp_path / name).write_text("provider: demo\n" + header + "endpoints:\n" + "".join(rows))


def test_header_query_params_merge_into_every_row_and_the_row_wins(tmp_path):
    (tmp_path / "capabilities.yaml").write_text("platforms: {web: Web}\ncapabilities: {}\n")
    _write(tmp_path, "demo.yaml",
           "queryParams:\n  jq: {type: string, note: core}\n  fields: {type: string, note: core}\n",
           [ROW.format(id="demo.core", tier="core", extra=""),
            ROW.format(id="demo.own", tier="core",
                       extra="      queryParams: {jq: {type: string, note: own}}\n")])
    # The extended file sorts first; the core file's block still wins.
    _write(tmp_path, "demo.extended.yaml", "queryParams:\n  jq: {type: string, note: extended}\n",
           [ROW.format(id="demo.ext", tier="extended", extra="")])
    cat = cs.load(directory=tmp_path)
    q = {i: cat.by_id[i]["input"]["queryParams"] for i in ("demo.core", "demo.own", "demo.ext")}
    assert q["demo.core"] == {"jq": {"type": "string", "note": "core"}, "fields": {"type": "string", "note": "core"}}
    assert q["demo.ext"] == q["demo.core"]
    assert q["demo.own"]["jq"]["note"] == "own" and q["demo.own"]["fields"]["note"] == "core"
    assert cat.by_id["demo.core"]["input"]["body"]["q"]["required"] is True


def test_no_header_block_leaves_rows_untouched(tmp_path):
    (tmp_path / "capabilities.yaml").write_text("platforms: {web: Web}\ncapabilities: {}\n")
    _write(tmp_path, "demo.yaml", "", [ROW.format(id="demo.core", tier="core", extra="")])
    assert "queryParams" not in cs.load(directory=tmp_path).by_id["demo.core"]["input"]
