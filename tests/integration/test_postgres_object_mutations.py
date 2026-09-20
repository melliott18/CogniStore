"""Cross-process API object mutation ordering against PostgreSQL."""

from pathlib import Path

import pytest

from tests.object_mutation_helpers import assert_process_mutation_serialization

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("first_method", ["DELETE", "PUT"])
def test_postgres_gateway_mutation_ordering_across_processes(
    postgres_dsn: str, tmp_path: Path, first_method: str,
):
    assert_process_mutation_serialization(postgres_dsn, tmp_path, first_method)
