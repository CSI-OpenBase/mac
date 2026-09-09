from __future__ import annotations

import pytest

from admin_app.schema_contract import (
    EXPECTED_COLUMNS,
    EXPECTED_UNIQUE_KEYS,
    STARTUP_EXPECTED_COLUMNS,
    STARTUP_EXPECTED_UNIQUE_KEYS,
    STARTUP_REQUIRED_TABLES,
    SchemaContractError,
    validate_schema_contract,
    validate_startup_schema_contract,
)


def _valid_contract() -> tuple[
    dict[str, set[str]], dict[str, set[tuple[str, ...]]]
]:
    return (
        {table: set(columns) for table, columns in EXPECTED_COLUMNS.items()},
        {table: set(keys) for table, keys in EXPECTED_UNIQUE_KEYS.items()},
    )


def test_schema_contract_accepts_required_shape_with_additional_metadata() -> None:
    columns, unique_keys = _valid_contract()
    columns["comments"].add("future_optional_column")
    unique_keys["comments"].add(("platform", "comment_id", "created_at"))

    validate_schema_contract(columns, unique_keys)


def test_schema_contract_reports_missing_table_before_shape_checks() -> None:
    columns, unique_keys = _valid_contract()
    columns.pop("workspace_identity")

    with pytest.raises(
        SchemaContractError,
        match=r"missing required MySQL tables: workspace_identity",
    ):
        validate_schema_contract(columns, unique_keys)


def test_schema_contract_reports_missing_columns_and_unique_keys() -> None:
    columns, unique_keys = _valid_contract()
    columns["comment_snapshots"].remove("record_json")
    unique_keys["audience_snapshots"].remove(
        ("platform", "observed_at", "dimension_name", "segment_name")
    )

    with pytest.raises(SchemaContractError) as exc_info:
        validate_schema_contract(columns, unique_keys)

    message = str(exc_info.value)
    assert "comment_snapshots missing columns: record_json" in message
    assert (
        "audience_snapshots missing unique keys: "
        "(platform, observed_at, dimension_name, segment_name)"
    ) in message


def test_startup_schema_contract_includes_admin_tables() -> None:
    columns = {
        table: set(expected) for table, expected in STARTUP_EXPECTED_COLUMNS.items()
    }
    unique_keys = {
        table: set(expected)
        for table, expected in STARTUP_EXPECTED_UNIQUE_KEYS.items()
    }

    validate_startup_schema_contract(columns, unique_keys)

    assert {"collection_jobs", "collection_schedules"} <= set(
        STARTUP_REQUIRED_TABLES
    )


@pytest.mark.parametrize("missing_table", ["collection_jobs", "collection_schedules"])
def test_startup_schema_contract_rejects_missing_admin_table(
    missing_table: str,
) -> None:
    columns = {
        table: set(expected) for table, expected in STARTUP_EXPECTED_COLUMNS.items()
    }
    unique_keys = {
        table: set(expected)
        for table, expected in STARTUP_EXPECTED_UNIQUE_KEYS.items()
    }
    columns.pop(missing_table)
    unique_keys.pop(missing_table)

    with pytest.raises(
        SchemaContractError,
        match=rf"missing required MySQL tables: {missing_table}",
    ):
        validate_startup_schema_contract(columns, unique_keys)
