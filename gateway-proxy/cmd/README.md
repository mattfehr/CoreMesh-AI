# Gateway command

<code>main.go</code> is the executable composition root. It loads and validates
gateway environment configuration, constructs the complete handler, registers
the local <code>/healthz</code> endpoint, forwards every other path to that
handler, and blocks on <code>http.ListenAndServe</code> at port 8080.

Construction is intentionally eager for configured infrastructure: Redis is
pinged before the listener opens, and the PostgreSQL autopilot store performs a
bounded ping when its DSN is set. The optional semantic cache creates its
selected embedding provider during construction. Fatal configuration,
connection, construction, or listener errors terminate the process through the
standard logger.

The health endpoint bypasses rate limiting, routing, caching, and upstreams. It
proves only that the gateway process and listener are alive.

Keep policy out of this package. New middleware belongs under
<code>internal</code> and should be composed here only through the gateway
factory so tests can continue to build core handlers with injected fakes.
