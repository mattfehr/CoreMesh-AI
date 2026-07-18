# Analytics worker source

This Python package separates CoreMesh's two offline feedback loops:

- [log_miner](log_miner/README.md) implements Phase 4.1 production failure
  curation, persistence, provider adapters, and scheduled execution.
- [fine_tuner](fine_tuner/README.md) remains the Phase 4.3 training placeholder.

Run modules from the <code>analytics-workers</code> boundary so imports resolve
as <code>src.*</code>. The package performs no work at import time; database and
model connections are lazy and occur only when a CLI command or injected
pipeline instance is invoked.
