-- Restartable DDL. The mode label does not make a plain CREATE convergent:
-- the author is responsible for a statement whose repetition satisfies the
-- migration contract, which is why IF NOT EXISTS is used here.
CREATE TABLE IF NOT EXISTS orders (
    id     INTEGER NOT NULL PRIMARY KEY,
    region TEXT
);
