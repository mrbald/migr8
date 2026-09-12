-- Oracle DDL commits independently, so it belongs to a restartable migration.
-- The mode label alone does not make a plain CREATE convergent: this block is
-- written so that repeating it satisfies the migration contract.
DECLARE
  already_exists EXCEPTION;
  PRAGMA EXCEPTION_INIT(already_exists, -955);
BEGIN
  EXECUTE IMMEDIATE 'CREATE TABLE orders (
      id       NUMBER(10)       NOT NULL,
      region   VARCHAR2(2 CHAR),
      note     VARCHAR2(200 CHAR),
      CONSTRAINT orders_pk PRIMARY KEY (id)
  )';
EXCEPTION
  WHEN already_exists THEN NULL;
END;
/
