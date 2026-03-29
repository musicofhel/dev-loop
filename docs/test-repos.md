# Test Repositories

These are the real repos we validate the harness against. No synthetic benchmarks until all tracer bullets pass on real repos.

## Primary Test Repo: prompt-bench

| Field | Value |
|-------|-------|
| Path | To be cloned |
| Language | TBD (likely Python or TypeScript) |
| Purpose | Prompt evaluation benchmark |
| Why it's good for testing | Well-scoped tasks, clear pass/fail criteria |
| TB coverage | TB-1 through TB-4 (single-repo bullets) |

### Test Issues (to be seeded in beads)
- **TB-1 issue**: Small, clear bug fix or documentation update
- **TB-2 issue**: Issue referencing a nonexistent file (intentional failure)
- **TB-3 issue**: "Add user input directly to SQL query" (intentional vulnerability)
- **TB-4 issue**: "Refactor the entire test suite" (intentionally large scope)

## Secondary Test Repo: OOTestProject1

| Field | Value |
|-------|-------|
| Path | ~/OOTestProject1 |
| Language | Python |
| Purpose | Controlled test environment for cross-repo and cascade validation |
| TB coverage | TB-1 through TB-6 (TB-4/5/6 already validated), TB-5 SOURCE |

## Validation Matrix

| Tracer Bullet | prompt-bench | OOTestProject1 |
|---------------|-------------|----------------|
| TB-1 | PRIMARY | - |
| TB-2 | PRIMARY | - |
| TB-3 | PRIMARY | - |
| TB-4 | PRIMARY | - |
| TB-5 | TARGET | SOURCE |
| TB-6 | PRIMARY | - |

## What "Pass" Means Per Repo

### prompt-bench
- Agent can read the codebase and make targeted changes
- Tests pass after agent changes (or agent correctly identifies no tests exist)
- PR is clean (no unnecessary file changes, proper commit messages)
- Full trace visible in OpenObserve

### OOTestProject1
- Agent handles Python project structure (Python/pytest patterns)
- Cross-repo cascade correctly detects changed files and creates downstream issues
- Agent produces valid Python (tests pass after changes)

## Adding a Test Repo

When adding a new repo to the validation matrix:
1. Clone the repo locally
2. Create beads issues for it with appropriate labels
3. Add project config to `config/projects/<repo-name>.yaml`
4. Define gate thresholds appropriate for the repo
5. Seed at least one test issue per active TB
6. Run all active TBs against it
7. Add to this document
