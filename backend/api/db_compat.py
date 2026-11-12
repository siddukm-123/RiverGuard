"""
Database compatibility helpers.

RiverGuard's tables grew over time (migrations 0001-0003). A database created from an older migration may lack an OPTIONAL
column. Instead of failing with a bare "Internal server error", inserts that use optional columns go through
insert_dropping_missing_columns(): if the database rejects an optional column, that column is dropped and the insert retried.
"""
import logging

logger = logging.getLogger(__name__)


def insert_dropping_missing_columns(insert, table, row, optional):
    """
    insert(table, row) -> saved row. Returns (saved_row, dropped_columns).
    Only columns listed in `optional` are ever dropped; any other error is raised unchanged. The dropped values are put back
    on the returned dict so the current request/session still sees them (they just are not stored by the database).
    """
    original = dict(row)
    work = dict(row)
    dropped = []
    for _ in range(len(optional) + 1):
        try:
            saved = insert(table, work)
            break
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            missing = [c for c in optional if c in work and c.lower() in msg]
            if not missing:
                raise
            for c in missing:
                work.pop(c)
                dropped.append(c)
            logger.warning(f"{table}: column(s) {missing} are missing in the database - saving without them (run the latest migration SQL).")
    else:
        raise RuntimeError(f"could not save to {table}")
    for c in dropped:
        saved[c] = original[c]
    return saved, dropped
