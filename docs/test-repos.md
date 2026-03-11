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

## Secondary Test Repo: omniswipe-backend

| Field | Value |
|-------|-------|
| Path | ~/omniswipe-backend |
| Language | TypeScript (Fastify + Prisma 7) |
| Purpose | Production API backend |
| Why it's good for testing | Real complexity, real tests, real DB |
| TB coverage | TB-5 (as downstream target for cross-repo cascade) |

### Test Issues
- **TB-5 downstream**: Auto-created when prompt-bench API changes

## Tertiary Test Repo: enterprise-pipeline

| Field | Value |
|-------|-------|
| Path | ~/enterprise-pipeline |
| Language | Python (FastAPI + Qdrant) |
| Purpose | Production RAG pipeline |
| Why it's good for testing | Different language, validates cross-language support |
| TB coverage | TB-5 (as additional downstream target) |

## Validation Matrix

| Tracer Bullet | prompt-bench | omniswipe-backend | enterprise-pipeline |
|---------------|-------------|-------------------|---------------------|
| TB-1 | PRIMARY | - | - |
| TB-2 | PRIMARY | - | - |
| TB-3 | PRIMARY | - | - |
| TB-4 | PRIMARY | - | - |
| TB-5 | SOURCE | TARGET | TARGET |
| TB-6 | PRIMARY | - | - |

## What "Pass" Means Per Repo

### prompt-bench
- Agent can read the codebase and make targeted changes
- Tests pass after agent changes (or agent correctly identifies no tests exist)
- PR is clean (no unnecessary file changes, proper commit messages)
- Full trace visible in OpenObserve

### omniswipe-backend
- Agent respects Prisma 7 patterns (generated client import path)
- Agent doesn't break existing Maestro E2E tests
- Agent handles TypeScript strict mode (zero tsc errors post-change)
- Cross-repo issue correctly references the upstream change

### enterprise-pipeline
- Agent handles Python project structure (FastAPI, pytest)
- Agent doesn't break deployment configs
- Agent produces valid Python (passes mypy if configured)

## Adding a Test Repo

When adding a new repo to the validation matrix:
1. Clone the repo locally
2. Create beads issues for it with appropriate labels
3. Add project config to `config/projects/<repo-name>.yaml`
4. Define gate thresholds appropriate for the repo
5. Seed at least one test issue per active TB
6. Run all active TBs against it
7. Add to this document
