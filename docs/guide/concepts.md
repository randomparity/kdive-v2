# Domain concepts

KDIVE models the kernel development lifecycle as six durable objects, each backed
by a Postgres row with an explicit state machine. Replacing the PoC's single
run-centric object with six independent lifecycles makes leasing, reprovisioning,
and multi-allocation investigations expressible without special cases
([ADR-0003](../adr/0003-six-durable-objects.md)).

## The six objects

```
(principal / project) ──< Investigation ──┐
                                          ├──< Run ──< DebugSession
   Resource ──< Allocation ──< System ────┘
```

**Resource** is a bookable thing registered by a provider: a local libvirt host, a
remote machine, a cloud instance type. Resources have capabilities (architecture,
console transports, PCIe devices) and a health status. They are discovered or
registered; the agent does not create them.

**Allocation** is a principal's claim on a Resource for a time window. It passes
through admission control — capability match, RBAC check, quota/budget check, and
capacity check — before transitioning from `requested` to `granted` to `active`.
Accounting events emit on every transition. An Allocation carries a lease expiry;
when it expires, in-flight jobs drain and then the owning Systems are torn down.

**System** is a provisioned, bootable instance produced by applying a provisioning
profile to an Allocation. A System is `defined → provisioning → ready`. Installing
a new kernel and rebooting does not make a new System — only an OS reprovision does.
One Allocation can host sequential Systems (reprovision in place). A System never
outlives its Allocation.

**Investigation** is a campaign grouping the sequence of Runs toward a goal — a
bug fix or a feature. Its lifetime is independent of any single Allocation: an
Investigation may span System reprovisions, multiple Allocations, and different
resource kinds. It becomes `active` when its first Run is created and is closed
explicitly by the agent. Closing or abandoning an Investigation does not cascade to
its Runs; they stay queryable for narrative and cost audit.

**Run** is the join point: it belongs to exactly one System (fixing its Allocation)
and exactly one Investigation. A Run covers one build→install→boot attempt. The
agent's main loop is many Runs against one persistent System, each run carrying at
most one DebugSession. Allocation and provisioning happen once; iteration across
Runs is cheap. A Run can only be created on a `ready` System whose Allocation is
`active`.

**DebugSession** is a sub-object of a Run, bounded by a single boot of a single
kernel. Within one boot a session may detach and re-attach any number of times; the
cycle ends only at reboot. A session is a durable row — not just worker-side state
— so the reconciler can detect a `live` session whose transport has died and move it
to `detached`. A reboot ends the session; the next attach after a reboot belongs to
the next Run.

## Lifecycle ordering

Within the `Resource → Allocation → System → Run` chain, **lower layers outlive
higher ones**: a Run never outlives its System, a System never outlives its
Allocation. The reconciler enforces this: a System whose Allocation is released or
expired is torn down; a Run on a torn-down System is failed. Investigation sits
outside this nesting — it is a cross-cutting grouping whose `(principal, project)`
scope is independent of any one Allocation.

See [ADR-0003](../adr/0003-six-durable-objects.md) and
[ADR-0026](../adr/0026-investigation-run-lifecycle.md) for the full state machines
and concurrency decisions.
