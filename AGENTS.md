# Working on migr8

## Writing

These rules apply to every file this project owns: documentation, comments,
docstrings, CLI messages, commit messages and CI output.

- Lead with the behavior, result, or required action.
- Use concrete subjects and verbs. Delete filler and rhetorical emphasis.
- Explain a non-obvious constraint once, next to the relevant contract or code.
- Avoid self-praise, "X, not Y" slogans, hypothetical inferior alternatives,
  "genuinely", and stock phrases about what tests "prove".
- Keep exact requirements, error codes, recovery steps, and evidence limits.
- Describe current behavior; keep change history in Git.
- Give measurements a source, date, and scope. Label unrun checks.
- Review prose in every diff, including comments and operator messages.

Technical contrasts and warnings stay when they carry a real constraint: the
transaction and recovery hazards in `docs/SPEC.md` and `docs/MANUAL.md` are the
reason those documents exist. Ruff and mypy do not enforce any of this; it is a
review rule. Do not add a prose linter, a readability score, or a word ban.

## Checks before a change is done

```sh
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/python -m pytest -m 'not oracle and not postgres'
testenv/dbctl.sh test            # live Oracle and PostgreSQL
```

Report a gate that did not run as NOT RUN rather than omitting it.
