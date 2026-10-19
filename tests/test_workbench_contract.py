import copy
import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError


CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "workbench"
FIXTURES = sorted((CONTRACT / "fixtures").glob("*.json"))


@pytest.fixture(scope="module")
def contract():
    return yaml.safe_load((CONTRACT / "openapi.yaml").read_text())


def _resolve(contract, value):
    while "$ref" in value:
        reference = value["$ref"]
        assert reference.startswith("#/"), reference
        value = contract
        for part in reference[2:].split("/"):
            value = value[part.replace("~1", "/").replace("~0", "~")]
    return value


def _operation(contract, operation_id):
    matches = [
        operation
        for path in contract["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and operation.get("operationId") == operation_id
    ]
    assert len(matches) == 1, operation_id
    return matches[0]


def _validator(contract, schema):
    schema = {**schema, "components": contract["components"]}
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _request_validator(contract, operation_id):
    body = _resolve(contract, _operation(contract, operation_id)["requestBody"])
    return _validator(contract, body["content"]["application/json"]["schema"])


@pytest.mark.parametrize("fixture_path", FIXTURES, ids=lambda path: path.name)
def test_fixture_matches_declared_response(contract, fixture_path):
    operation_id = fixture_path.name.split(".", 1)[0]
    if operation_id == "error":
        schemas = [{"$ref": "#/components/schemas/Error"}]
    else:
        schemas = []
        for status, response in _operation(contract, operation_id)["responses"].items():
            response = _resolve(contract, response)
            if str(status).startswith("2") and "application/json" in response.get("content", {}):
                schemas.append(response["content"]["application/json"]["schema"])
    assert schemas, fixture_path.name
    _validator(contract, {"anyOf": schemas}).validate(json.loads(fixture_path.read_text()))


@pytest.mark.parametrize("missing", [
    ("database_generation",),
    ("expected_schema_version",),
    ("rows", 0, "id"),
    ("rows", 0, "expected_revision"),
])
def test_patch_rejects_missing_write_guards(contract, missing):
    payload = {
        "database_generation": 2,
        "expected_schema_version": 3,
        "mode": "patch",
        "rows": [{"id": "row_1", "expected_revision": 4, "values": {"email": "a@example.test"}}],
    }
    validator = _request_validator(contract, "writeRows")
    validator.validate(payload)
    target = payload
    for part in missing[:-1]:
        target = target[part]
    del target[missing[-1]]
    with pytest.raises(ValidationError):
        validator.validate(payload)


def test_create_table_does_not_accept_imported_rows(contract):
    payload = {
        "name": "leads",
        "columns": [{"sql_name": "email", "storage_type": "TEXT", "nullable": True}],
    }
    validator = _request_validator(contract, "createTable")
    validator.validate(payload)
    with pytest.raises(ValidationError):
        validator.validate({**payload, "rows": [{"email": "a@example.test"}]})


@pytest.mark.parametrize("sql_name", ["email\n", "LEADS", "sqlite_master", "_treg_receipts"])
def test_table_creation_rejects_unsafe_or_reserved_column_names(contract, sql_name):
    payload = {
        "name": "leads",
        "columns": [{"sql_name": "email", "storage_type": "TEXT", "nullable": True}],
    }
    validator = _request_validator(contract, "createTable")
    validator.validate(payload)
    payload["columns"][0]["sql_name"] = sql_name
    with pytest.raises(ValidationError):
        validator.validate(payload)


def test_table_metadata_update_requires_compare_and_set_revision(contract):
    payload = {"name": "renamed-leads", "expected_metadata_revision": 2}
    validator = _request_validator(contract, "updateTable")
    validator.validate(payload)
    for revision in (None, 0):
        invalid = {"name": payload["name"]}
        if revision is not None:
            invalid["expected_metadata_revision"] = revision
        with pytest.raises(ValidationError):
            validator.validate(invalid)


@pytest.mark.parametrize("field", ["database_generation", "schema_version"])
def test_ready_table_requires_verified_versions(contract, field):
    table = json.loads((CONTRACT / "fixtures" / "getTable.leads.json").read_text())
    validator = _validator(contract, {"$ref": "#/components/schemas/Table"})
    validator.validate(table)
    for version in (None, 0):
        invalid = copy.deepcopy(table)
        invalid[field] = version
        with pytest.raises(ValidationError):
            validator.validate(invalid)


def test_creating_table_does_not_claim_verified_versions(contract):
    table = json.loads((CONTRACT / "fixtures" / "getTable.creating.json").read_text())
    _validator(contract, {"$ref": "#/components/schemas/Table"}).validate(table)
    assert table["status"] == "creating"
    assert table["database_generation"] is None
    assert table["schema_version"] is None


@pytest.mark.parametrize("missing", ["install_id", "expected_install_revision"])
def test_run_creation_requires_install_identity_and_revision(contract, missing):
    payload = {"install_id": "ins_lead-emails", "expected_install_revision": 2, "wait_s": 0}
    validator = _request_validator(contract, "runInstall")
    validator.validate(payload)
    del payload[missing]
    with pytest.raises(ValidationError):
        validator.validate(payload)


@pytest.mark.parametrize("operation_id", ["runInstall", "retryFailedRows"])
def test_paid_run_requests_require_a_body_and_idempotency_header(contract, operation_id):
    operation = _operation(contract, operation_id)
    assert _resolve(contract, operation["requestBody"])["required"] is True
    headers = [
        parameter for reference in operation.get("parameters", [])
        if (parameter := _resolve(contract, reference))["in"] == "header"
        and parameter["name"].lower() == "idempotency-key"
    ]
    assert len(headers) == 1
    assert headers[0]["required"] is True
    validator = _validator(contract, headers[0]["schema"])
    validator.validate("retry-lead-emails-1")
    with pytest.raises(ValidationError):
        validator.validate("")


@pytest.mark.parametrize("items", [[], ["item_1", "item_1"], [""]])
def test_retry_items_must_be_nonempty_unique_identities(contract, items):
    payload = {
        "selection": {"type": "items", "selection_id": "failset_1", "item_ids": ["item_1", "item_2"]},
        "input_mode": "current_rows",
    }
    validator = _request_validator(contract, "retryFailedRows")
    validator.validate(payload)
    payload["selection"]["item_ids"] = items
    with pytest.raises(ValidationError):
        validator.validate(payload)


@pytest.mark.parametrize("invalid", [
    {},
    {"input_mode": "current_rows"},
    {"selection": {"type": "failed", "selection_id": "failset_1"}},
    {"selection": {"type": "failed"}, "input_mode": "current_rows"},
    {"selection": {"type": "failed", "selection_id": ""}, "input_mode": "current_rows"},
    {"selection": {"type": "items", "selection_id": "failset_1"}, "input_mode": "current_rows"},
    {"selection": {"type": "all", "selection_id": "failset_1"}, "input_mode": "current_rows"},
    {"selection": {"type": "failed", "selection_id": "failset_1"}, "input_mode": "original_rows"},
])
def test_retry_rejects_missing_or_ambiguous_selection(contract, invalid):
    validator = _request_validator(contract, "retryFailedRows")
    validator.validate({
        "selection": {"type": "failed", "selection_id": "failset_1"}, "input_mode": "current_rows",
    })
    with pytest.raises(ValidationError):
        validator.validate(invalid)


def _run_objects(value):
    if isinstance(value, dict):
        if "progress" in value and "spent_micro" in value:
            yield value
        for child in value.values():
            yield from _run_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _run_objects(child)


def _run_examples():
    for path in FIXTURES:
        payload = json.loads(path.read_text())
        for run in _run_objects(payload):
            yield pytest.param(run, id=f"{path.name}:{run.get('id', run.get('run_id'))}")


@pytest.mark.parametrize("run", list(_run_examples()))
def test_run_fixture_preserves_the_shared_handle_identity(contract, run):
    _validator(contract, {"$ref": "#/components/schemas/RunHandle"}).validate(run)
    if "id" in run:
        assert run["run_id"] == run["id"]


@pytest.mark.parametrize("run", list(_run_examples()))
def test_run_fixture_progress_preserves_all_selected_work(run):
    progress = run["progress"]
    assert progress["selected"] == sum(
        progress[key] for key in ("not_started", "in_progress", "completed", "interrupted")
    )
    assert progress["completed"] == sum(progress[key] for key in ("succeeded", "failed", "skipped"))
    if progress["total"] is not None:
        assert progress["selected"] <= progress["total"]
    terminal = run["status"] in {"succeeded", "partial", "failed", "stopped"}
    assert run["terminal"] is terminal
    if terminal:
        assert progress["in_progress"] == 0
    if run["status"] == "queued" and "id" in run:
        assert run["started_at"] is None


def test_stopping_does_not_reclassify_interrupted_work_as_failed():
    pending = json.loads((CONTRACT / "fixtures" / "stopRun.default.json").read_text())
    stopped = json.loads((CONTRACT / "fixtures" / "getRun.stopped.json").read_text())
    assert pending["id"] == stopped["id"]
    assert pending["status"] == "stopping" and stopped["status"] == "stopped"
    assert stopped["progress"]["interrupted"] > 0
    assert stopped["progress"]["failed"] == pending["progress"]["failed"]
    assert stopped["progress"]["completed"] == pending["progress"]["completed"]
    assert stopped["progress"]["not_started"] == pending["progress"]["not_started"]
    assert stopped["progress"]["interrupted"] == (
        pending["progress"]["interrupted"] + pending["progress"]["in_progress"]
    )


def test_queued_run_can_exist_before_it_has_started(contract):
    run = json.loads((CONTRACT / "fixtures" / "getRun.r_8f21.json").read_text())
    run.update(status="queued", terminal=False, started_at=None, finished_at=None)
    run["progress"].update(
        not_started=run["progress"]["selected"], in_progress=0, completed=0,
        succeeded=0, failed=0, skipped=0, interrupted=0,
    )
    _validator(contract, {"$ref": "#/components/schemas/Run"}).validate(run)


@pytest.mark.parametrize("payload,missing", [
    ({"config_revision": 0, "tool": "tim.lead-emails@3"}, "config_revision"),
    ({"config_revision": 0, "tool": "tim.lead-emails@3"}, "tool"),
    ({"config_revision": 2, "install_id": "ins_lead-emails", "paused": True}, "config_revision"),
    ({"config_revision": 2, "install_id": "ins_lead-emails", "paused": True}, "install_id"),
])
def test_install_save_requires_create_or_update_identity(contract, payload, missing):
    validator = _request_validator(contract, "saveInstall")
    validator.validate(payload)
    invalid = {key: value for key, value in payload.items() if key != missing}
    with pytest.raises(ValidationError):
        validator.validate(invalid)


@pytest.mark.parametrize("revision", [None, -1, 1.5])
def test_install_save_requires_an_integer_compare_and_set_revision(contract, revision):
    payload = {"config_revision": revision, "install_id": "ins_lead-emails", "paused": True}
    with pytest.raises(ValidationError):
        _request_validator(contract, "saveInstall").validate(payload)


@pytest.mark.parametrize("revision,missing", [
    (0, "config_revision"), (0, "run_as_membership_id"), (2, "config_revision"), (2, "id"),
])
def test_trigger_save_requires_create_or_update_identity(contract, revision, missing):
    trigger = {"key": "weekly", "kind": "schedule", "config_revision": revision}
    if revision:
        trigger["id"] = "trg_weekly"
    else:
        trigger["run_as_membership_id"] = 87
    payload = {"install_id": "ins_lead-emails", "config_revision": 2, "triggers": [trigger]}
    validator = _request_validator(contract, "saveInstall")
    validator.validate(payload)
    del trigger[missing]
    with pytest.raises(ValidationError):
        validator.validate(payload)


@pytest.mark.parametrize("membership_id", [None, 0, -1, 1.5])
def test_trigger_creation_requires_a_positive_integer_execution_identity(contract, membership_id):
    trigger = {
        "key": "weekly", "kind": "schedule", "config_revision": 0,
        "run_as_membership_id": membership_id,
    }
    payload = {"install_id": "ins_lead-emails", "config_revision": 2, "triggers": [trigger]}
    with pytest.raises(ValidationError):
        _request_validator(contract, "saveInstall").validate(payload)


@pytest.mark.parametrize("fixture_name,status", [
    ("getTool.pendingBuild.json", "saved"),
    ("getTool.pendingReview.json", "review"),
])
def test_pending_tool_version_keeps_the_previous_serving_version(fixture_name, status):
    tool = json.loads((CONTRACT / "fixtures" / fixture_name).read_text())
    versions = {version["version"]: version for version in tool["versions"]}
    latest = versions[tool["latest_version"]]
    serving = versions[tool["serving_version"]]
    assert latest["version"] > serving["version"]
    assert latest["status"] == status
    assert latest["published_at"] is None
    assert serving["status"] == "live"
    assert serving["build_status"] == "ready"
    assert serving["check_status"] == "passed"
    assert serving["published_at"] is not None
