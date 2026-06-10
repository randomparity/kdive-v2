"""The explicit manifest of setting-bearing module paths (ADR-0087 decision 2).

The registry force-loads each module here and aggregates its ``SETTINGS`` list, so the
full ``KDIVE_*`` set is available regardless of which provider a given process happened
to import. Providers are opt-in and lazily imported, so "whatever happened to import" is
not a complete set; this manifest is.

A new provider adds **one line per setting-bearing module** (not per variable); its
``SETTINGS`` live co-located in the provider package. ``kdive/config/`` is outside the
M2 portability gate's ``CORE_PREFIXES``, so adding a line here is not a gated core touch.
"""

from __future__ import annotations

SETTING_MODULES: tuple[str, ...] = (
    "kdive.config.core_settings",
    # Provider setting-bearing modules are appended as Task 1.6 adds their SETTINGS lists:
    #   "kdive.providers.local_libvirt.discovery",
    #   "kdive.providers.fault_inject.discovery",
    #   "kdive.providers.remote_libvirt.config",
)
