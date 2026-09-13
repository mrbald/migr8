# Licensing

Model: AGPL-3.0-only for the public repository + commercial licenses sold separately
(dual licensing). Rationale: AGPL is maximally unattractive for enterprise internal
forks, which channels enterprise users to the commercial license.

Operational requirements:

1. Copyright unity. All contributors sign a CLA assigning (or broadly licensing)
   copyright to the founder's entity, preserving the right to dual-license.
   No external PR is merged without a signed CLA. (Add CLA text before first
   external contribution; not needed while the founder is the sole contributor.)
2. Clean-room discipline. No proprietary schema, DDL, migration source, table or
   column names, or derived artifacts from any production estate may enter this
   repository, its tests, docs, examples or issue tracker. `examples/` and
   `tests/` are synthetic only.
3. LICENSE file: verbatim AGPL-3.0 text from gnu.org (sha256
   `0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0`, the
   canonical `agpl-3.0.txt`). COMMERCIAL.md holds the commercial-availability
   notice. Per-file SPDX headers are deliberately omitted: the root LICENSE and
   the pyproject `license` field govern the whole work; source files stay free of
   non-functional boilerplate. Do not add headers.
4. Dependency audit (2026-09-13). The package itself has **no** required runtime
   dependencies; both database drivers are optional extras, installed separately
   by the user and imported dynamically, and neither is vendored or bundled into
   the wheel or sdist.

   | Dependency | Extra | License | Position |
   |---|---|---|---|
   | `oracledb` | `oracle` | Apache-2.0 OR UPL-1.0 | Permissive; compatible with AGPL-3.0 in one direction (into it), and imposes nothing on a commercial license. |
   | `psycopg` | `postgres` | LGPL-3.0-only | Compatible with AGPL-3.0. Under a commercial license it stays an LGPL work used through its published interface as a separately installed package, so LGPL §4/§5 are satisfied without any obligation on migr8's own source. Do not vendor it, statically bundle it, or modify it in-tree; any of those would change this analysis. |
   | `psycopg-binary` | `postgres` | LGPL-3.0-only; wheel carries libpq (PostgreSQL License) and OpenSSL (Apache-2.0) | Same position. Both bundled libraries are permissive. |
   | `pytest`, `pytest-timeout`, `ruff`, `mypy` | dev only | MIT | Not distributed with the package. |
   | `coverage` | dev only | Apache-2.0 | Not distributed with the package. |

   Re-audit whenever a dependency is added, an optional extra becomes required,
   or a dependency is vendored.
5. Copyright notices currently read "migr8 authors"; replace with the founder's
   entity name once it exists.
6. Test-environment container images (`gvenzl/oracle-free`, `postgres`) are pulled
   at test time and are never redistributed by this project. Oracle Database Free
   carries Oracle's own licence terms, which govern anyone who runs it; nothing in
   this repository grants or restricts those terms.
