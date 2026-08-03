# CoreMesh frontend dashboard

This Vite React + TypeScript application is the browser control plane for
CoreMesh. It provides execution, live gateway observability, and read-only
forensic trace exploration. Every API call uses one configured Go gateway
origin; browser code never calls the private Python runtime directly.

## Capabilities

- **Execution Studio** submits restricted RAG, text-to-SQL, and supervisor
  requests, retains display-only session history, and renders specialist-aware
  results.
- **Gateway Observability** polls five-second snapshots for admission
  configuration, cache hit rate, circuit state, routing distribution, and
  error counters.
- **Agent Forensics** filters redacted trace summaries and projects
  `parent_span_id` relationships into an interactive top-down React Flow tree.
- Gateway response headers expose per-run budget, cache, circuit, route, model,
  and retry metadata without adding another API origin.

The browser persists only its generated session ID and display history in
session storage. Theme selection and last visible gateway headers remain
in-memory. Trace artifacts and operational counters stay server-owned.

## Local development

Prerequisites are Node.js 22 or newer and npm.

~~~powershell
Set-Location frontend-ui
npm install
Copy-Item .env.example .env
npm run dev
~~~

Open <http://localhost:3000>. The Go gateway must be available at
<http://localhost:8080> and allow the frontend origin. The default environment
already targets that gateway:

| Variable | Default | Meaning |
| --- | --- | --- |
| `VITE_GATEWAY_BASE_URL` | `http://localhost:8080` | Only browser API origin. Embedded at build time; restart/rebuild after changing it. |

Do not point this value at port 8000. The runtime endpoints intentionally flow
through gateway admission, resilience routing, CORS, and response metadata.

## Production container

The multi-stage `Dockerfile` runs `npm ci`, builds static Vite assets, and
serves them from Nginx on port 3000. Nginx uses an SPA fallback so
`/observability`, `/forensics`, and trace query links survive page refreshes.

From the repository root, the complete local application profile is:

~~~powershell
docker compose --profile app up --build
~~~

This exposes the UI at port 3000, the gateway at 8080, and the runtime at 8000.
The UI still calls only 8080.

## Tests and quality checks

~~~powershell
npm run lint
npm test
npm run build
npx playwright install chromium
npm run test:e2e
~~~

If system Chrome is already installed, the browser download is optional:

~~~powershell
$env:PLAYWRIGHT_CHANNEL = "chrome"
npm run test:e2e
~~~

Vitest covers client/error parsing, all specialist result renderers,
observability empty/disabled states, execution keyboard access, storage
failure tolerance, and forensic graph construction. Playwright mocks the
gateway at `http://localhost:8080`, observes every browser fetch/XHR URL, runs
all three modes, refreshes metrics, selects a failed trace span, verifies theme
synchronization, and checks a phone viewport for horizontal overflow. Its
mocked flows make no provider or runtime calls.

## Directory map

| Path | Responsibility |
| --- | --- |
| `src/api` | Central gateway client and public contract projections. |
| `src/components` | Application shell, result renderers, metrics, and trace graph. |
| `src/lib` | Formatting, status semantics, and deterministic trace layout. |
| `src/pages` | Execution, Observability, and Forensics routes. |
| `src/test` | Shared DOM setup and redacted fixtures. |
| `e2e` | Browser-level gateway-origin and workflow tests. |
| `nginx.conf` | Port 3000 static hosting and SPA route fallback. |

## Security and scope

This dashboard is intended for local development and portfolio demonstrations.
It has no authentication, TLS termination, or tenant authorization. Trace
views are read-only and display only the runtime's redacted forensic contract.
Document indexing, feedback mutation, replay, approvals, and feature-flag
administration are outside this interface.
