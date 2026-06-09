"""Fault-injection mock provider (M1.5, ADR-0072).

A second :class:`~kdive.providers.runtime.ProviderRuntime` that satisfies every typed
provider port with synthetic-but-plausible outputs. M1.5 issue 2 ships the happy path
(no faults); the seeded fault engine (issue 3) and forced secret resolution (issue 4)
layer on later. The runtime is opt-in and absent from the default production composition
(ADR-0071).
"""
