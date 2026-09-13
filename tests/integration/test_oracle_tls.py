"""Certificate authentication over TLS, proxying into the schema (spec Section 12).

This is the shape a deployment uses when no password exists anywhere: the client
proves who it is with a certificate in a wallet, the database maps that
certificate to a user, and a proxy grant says which schema that user may become.
The runner holds no secret, and there is nothing to rotate in its configuration.

`testenv/provision_tls.sh` builds the fixture these need; without it they skip
and say so. A wallet is read by the Oracle Client libraries, so they need Thick
mode too, which means running where those libraries are.
"""

from __future__ import annotations

import os

import pytest
import support

from migr8.errors import Exit
from migr8.model import HISTORY_TABLE, META_TABLE

pytestmark = pytest.mark.oracle


def _one_migration(root):
    support.unit(root, "m1", {"up.sql": "CREATE TABLE tls_orders (id NUMBER(10) PRIMARY KEY)"})
    return support.manifest(
        root,
        [
            {
                "id": "tls-orders",
                "path": "m1",
                "language": "sql",
                "mode": "restartable",
                "entry": "up.sql",
            }
        ],
    )


def test_a_certificate_identity_migrates_with_no_password_anywhere(oracle_tls_project):
    """Initial install over TLS, authenticated by certificate, running as the schema."""
    import oracledb

    root, config, schema, connect = oracle_tls_project
    manifest = _one_migration(root)
    assert "MIGR8_PASSWORD" not in os.environ

    assert support.report_for("status", config, manifest).exit_code == Exit.NOT_INITIALIZED
    assert support.migrate(config, manifest) == Exit.OK
    assert support.report_for("validate", config, manifest).exit_code == Exit.OK

    with oracledb.connect(**connect) as observer:
        cursor = observer.cursor()
        context = {
            key: cursor.execute(
                "SELECT sys_context('USERENV', :key) FROM dual", key=key
            ).fetchone()[0]
            for key in ("NETWORK_PROTOCOL", "AUTHENTICATION_METHOD", "PROXY_USER", "SESSION_USER")
        }
        owned = {
            row[0]
            for row in cursor.execute(
                "SELECT object_name FROM all_objects WHERE owner = :owner "
                "AND object_name IN ('TLS_ORDERS', :history, :meta)",
                owner=schema,
                history=HISTORY_TABLE.upper(),
                meta=META_TABLE.upper(),
            ).fetchall()
        }

    assert context["NETWORK_PROTOCOL"] == "tcps"
    assert context["AUTHENTICATION_METHOD"] == "SSL_PROXY"
    assert context["SESSION_USER"] == schema
    assert context["PROXY_USER"].startswith("CN=")
    assert owned == {"TLS_ORDERS", HISTORY_TABLE.upper(), META_TABLE.upper()}


def test_a_password_with_a_certificate_connect_string_is_refused(oracle_tls_project, monkeypatch):
    """The two say different things about who is connecting, so the run stops."""
    root, config, _schema, _connect = oracle_tls_project
    manifest = _one_migration(root)
    monkeypatch.setenv("MIGR8_PASSWORD", "not-used-here")
    report = support.migrate_report(config, manifest)
    assert report.exit_code == Exit.USAGE
    assert "no connecting user" in (report.message or "")
