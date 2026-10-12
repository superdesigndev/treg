"""Turn one treg answer into rows and columns (`POST /table/<tool id>`, for spreadsheets).

Pure and stdlib-only: the parsed JSON answer goes in, with what the catalog knows about the tool,
and a table comes out. No HTTP, no database, no money: the call is already made and settled by the
time this runs (`application/table.py`). Design: tools-gsheet `docs/decisions.md` round 1 q1,
round 2 q9, round 3 B and F.

Four kinds of tool, four sources of columns (`column_source`):

- a routed job with a flat contract output (people.email.verify): the contract's fields, in the
  contract's order, then `served_by`; one row, none on a miss;
- a routed job whose contract output has a required list (people.search -> `people`): one row per
  item; items are the provider's own objects, so a people-shaped list maps common names to fixed
  columns first (`LIST_MAPS`, data, not code), then every other field under its own name;
- a hub tool: its manifest's output fields, one row;
- anything else (`generated`): the lists of objects found in the answer decide the shape: none is
  `flat`, one is `list`, two or more are `nested` (`tables` + `summary`).
"""

from __future__ import annotations

import json
from typing import Any

MAX_DEPTH = 3               # a nested object flattens to `a.b.c` columns, no deeper
MAX_WALK = 4                # how deep the search for lists of objects goes
RAW_TEXT_BYTES = 64 * 1024  # the most text a `raw` table carries

# A list holding exactly one object under one of these names is a wrapper, not a table: the search
# for tables goes inside it (seranking `summary[0]`, DataForSEO `tasks[0].result[0]`). Data, so a
# provider's wrapper is one word here, not a code branch.
ENVELOPE_KEYS = frozenset({"summary", "tasks", "result", "results", "data", "response", "response_data"})

# Provider-native rows of a routed list job, mapped to fixed columns first. Each entry: the column,
# then the item paths that may hold it, first match wins. Keyed by the contract's list field.
_PEOPLE_MAP: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("first_name", ("first_name", "firstname", "firstName", "profile.first_name", "person.first_name")),
    ("last_name", ("last_name", "lastname", "lastName", "last_name_obfuscated", "profile.last_name", "person.last_name")),
    # `headline` last: a profile tagline, used only when no job title field exists
    ("title", ("title", "job_title", "jobTitle", "jobTitle.title", "position", "profile.title",
               "basic_profile.current_title", "lastJobTitle", "person.current_job_title",
               "currentPositions.0.title", "headline")),
    ("company", ("company", "company_name", "companyName", "organization_name", "organization.name",
                 "company.name", "job_company_name", "lastCompanyName", "companyName",
                 "currentPositions.0.companyName")),
    ("linkedin_url", ("linkedin_url", "linkedinUrl", "profileUrl", "linkedin", "employee_linkedin",
                      "URLs.linkedin", "socials.linkedin_url", "link.linkedin", "socialLinks.linkedin",
                      "person.linkedin_url", "profile_url",
                      "social_handles.professional_network_identifier.profile_url")),
    ("location", ("location", "location_name", "basic_profile.location.full_location", "location.linkedinText",
                  "location.address", "city", "location.city", "address", "location.country",
                  "person.location.country", "country_code")),
)
# `title` and `url` come last: exa names a company `title` and its homepage `url`, while other
# providers' `url` is the LinkedIn page, read only after every domain field has been tried
_COMPANIES_MAP: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("name", ("name", "company_name", "basic_info.name", "company.name", "about.name", "organization",
              "display_name", "title")),
    ("domain", ("domain", "company_domain", "basic_info.primary_domain", "company.domain", "domain.domain",
                "email_domain", "website", "website_url", "company_website", "company.website", "URLs.website",
                "websiteUrl", "url")),
    ("industry", ("industry", "company_industry_linkedin", "company.industry", "about.industry", "industryList",
                  "product_category")),
    ("employees", ("employee_count", "employees", "company.employee_count", "linkedin_employee_count",
                   "numberOfEmployees", "about.totalEmployeesExact", "employee_range", "size",
                   "about.totalEmployees", "company_size", "entities.0.properties.workforce.total")),
    ("location", ("location.name", "location_name", "headquarter", "hq_city", "company.location.city", "city",
                  "locality", "location", "address", "location.country", "country",
                  "locations.headquarters.city.name", "entities.0.properties.headquarters.address")),
    ("linkedin_url", ("linkedin_url", "linkedinUrl", "company.linkedin_url", "URLs.linkedin",
                      "linkedin_profile_url", "socials.linkedin.url")),
)
LIST_MAPS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {"people": _PEOPLE_MAP,
                                                                  "companies": _COMPANIES_MAP}


