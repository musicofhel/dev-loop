# Test Repositories

These are the real repos we validate the harness against. No synthetic benchmarks until all tracer bullets pass on real repos.

## Primary Test Repo: OOTestProject1

| Field | Value |
|-------|-------|
| Path | ~/OOTestProject1 |
| Language | Python |
| Purpose | Controlled test environment for all tracer bullets |
| TB coverage | TB-1 through TB-7 (TB-5 SOURCE) |

### Structure
```
src/oo_test_project/
  __init__.py
  calculator.py
  evaluator.py
  scoring.py
  db/__init__.py
  db/users.py
tests/
  test_evaluator.py
  test_scoring.py
```

### Test Issues (seeded in beads)
- **TB-1 issue**: Small, clear bug fix or documentation update
- **TB-2 issue**: Issue referencing a nonexistent file (intentional failure)
- **TB-3 issue**: "Add user input directly to SQL query" (intentional vulnerability)
- **TB-4 issue**: "Refactor the entire test suite" (intentionally large scope)

## Secondary Test Repo: OOTestProject2

| Field | Value |
|-------|-------|
| Path | ~/OOTestProject2 |
| Language | Python |
| Purpose | Cascade test target — downstream of OOTestProject1 |
| TB coverage | TB-5 TARGET, TB-1 through TB-4 and TB-6 standalone |

### Structure
```
src/oo_test_project2/
  __init__.py
  models.py       # User data models (mirrors upstream schema)
  reports.py      # Report generation from user data
  formatters.py   # Output formatting (text, CSV, table)
  validators.py   # Schema validation for incoming data
tests/
  test_models.py
  test_reports.py
  test_formatters.py
  test_validators.py
```

### Cascade Relationship
OOTestProject1 changes to `src/oo_test_project/db/**` trigger cascade issues in OOTestProject2. The dependency is real: OOTestProject2's `validators.py` defines field requirements and `models.py` defines data structures that must match the upstream schema.

## Validation Matrix

| Tracer Bullet | OOTestProject1 | OOTestProject2 |
|---------------|----------------|----------------|
| TB-1 | PRIMARY | Standalone |
| TB-2 | PRIMARY | Standalone |
| TB-3 | PRIMARY | Standalone |
| TB-4 | PRIMARY | Standalone |
| TB-5 | SOURCE | TARGET |
| TB-6 | PRIMARY | Standalone |
| TB-7 | PRIMARY | - |

## What "Pass" Means

### OOTestProject1
- Agent handles Python project structure (Python/pytest patterns)
- Agent can read the codebase and make targeted changes
- Tests pass after agent changes (or agent correctly identifies no tests exist)
- PR is clean (no unnecessary file changes, proper commit messages)
- Full trace visible in OpenObserve

### OOTestProject2
- Agent handles Python project structure (Python/pytest patterns)
- Cascade issues correctly reference upstream schema changes
- Agent updates validators.py and models.py when upstream schema changes
- Tests pass after agent changes

## Adding a Test Repo

When adding a new repo to the validation matrix:
1. Clone the repo locally
2. Create beads issues for it with appropriate labels
3. Add project config to `config/projects/<repo-name>.yaml`
4. Define gate thresholds appropriate for the repo
5. Seed at least one test issue per active TB
6. Run all active TBs against it
7. Add to this document
