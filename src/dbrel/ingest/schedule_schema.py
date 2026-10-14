"""Add dated-plan evidence to an existing collection without rewriting facts."""

from sqlalchemy import Engine, inspect
from sqlalchemy.orm import Session

from dbrel.models import TripScheduleSnapshot


def ensure_dated_schedule_schema(engine: Engine) -> bool:
    """Return whether the new table was created; never create parent tables."""
    with engine.begin() as connection:
        present = set(inspect(connection).get_table_names())
        missing = {"trip", "station", "station_stop"} - present
        if missing:
            raise ValueError(f"collection parent tables missing: {', '.join(sorted(missing))}")
        table = TripScheduleSnapshot.__table__
        if table.name in present:
            _validate_schema(inspect(connection))
            return False
        table.create(connection, checkfirst=True)
        return True


def require_dated_schedule_schema(session: Session) -> None:
    """Fail before collection when an existing database lacks dated evidence."""
    inspector = inspect(session.connection())
    if not inspector.has_table(TripScheduleSnapshot.__tablename__):
        raise ValueError("dated schedule schema missing; run 'dbrel migrate-trip-schedule-schema' first")
    _validate_schema(inspector)


def _validate_schema(inspector) -> None:
    table = TripScheduleSnapshot.__table__
    columns = {column['name']: column for column in inspector.get_columns(table.name)}
    wrong = [column.name for column in table.columns
             if column.name not in columns or
             columns[column.name]['type']._type_affinity != column.type._type_affinity]
    primary = inspector.get_pk_constraint(table.name).get('constrained_columns')
    identities = {tuple(constraint['column_names'])
                  for constraint in inspector.get_unique_constraints(table.name)}
    identities |= {tuple(index['column_names']) for index in inspector.get_indexes(table.name)
                   if index.get('unique')}
    expected_identity = ('trip_id', 'stop_sequence', 'observed_at',
                         'source_content_sha256', 'value_fingerprint')
    if wrong or primary != ['id'] or expected_identity not in identities:
        detail = ', '.join(wrong) if wrong else 'primary or receipt identity constraint'
        raise ValueError(f"incompatible dated schedule schema: {detail}; repair migration before ingestion")
