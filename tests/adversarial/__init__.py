"""Red-team adversarial + property tests for the concurrency/auth core.

Each module attacks a stated invariant of a concurrency-, auth-, or
supply-chain-sensitive surface. A passing test corroborates the invariant under
real contention (regression value); a failing test reproduces a defect that a
fix must close.
"""
