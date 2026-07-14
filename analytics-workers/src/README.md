# Analytics worker source

This directory separates the two planned offline pipelines:

- [log_miner](log_miner/README.md) is the data-curation loop.
- [fine_tuner](fine_tuner/README.md) is the model-training loop.

There is currently no Python package, entry point, dependency manifest, or
worker runtime here. Add those at the <code>analytics-workers</code> boundary
when the first pipeline becomes executable so its much heavier dependencies do
not leak into the request-serving Python service.
