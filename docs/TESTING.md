# Testing and production build

The project has no Python or npm package dependencies. GitLab CI runs backend
checks in `python:3.12-slim` and frontend checks/builds in `node:22-alpine`.

## Backend

Run from the repository root:

```powershell
python -m py_compile server.py llm_provider.py scripts/fetch_data.py
python scripts/check_dataset.py
python -m unittest discover -s tests -v
```

The unit tests cover data shape, forecast thresholds, public round data, and
portfolio calculations. The HTTP integration tests serve the real frontend,
create a session, submit all five rounds, verify result disclosure, and check
duplicate submission handling.

## Frontend

The frontend uses browser APIs and has no npm dependencies. Check JavaScript
syntax and produce the static production files with:

```powershell
node --check web/app.js
node --check scripts/build_frontend.mjs
node scripts/build_frontend.mjs
```

The build copies `web/index.html`, `web/app.js`, and `web/styles.css` into
`dist/`. CI verifies that each output file is present.

## GitLab CI

The `backend` and `frontend` jobs run in separate Python and Node images. The
`backend-tests` job executes unit and HTTP integration tests. A successful
pipeline requires all three jobs to pass.
