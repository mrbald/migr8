"""Bounded, checkpointed Oracle backfill.

Stated assumptions, which an author must re-examine for real traffic:

* ids are positive and stable for the rows this migration selects;
* nothing else changes the selected rows while a batch is open;
* assigning the package's default region is the intended transformation.

The checkpoint is derived from the exact batch that was selected and updated,
and it commits inside that batch's transaction. This does not claim that a row
inserted below the checkpoint afterwards will be covered: that would need an
idempotent predicate or an explicit strategy for application writes.
"""

from .assumptions import BATCH_SIZE


def migrate(ctx):
    # The region value comes from the package this sequence already created,
    # which is why that package is declared in require_valid.
    region = ctx.query("SELECT pkg_orders.default_region FROM dual")[0][0]
    last = int(ctx.progress.get("last_id", "0"))

    while True:
        with ctx.transaction() as tx:
            rows = tx.query(
                """SELECT id FROM orders
                    WHERE id > :after AND region IS NULL
                    ORDER BY id
                    FETCH FIRST :batch_size ROWS ONLY""",
                {"after": last, "batch_size": BATCH_SIZE},
            )
            if not rows:
                break
            ids = [row[0] for row in rows]
            changed = tx.executemany(
                """UPDATE orders SET region = :region
                    WHERE id = :id AND region IS NULL""",
                [{"id": key, "region": region} for key in ids],
            )
            if changed != len(ids):
                raise RuntimeError(
                    f"batch membership changed: updated {changed} of {len(ids)} rows"
                )
            last = max(ids)
            ctx.progress.set("last_id", str(last))
        ctx.log("batch committed", last_id=last, rows=len(ids))
