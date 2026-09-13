"""Bounded, checkpointed backfill.

The checkpoint is derived from the exact batch that was selected and updated,
and it commits inside the same transaction as that batch. This migration does
not claim that a row inserted below the checkpoint after the fact will be
covered; that would need an idempotent predicate or an explicit strategy for
application writes.
"""

# Sibling modules are imported relatively; the unit is its own private package
# and is never added to sys.path.
from .assumptions import BATCH_SIZE, TARGET_REGION


def migrate(ctx):
    last = int(ctx.progress.get("last_id", "0"))
    while True:
        with ctx.transaction() as tx:
            rows = tx.query(
                "SELECT id FROM orders WHERE id > ? AND region IS NULL "
                "ORDER BY id LIMIT ?",
                (last, BATCH_SIZE),
            )
            if not rows:
                break
            ids = [row[0] for row in rows]
            changed = tx.executemany(
                "UPDATE orders SET region = ? WHERE id = ? AND region IS NULL",
                [(TARGET_REGION, key) for key in ids],
            )
            if changed != len(ids):
                raise RuntimeError("batch membership changed under the migration")
            last = max(ids)
            ctx.progress.set("last_id", str(last))
        ctx.log("batch committed", last_id=last, rows=len(ids))