# ------------------------------------------------------------------------------------------------
# Cells and records

def cell(value: Any) -> Any:
    """One value as a spreadsheet cell: a scalar as it is; a list of scalars joined with ", "
    (decision B); an object, or a list holding objects, as compact JSON."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list) and all(v is None or isinstance(v, (bool, int, float, str)) for v in value):
        return ", ".join("" if v is None else str(v) for v in value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _hidden(key: Any) -> bool:
    return isinstance(key, str) and key.startswith("_")


def flatten(obj: Any, prefix: str = "", depth: int = 0) -> dict[str, Any]:
    """An object as `{column: cell}`: nested objects become `a.b` columns down to MAX_DEPTH, and a
    field whose name starts with `_` (treg's own `_treg`, a provider's `_note`) is never a column."""
    if not isinstance(obj, dict):
        return {prefix or "value": cell(obj)}
    out: dict[str, Any] = {}
    for key, value in obj.items():
        if _hidden(key):
            continue
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and value and depth + 1 < MAX_DEPTH:
            out.update(flatten(value, name, depth + 1))
        else:
            out[name] = cell(value)
    return out


def _get_path(obj: Any, path: str) -> tuple[bool, Any]:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list) and part.isdigit() and int(part) < len(cur):   # `entities.0.name`
            cur = cur[int(part)]
        elif isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def _grid(records: list[dict[str, Any]], fixed: list[str] | None = None) -> tuple[list[str], list[list[Any]]]:
    """Records as columns and rows: the fixed columns first, then every other key in first-seen order."""
    columns: list[str] = list(fixed or [])
    seen = set(columns)
    for rec in records:
        for key in rec:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    return columns, [[rec.get(c) for c in columns] for rec in records]


# The saved example answers (`src/treg/catalog/examples/`) end a long list with this marker string,
# written by `scripts/catalog_verify.py`; a real answer never has it. Skipped, so an example reads
# like the answer it was cut from.
_TRUNCATION_MARK = " more item(s) truncated"


def _items(value: list) -> list:
    return [v for v in value if not (isinstance(v, str) and v.endswith(_TRUNCATION_MARK))]


def _is_record_list(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    items = _items(value)
    return bool(items) and all(isinstance(v, dict) for v in items)


# ------------------------------------------------------------------------------------------------
# The four kinds of tool

def from_contract(body: Any, output_fields: list[str], list_field: str | None) -> dict[str, Any]:
    """A routed job's answer, `{output, raw, _treg}`. `list_field` names the contract's required
    list, when it has one."""
    body = body if isinstance(body, dict) else {}
    output = body.get("output") if isinstance(body.get("output"), dict) else {}
    meta = body.get("_treg") if isinstance(body.get("_treg"), dict) else {}
    served_by = meta.get("served_by")
    miss = meta.get("outcome") == "miss"
    if list_field:
        items = output.get(list_field) if not miss else None
        items = _items(items) if isinstance(items, list) else []
        mapping = LIST_MAPS.get(list_field)
        records = [_mapped(item, mapping) if isinstance(item, dict) else {"value": cell(item)} for item in items]
        columns, rows = _grid(records, [c for c, _ in mapping] if mapping else None)
        return {"shape": "list", "columns": columns, "rows": rows, "column_source": "contract", "_treg": meta}
    columns = list(output_fields) + ["served_by"]
    rows = [] if miss else [[cell(output.get(f)) for f in output_fields] + [served_by]]
    return {"shape": "flat", "columns": columns, "rows": rows, "column_source": "contract", "_treg": meta}


def _mapped(item: dict[str, Any], mapping) -> dict[str, Any]:
    """One provider row: the mapped columns first (first path that holds a value wins), then every
    other field under the provider's own name, minus the fields a mapped column already used."""
    rec: dict[str, Any] = {}
    used: set[str] = set()
    for column, paths in mapping or ():
        rec[column] = None
        for path in paths:
            found, value = _get_path(item, path)
            # text only: a provider whose `location` or `company` is an object is read at its inner
            # path (`company.name`, `location.city`), never shown as a JSON cell under a fixed name
            if found and value not in (None, "") and not isinstance(value, dict) and not (
                    isinstance(value, list) and any(isinstance(v, (dict, list)) for v in value)):
                rec[column] = cell(value)
                used.add(path)
                break
    for key, value in flatten(item).items():
        if key not in used and key not in rec:
            rec[key] = value
    return rec


