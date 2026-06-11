"""The `Setting` descriptor and the snapshot-resolving `Registry` (ADR-0087).

`Setting` is one declaration per `KDIVE_*` variable — the registry's atom of truth.
`Registry` holds the declared settings and resolves them against a *snapshot* of the
environment taken at `load()` time (not a permanent process-global cache), so a test
that sets `KDIVE_*` per case via `monkeypatch.setenv` then calls `load()` sees its own
value rather than a frozen first read.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from kdive.domain.errors import CategorizedError, ErrorCategory

# The runnable subcommands a setting may declare it is consumed by.
RUNNABLE: frozenset[str] = frozenset({"server", "worker", "reconciler", "migrate"})


def never_required(env: Mapping[str, str]) -> bool:
    """The default ``required_when``: a setting is never required by default.

    Exposed (not private) so the reference generator can distinguish a genuinely
    optional setting from one with a conditional predicate that is merely false for an
    empty environment.
    """
    return False


@dataclass(frozen=True, slots=True)
class Setting[T]:
    """One declared `KDIVE_*` variable.

    Args:
        name: The environment variable name (must start with ``KDIVE_``).
        parse: Callable turning the raw string into the typed value.
        default: The raw default string used when the variable is unset; ``None`` means
            the setting resolves to ``None`` when absent.
        secret: Whether the value is secret material (routed through the redaction path;
            shown ref-only in the generated reference).
        processes: The subset of :data:`RUNNABLE` commands that consume this setting.
        group: A logical category (e.g. ``database``, ``objectstore``, ``remote-libvirt``).
        help: One-line operator-facing description.
        suggest: The actionable fix surfaced on a validation failure.
        required_when: Predicate over the resolved environment deciding whether the
            setting must be present. Defaults to never-required.
    """

    name: str
    parse: Callable[[str], T]
    default: str | None = None
    secret: bool = False
    processes: frozenset[str] = field(default_factory=frozenset)
    group: str = "core"
    help: str = ""
    suggest: str = ""
    required_when: Callable[[Mapping[str, str]], bool] = never_required

    def __post_init__(self) -> None:
        if not self.name.startswith("KDIVE_"):
            raise ValueError(f"{self.name}: setting name must start with 'KDIVE_'")
        unknown = self.processes - RUNNABLE
        if unknown:
            raise ValueError(f"{self.name}: unknown processes {sorted(unknown)}")


class Registry:
    """Holds the declared settings and resolves them against a snapshot."""

    def __init__(self, settings: Sequence[Setting[Any]]) -> None:
        self._settings: tuple[Setting[Any], ...] = tuple(settings)
        by_name: dict[str, Setting[Any]] = {}
        for s in self._settings:
            if s.name in by_name:
                raise ValueError(f"duplicate setting {s.name}")
            by_name[s.name] = s
        self._by_name = by_name
        self._snapshot: dict[str, str] | None = None

    def load(self, env: Mapping[str, str]) -> None:
        """Snapshot the ``KDIVE_*`` subset of ``env``, replacing any prior snapshot."""
        self._snapshot = {k: v for k, v in env.items() if k.startswith("KDIVE_")}

    def reset(self) -> None:
        """Drop the snapshot so the next read re-snapshots from ``os.environ``."""
        self._snapshot = None

    def _env(self) -> dict[str, str]:
        if self._snapshot is None:
            import os

            self.load(os.environ)
        assert self._snapshot is not None
        return self._snapshot

    def all_settings(self) -> tuple[Setting[Any], ...]:
        return self._settings

    def env_snapshot(self) -> dict[str, str]:
        """Return a copy of the ``KDIVE_*`` snapshot the registry resolves against.

        For consumers (e.g. the diagnostics ``secret_ref`` check) that must evaluate a
        setting's ``required_when`` predicate against the same environment the registry
        sees, without reaching into ``os.environ`` directly and diverging from the snapshot.
        """
        return dict(self._env())

    def get[T](self, setting: Setting[T]) -> T | None:
        """Return the parsed value for ``setting`` from the snapshot, or its default.

        Returns ``None`` only when the variable is absent and the setting has no default.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` when the raw value does not parse.
        """
        raw = self._env().get(setting.name, setting.default)
        if raw is None:
            return None
        try:
            return setting.parse(raw)
        except (ValueError, TypeError) as exc:
            raise CategorizedError(
                f"{setting.name}: cannot parse {raw!r} ({exc})",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"variable": setting.name, "suggest": setting.suggest},
            ) from exc

    def require[T](self, setting: Setting[T]) -> T:
        """Like :meth:`get`, but fail with a named ``CONFIGURATION_ERROR`` if unset.

        Use for settings a reader cannot proceed without (no usable default).

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if the variable is absent and the
                setting has no default, or if the value does not parse.
        """
        value = self.get(setting)
        if value is None:
            raise CategorizedError(
                f"{setting.name} is not set",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"variable": setting.name, "suggest": setting.suggest},
            )
        return value

    def validate(self, process: str) -> None:
        """Fail fast on settings ``process`` requires that are missing or malformed.

        Startup-time validation: for each setting this ``process`` consumes, a
        ``required_when`` that holds against the snapshot with no value present is a
        missing-config failure, and a present-but-malformed value raises on parse.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` listing the missing/malformed
                variables and their ``suggest`` fixes.
        """
        env = self._env()
        missing: list[str] = []
        for s in self._settings:
            if process not in s.processes:
                continue
            present = s.name in env or s.default is not None
            if s.required_when(env) and not present:
                missing.append(s.name)
                continue
            if s.name in env:
                self.get(s)  # raises CONFIGURATION_ERROR on a malformed value
        if missing:
            lines = "\n".join(
                f"  - {n}: {self._by_name[n].suggest or 'required for this process'}"
                for n in missing
            )
            raise CategorizedError(
                f"missing required configuration for {process}:\n{lines}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"process": process, "missing": missing},
            )
