# Graph Trial Match — Frontend (PIR visualization)

React + Vite UI for trial match results. Calls the backend **`/api/*`** routes served by `pir_visualization` in the parent repo.

## Prerequisites

- Node.js 18+
- Backend running at http://127.0.0.1:8000 (see parent [README](../README.md))
- MySQL data for your NCT (run `POST /test_engine` first)

## Run

```powershell
cd C:\Users\ADMIN\Documents\graph_trial_match\frontend
npm install
npm run dev
```

Open http://localhost:5173 (or the URL Vite prints).

Vite proxies `/api` → `http://localhost:8000` (see `vite.config.js`).

## Usage

1. Enter an **NCT ID** (same as used in `/test_engine`).
2. Click **Load**.
3. Use **Inclusion** / **Exclusion** tabs and criterion list to explore graphs.

## Override API URL

If the backend is on another port:

```powershell
$env:VITE_API_BASE="http://127.0.0.1:8001"
npm run dev
```

Or add `frontend/.env.local`:

```
VITE_API_BASE=http://127.0.0.1:8000
```

## Build for production

```powershell
npm run build
npm run preview
```

Serve `dist/` behind a reverse proxy that forwards `/api` to the FastAPI backend.
