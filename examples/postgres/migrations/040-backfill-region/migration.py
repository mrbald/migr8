"""Bounded, checkpointed backfill using PostgreSQL's native parameter style.

The same assumptions as the Oracle example apply and must be re-examined for
real traffic: stable positive ids, no competing changes to the selected rows,
and that assigning the target region is the intended transformation.
"""

from .assumptions import BATCH_SIZE, TARGET_REGION


def migrate(ctx):
    last = int(ctx.progress.get("last_id", "0"))
    while True:
        with ctx.transaction() as tx:
            rows = tx.query(
                """SELECT id FROM orders
                    WHERE id > %s AND region IS NULL
                    ORDER BY id
                    LIMIT %s""",
                (last, BATCH_SIZE),
            )
            if not rows:
                break
            ids = [row[0] for row in rows]
            changed = tx.executemany(
                "UPDATE orders SET region = %s WHERE id = %s AND region IS NULL",
                [(TARGET_REGION, key) for key in ids],
            )
            if changed != len(ids):
                raise RuntimeError(
                    f"batch membership changed: updated {changed} of {len(ids)} rows"
                )
            last = max(ids)
            ctx.progress.set("last_id", str(last))
        ctx.log("batch committed", last_id=last, rows=len(ids))
