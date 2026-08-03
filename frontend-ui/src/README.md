# Frontend source package

This source tree implements the browser-only CoreMesh control plane. It renders
server-owned execution, operational, and forensic state while keeping all HTTP
traffic behind the centralized Go gateway client.

| Path | Responsibility |
| --- | --- |
| `api` | Public contract projections, safe error parsing, in-memory response metadata, and the only Fetch boundary. |
| `components` | Responsive application shell, specialist result views, status/metric primitives, and React Flow canvas. |
| `lib` | Formatting, status semantics, and deterministic parent-span graph layout. |
| `pages` | Execution Studio, five-second Observability polling, and read-only Forensics explorer. |
| `test` | JSDOM setup and privacy-redacted fixtures shared by co-located unit tests. |
| `App.tsx` / `main.tsx` | Browser routes plus React Query and root-style composition. |
| `styles.css` | Dark/light design tokens, accessible interaction states, and responsive layouts. |

## State and network invariants

- `api/client.ts` is the sole network boundary. New pages must use it rather
  than calling Fetch directly, and production requests must target the gateway.
- Only the generated execution session ID and local display history persist in
  session storage. Gateway headers and theme state remain in memory; traces and
  metric snapshots stay server-owned.
- Route queries are cacheable server state. Unified execution is a mutation and
  must remain non-streaming/non-cacheable.
- Trace nodes derive only from redacted span contracts and
  `parent_span_id`; never infer or request artifact filesystem paths.
- State colors always retain a text label or icon, and every interactive
  control needs a keyboard-visible focus state.

Run `npm run lint`, `npm test`, and `npm run build` from `frontend-ui` after
changing this tree.
