"""Shared Resource capability keys."""

CONCURRENT_ALLOCATION_CAP_KEY = "concurrent_allocation_cap"

# Billable size ceilings the discovery provider advertises and admission's ≤ resource-caps
# check reads (ADR-0007 §2). A selector may not exceed these.
VCPUS_KEY = "vcpus"
MEMORY_MB_KEY = "memory_mb"
