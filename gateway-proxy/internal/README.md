# Gateway internal packages

Go's <code>internal</code> boundary prevents consumers outside this module from
importing gateway implementation details.

| Package | Status | Owns |
| --- | --- | --- |
| [gateway](gateway/README.md) | Implemented | Mandatory Redis admission and upstream resilience plus middleware composition. |
| [autopilot](autopilot/README.md) | Implemented | Model-tier classification and optional experiment splits. |
| [cache](cache/README.md) | Implemented | Optional semantic response caching. |
| [flags](flags/README.md) | Placeholder | Planned general quality-aware feature flags. |
| [registry](registry/README.md) | Placeholder | Planned versioned prompt registry. |

The request enters <code>autopilot</code>, then <code>cache</code> when enabled,
then <code>gateway.Proxy</code>. Do not reorder these packages without updating
cache-policy/model-scope tests and the architecture documentation.
