# Contributing

Read `docs/05-project-context.md`, the relevant Issue and `docs/06-work-log.md` before changes. Use one branch and PR per Issue; dependent PRs must name their base/dependencies. Do not merge PRs automatically. Preserve existing user changes and never commit unrelated WORK files, secrets, raw private recordings or generated reports.

Python 3.12 is the initial validated environment. In PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m aivoicebench validate examples/test-case.example.yaml examples/test-cases/barge-in.json
```

Schema changes require a version decision, updated examples, meaningful positive/negative tests and migration notes. Structural schema invariants and cross-reference/runtime invariants must be documented separately. Never label synthetic fixtures or dry-runs as measured hardware results. New provider APIs/models require official-document verification before integration. No service credentials belong in manifests.

Record actual validation, untested hardware/provider dependencies, commits, push status, PR URLs and next steps in the work log. Windows executable/installer packaging and a verified local full workflow remain final product requirements after the CLI MVP.