def from_hub(body: Any, output_fields: list[str]) -> dict[str, Any]:
    """A hub tool's answer, `{run_id, output, usage, trace}`: its manifest's fields, one row."""
    body = body if isinstance(body, dict) else {}
    output = body.get("output") if isinstance(body.get("output"), dict) else {}
    fields = list(output_fields) or list(output)
    meta = {"run_id": body.get("run_id"), "recipe": body.get("recipe"), "usage": body.get("usage")}
    return {"shape": "flat", "columns": fields, "rows": [[cell(output.get(f)) for f in fields]],
            "column_source": "hub", "_treg": meta}


def generated(body: Any) -> dict[str, Any]:
    """Any other answer: the lists of objects in it decide the shape."""
    tables = _find_tables(body, "", 0)
    if not tables:
        base = body
        if isinstance(body, list):
            # a bare list of scalars, or of mixed values: one column
            return {"shape": "list", "columns": ["value"], "rows": [[cell(v)] for v in body],
                    "column_source": "generated"}
        columns, rows = _grid([flatten(base)])
        return {"shape": "flat", "columns": columns, "rows": rows, "column_source": "generated"}
    if len(tables) == 1:
        _, path, items = tables[0]
        columns, rows = _grid([flatten(i) for i in items])
        return {"shape": "list", "columns": columns, "rows": rows, "column_source": "generated", "path": path}
    views = []
    for name, path, items in tables:
        columns, rows = _grid([flatten(i) for i in items])
        views.append({"name": name, "path": path, "row_count": len(rows), "columns": columns, "rows": rows})
    summary = _summary(_common_parent(body, [p for _, p, _ in tables]))
    return {"shape": "nested", "columns": summary["columns"], "rows": summary["rows"],
            "tables": views, "summary": summary, "column_source": "generated"}


def _find_tables(obj: Any, path: str, depth: int) -> list[tuple[str, str, list[dict]]]:
    """Every list of objects in the answer, as (name, path, items). A one-object list under a
    wrapper name (`ENVELOPE_KEYS`) is searched inside instead of counted, and a table's own items
    are never searched: their inner lists are cells."""
    found: list[tuple[str, str, list[dict]]] = []
    if depth > MAX_WALK:
        return found
    if isinstance(obj, list) and _is_record_list(obj) and not path:
        return [("rows", "", _items(obj))]
    if not isinstance(obj, dict):
        return found
    for key, value in obj.items():
        if _hidden(key):
            continue
        here = f"{path}.{key}" if path else str(key)
        if _is_record_list(value):
            items = _items(value)
            # a one-item wrapper is opened only when it holds tables; otherwise its one item is a
            # one-row table (a search that found one person), not a JSON cell
            inner = _find_tables(items[0], f"{here}[0]", depth + 1) if len(items) == 1 and key in ENVELOPE_KEYS else []
            if inner:
                found += inner
            else:
                found.append((str(key), here, items))
        elif isinstance(value, dict):
            found += _find_tables(value, here, depth + 1)
    return found


