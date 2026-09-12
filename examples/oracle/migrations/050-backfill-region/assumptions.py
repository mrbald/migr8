"""Unit-local helper, imported relatively from inside the staged unit."""

#: Bounded so one transaction stays small and the migration stays restartable.
BATCH_SIZE = 500
