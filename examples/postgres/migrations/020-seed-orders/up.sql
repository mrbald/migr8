-- The literal contains a semicolon and a slash; neither terminates a statement.
INSERT INTO orders (id, region, note)
SELECT g, NULL, 'seeded; see docs/SPEC.md'
  FROM generate_series(1, 2500) AS g;
