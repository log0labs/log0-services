# Database

## Connection pool

When requests fail with "timed out while checking out a connection from the connection pool", the pool is too
small for the traffic or connections are being leaked. Check the configured maximum pool size and the wait queue
timeout first, then look for code paths that hold a connection while doing slow work. A pool of 2 connections on a
busy API is almost always a mistake. Raise it to 20-50 and watch the pool metrics.

## Slow queries

Run EXPLAIN on the slowest statement. A full collection scan in an aggregation, a missing index on the filter
field, or a $group over millions of documents are the usual causes. Add the index or precompute the aggregate.

## Disk space

When writes are rejected and the database logs "No space left on device", free disk space or expand the volume.
Check the largest collections and the oplog size.
