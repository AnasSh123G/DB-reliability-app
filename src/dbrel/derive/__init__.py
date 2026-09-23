"""Derived/materialized tables built from the fact tables, on demand.

Nothing here is written by the ingest path. Each module in this package reads
`trip`/`trip_stop`/`station` and writes back a precomputed table or column;
re-running is always safe and the tables are always fully rebuilt, never
incrementally patched (see stop-delay-averages spec, "precomputed, not
live").
"""
