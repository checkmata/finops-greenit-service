"""
Scheduler — periodic jobs for snapshot persistence and recommendation generation.

snapshot_job: Runs on a timer, queries Prometheus via MetricsPort,
generates recommendations via recommendation_engine, and persists to TimescaleDB.
"""