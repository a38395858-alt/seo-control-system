# Local deployment

The stable local workspace runs at `http://127.0.0.1:8000`.

## Current runtime

- Frontend: production Vite build in `web/`
- Backend: `python -m seo_control serve`
- Database: local SQLite emergency runtime at `data/seo-control.sqlite3`
- Queue: durable local queue with two workers
- Startup: Windows scheduled task `SEO-Control-LocalServer`

Run manually:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-local-environment.ps1
```

The scheduled task starts the same script after the current Windows user logs in.

## PostgreSQL runtime

PostgreSQL data and the persisted cutover state are retained. The PostgreSQL runtime can be restored with `start-workspace-postgres.ps1` after Docker Desktop is healthy. Docker Desktop 4.82 currently fails before the engine starts because its Windows inference socket cannot be reopened; this is outside the application process.

Do not delete `data/runtime-database-state.json`, the Docker volumes, or the two timestamped stale socket-directory backups while diagnosing Docker Desktop.
