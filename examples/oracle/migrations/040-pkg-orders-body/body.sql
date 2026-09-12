CREATE OR REPLACE PACKAGE BODY pkg_orders AS
  FUNCTION default_region RETURN VARCHAR2 IS
  BEGIN
    RETURN 'EU';
  END default_region;
END pkg_orders;
/
