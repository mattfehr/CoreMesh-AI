# Gateway internal packages

Go's <code>internal</code> boundary prevents consumers outside this module from
importing gateway implementation details.

| Package | Status | Owns |
| --- | --- | --- |
| [gateway](gateway/README.md) | Implemented | Browser CORS, operational counters, mandatory Redis admission, and upstream resilience. |
| [autopilot](autopilot/README.md) | Implemented | Model-tier classification and optional experiment splits. |
| [cache](cache/README.md) | Implemented | Optional semantic response caching. |
| [flags](flags/README.md) | Placeholder | Planned general quality-aware feature flags. |
| [registry](registry/README.md) | Placeholder | Planned versioned prompt registry. |

The application wrapper handles CORS preflights and local observability first.
Proxied requests then enter the metrics wrapper, <code>autopilot</code>,
<code>cache</code> when enabled, and <code>gateway.Proxy</code>. Do not reorder
these packages without updating CORS, counter, cache-policy/model-scope tests
and the architecture documentation.
