-- 0013_system_shapes.sql — M1.4 named system-shape catalog (ADR-0067).
-- Additive to 0012 (forward-only, ADR-0015). A shape fixes size only: vcpus / memory_mb /
-- disk_gb plus an optional, opaquely-stored pcie_match (the matcher grammar lands later).
-- memory_mb is constrained to whole-GB multiples so the resolver's memory_mb → memory_gb
-- mapping is exact (a shape can never price a fractional GB the cost Selector cannot model).
-- cost_class is NOT a shape field — it stays host-resolved at admission. The seed targets
-- the M1 dev host; an over-large shape is NOT rejected here (it fails closed at request via
-- validate_against_resource), so no host-cap check at seed time.

CREATE TABLE system_shapes (
    name       text PRIMARY KEY,
    vcpus      integer NOT NULL CONSTRAINT system_shapes_vcpus_check CHECK (vcpus > 0),
    memory_mb  integer NOT NULL
               CONSTRAINT system_shapes_memory_positive_check CHECK (memory_mb > 0)
               CONSTRAINT system_shapes_memory_whole_gb_check CHECK (memory_mb % 1024 = 0),
    disk_gb    integer NOT NULL CONSTRAINT system_shapes_disk_check CHECK (disk_gb > 0),
    pcie_match text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER system_shapes_set_updated_at BEFORE UPDATE ON system_shapes
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

-- Seed the named presets (data migration, bisectable like the cost-class seed). Sized to
-- the M1 dev host; pcie_match stays NULL until a shape needs a device.
INSERT INTO system_shapes (name, vcpus, memory_mb, disk_gb) VALUES
    ('small',  1,  1024, 10),
    ('medium', 2,  4096, 20),
    ('large',  4,  8192, 40),
    ('max',    8, 16384, 80);
