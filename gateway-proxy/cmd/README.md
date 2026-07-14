# Gateway command

<code>main.go</code> is the executable composition root. It loads and validates
gateway environment configuration, constructs the complete handler, registers
the local <code>/healthz</code> endpoint, forwards every other path to that
handler, and blocks on <code>http.ListenAndServe</code> at port 8080.

Construction is intentionally eager for mandatory infrastructure: Redis is
pinged before the listener opens. Optional semantic-cache and PostgreSQL
autopilot clients are also constructed when configured. Fatal configuration,
connection, construction, or listener errors terminate the process through the
standard logger.

The health endpoint bypasses rate limiting, routing, caching, and upstreams. It
proves only that the gateway process and listener are alive.

Keep policy out of this package. New middleware belongs under
<code>internal</code> and should be composed here only through the gateway
factory so tests can continue to build core handlers with injected fakes.
