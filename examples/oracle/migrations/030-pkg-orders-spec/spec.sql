CREATE OR REPLACE PACKAGE pkg_orders AS
  -- The default region assigned by the backfill.
  FUNCTION default_region RETURN VARCHAR2;
END pkg_orders;
/
