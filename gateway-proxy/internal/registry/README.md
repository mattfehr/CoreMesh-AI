# Prompt registry placeholder

This directory is reserved for versioned prompt lookup and hot reload. It
currently contains only <code>.gitkeep</code>; no Go package reads or mutates
<code>prompt_registry</code>.

The PostgreSQL table created by <code>init.sql</code> is a data contract, not an
implemented management service. Future code must document activation
atomicity, cache invalidation, audit history, fallback version, database outage
behavior, and how prompt versions interact with experiment headers.
