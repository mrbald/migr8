"""Create an index concurrently.

This cannot run inside a transaction block, so the adapter executes it outside
one. That is why the migration is restartable: a failed concurrent build leaves
an invalid index behind, and this code checks for that state before retrying.
"""


def migrate(ctx):
    rows = ctx.query(
        """SELECT i.indisvalid
             FROM pg_class c
             JOIN pg_index i ON i.indexrelid = c.oid
            WHERE c.relname = 'orders_region_idx'"""
    )
    if rows and not rows[0][0]:
        # A previous attempt left an invalid index; drop it before retrying.
        ctx.ddl("DROP INDEX CONCURRENTLY orders_region_idx")
        rows = []
    if not rows:
        ctx.ddl("CREATE INDEX CONCURRENTLY orders_region_idx ON orders (region)")
