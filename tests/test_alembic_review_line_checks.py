import importlib.util
from pathlib import Path
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from app.evaluation.models import ReviewLineCheckRow

_MIGRATION = (
    Path(__file__).parent.parent / "alembic" / "versions" / "0002_create_review_line_checks.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0002", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_is_chained_after_the_evaluation_tables() -> None:
    migration = _load_migration()

    assert migration.revision == "0002"
    assert migration.down_revision == "0001"


def test_migration_creates_the_columns_the_model_expects_and_downgrade_drops_it() -> None:
    migration = _load_migration()
    engine = create_engine("sqlite:///:memory:")

    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()

        columns = {c["name"]: c for c in inspect(connection).get_columns("review_line_checks")}
        model_columns = {c.name: c for c in ReviewLineCheckRow.__table__.columns}
        assert set(columns) == set(model_columns)
        for name, column in model_columns.items():
            assert columns[name]["nullable"] == column.nullable, name
        assert inspect(connection).get_pk_constraint("review_line_checks")[
            "constrained_columns"
        ] == ["review_job_id"]

        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert "review_line_checks" not in inspect(connection).get_table_names()
