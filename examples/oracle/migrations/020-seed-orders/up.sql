-- Atomic: this DML and the SUCCESS history row commit in one transaction.
-- The literal below deliberately contains a semicolon and a slash; neither is
-- a statement terminator inside a literal.
INSERT INTO orders (id, region, note)
SELECT LEVEL, NULL, 'seeded; see docs/SPEC.md for details'
  FROM dual
CONNECT BY LEVEL <= 2500;