def _common_parent(body: Any, paths: list[str]) -> Any:
    """The object that holds the tables, when they share one parent (seranking `summary[0]`), so the
    summary lists that object's own fields; else the whole answer."""
    parents = {p.rsplit(".", 1)[0] if "." in p else "" for p in paths}
    if len(parents) != 1:
        return body
    parent = parents.pop()
    cur = body
    for part in [p for p in parent.split(".") if p]:
        key, _, index = part.partition("[")
        cur = cur.get(key) if isinstance(cur, dict) else None
        if index:
            i = int(index.rstrip("]"))
            cur = cur[i] if isinstance(cur, list) and len(cur) > i else None
    return cur if isinstance(cur, dict) else body


def _summary(obj: Any) -> dict[str, Any]:
    """An object as two columns, field and value. A list of objects becomes one cell: the first
    value of each item, joined with ", " (`top_tlds` -> "com, info, org")."""
    rows: list[list[Any]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if _hidden(key):
                continue
            if _is_record_list(value):
                firsts = [next(iter(v.values()), None) for v in _items(value)]
                rows.append([str(key), cell([f if not isinstance(f, (dict, list)) else cell(f) for f in firsts])])
            elif isinstance(value, dict):
                rows += [[k, v] for k, v in flatten(value, str(key)).items()]
            else:
                rows.append([str(key), cell(value)])
    return {"columns": ["field", "value"], "rows": rows}


def sample_items(body: Any) -> list[dict]:
    """The items of the first list of objects in an answer ([] when none): what a routed list job's
    provider returns, read from its saved example to preview the job's columns."""
    tables = _find_tables(body, "", 0)
    return list(tables[0][2]) if tables else []


def columns_only(table: dict[str, Any]) -> dict[str, Any]:
    """A table with its rows removed: the free preview `GET /table-columns/<tool id>` answers, so a
    client can show the columns before anyone pays."""
    out = {"shape": table.get("shape"), "columns": list(table.get("columns") or []),
           "column_source": table.get("column_source")}
    if table.get("tables"):
        out["tables"] = [{"name": tb["name"], "path": tb["path"], "row_count": tb["row_count"],
                          "columns": tb["columns"]} for tb in table["tables"]]
    return out


def raw(text: str, *, truncated: bool = False) -> dict[str, Any]:
    """An answer that is not JSON, or too big to read: its text, cut to RAW_TEXT_BYTES."""
    data = text.encode("utf-8", "replace")
    cut = len(data) > RAW_TEXT_BYTES
    body = data[:RAW_TEXT_BYTES].decode("utf-8", "ignore") if cut else text
    return {"shape": "raw", "columns": ["text"], "rows": [[body]], "column_source": "generated",
            "truncated": truncated or cut}


def to_table(body: Any, *, contract_output: list[str] | None = None, list_field: str | None = None,
             hub_fields: list[str] | None = None) -> dict[str, Any]:
    """The table for one parsed answer. Pass `contract_output` for a routed job (with `list_field`
    when its contract has a required list), `hub_fields` for a hub tool, neither for anything else.
    Never raises on a strange body: anything it cannot shape becomes a `raw` table."""
    try:
        if contract_output is not None:
            return from_contract(body, contract_output, list_field)
        if hub_fields is not None:
            return from_hub(body, hub_fields)
        return generated(body)
    except Exception:  # noqa: BLE001 - a table is a view; the answer itself was already delivered
        return raw(json.dumps(body, ensure_ascii=False, default=str))
