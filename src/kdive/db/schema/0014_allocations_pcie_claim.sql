-- 0014_allocations_pcie_claim.sql — the resolved PCIe device claim (M1.4, ADR-0068).
-- `pcie_claim` is the durable snapshot of the devices an allocation holds: a JSON list
-- of `{vendor_id, device_id, bdf}` objects, resolved inside the per-Resource lock at
-- admission. Occupancy is DERIVED from this column on non-terminal allocations (no `free`
-- flag on the host descriptor), so the claim frees on every terminal transition simply by
-- the allocation leaving the non-terminal set — the row persists as a historical snapshot.
-- Defaults to `'[]'` so every existing/non-PCIe allocation reads as holding no device.

ALTER TABLE allocations
    ADD COLUMN pcie_claim jsonb NOT NULL DEFAULT '[]';
