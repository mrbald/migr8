-- Atomic: this INSERT and the SUCCESS history row commit in one transaction.
INSERT INTO orders (id, region)
SELECT value, NULL FROM (
    WITH RECURSIVE counter(value) AS (
        SELECT 1 UNION ALL SELECT value + 1 FROM counter WHERE value < 2500
    )
    SELECT value FROM counter
);
