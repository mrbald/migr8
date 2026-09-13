## What changed

<!-- The behaviour, not the diff. -->

## Checks

- [ ] `ruff check` and `ruff format --check` pass
- [ ] `mypy src` passes
- [ ] `pytest -m 'not oracle and not postgres'` passes, and the coverage gate holds
- [ ] Live Oracle and PostgreSQL suites pass, or are reported as NOT RUN with the reason
- [ ] Prose reviewed against `AGENTS.md`, including comments, docstrings and operator messages
- [ ] `docs/SPEC.md` still matches the behaviour; evidence in `docs/ACCEPTANCE.md` is dated and scoped
