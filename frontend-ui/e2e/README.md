# Frontend browser flows

These Playwright tests exercise the dashboard in system Chrome or Playwright
Chromium while intercepting the public Go gateway contract. They make no live
runtime, database, Redis, provider, or model calls.

`dashboard.spec.ts` executes RAG, SQL, and agent modes, checks per-run gateway
metadata, opens the observability view, selects a failed/root-cause trace node,
asserts every API URL uses port 8080, and verifies a phone viewport has no
horizontal overflow.

Run with Playwright-managed Chromium:

~~~powershell
npx playwright install chromium
npm run test:e2e
~~~

Or use an installed Chrome channel:

~~~powershell
$env:PLAYWRIGHT_CHANNEL = "chrome"
npm run test:e2e
~~~

Keep gateway interception exact. A new production API call should cause these
tests to fail until its port-8080 route and privacy-safe fixture are explicit.
