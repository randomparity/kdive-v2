"""Registry mapping ``(group, subcommand)`` to a CLI verb handler and its argparse shape.

The registry is the single source of truth: :func:`add_subparsers` builds the parser tree
from it and :func:`run_verb` dispatches against it, so adding a verb is one ``Verb`` entry.
Mutating verbs (a later M2.2 task) append their own entries to this same tuple (ADR-0089).
"""

from __future__ import annotations

import argparse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from kdive.cli.commands import mutations, reads


@dataclass(frozen=True)
class Verb:
    """One CLI verb: its ``group subcommand`` path, handler, MCP tool, and argparse shape.

    ``tool`` is the MCP tool the handler calls. It is declared here so the read-only gate
    test (``tests/mcp/test_read_tools_annotated.py``) can prove, from the same registry that
    drives dispatch, that no curated read verb reaches a non-read-only tool (ADR-0089).

    ``read_only`` distinguishes the curated read verbs (default ``True``) from the
    break-glass mutating verbs (``False``), whose ``tool`` is intentionally a
    ``destructive()``-annotated server tool. The gate test only holds read-only verbs to
    the read-only hint; the mutating verbs are reachable only through their curated handler,
    never the read-only passthrough.
    """

    group: str
    sub: str
    handler: Callable[[argparse.Namespace], Awaitable[int]]
    tool: str
    positionals: tuple[str, ...] = ()
    options: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    read_only: bool = True


REGISTRY: tuple[Verb, ...] = (
    Verb("resources", "list", reads.resources_list, "resources.list", options=("kind",)),
    Verb("resources", "describe", reads.resources_describe, "resources.describe", ("resource_id",)),
    Verb("allocations", "list", reads.allocations_list, "allocations.list", options=("project",)),
    Verb("allocations", "get", reads.allocations_get, "allocations.get", ("allocation_id",)),
    Verb("systems", "list", reads.systems_list, "systems.list", options=("state",)),
    Verb("systems", "show", reads.systems_show, "systems.get", ("system_id",)),
    Verb("runs", "show", reads.runs_show, "runs.get", ("run_id",)),
    Verb("jobs", "list", reads.jobs_list, "jobs.list"),
    Verb("jobs", "get", reads.jobs_get, "jobs.get", ("job_id",)),
    Verb("ledger", "show", reads.ledger_show, "accounting.usage_project", options=("project",)),
    Verb("inventory", "show", reads.inventory_show, "inventory.list", options=("project",)),
    Verb("secrets", "list", reads.secrets_list, "secrets.list"),
    Verb("fixtures", "list", reads.fixtures_list, "fixtures.list"),
    Verb(
        "teardown",
        "system",
        mutations.teardown,
        "ops.force_teardown",
        ("system_id",),
        options=("reason",),
        flags=("force",),
        read_only=False,
    ),
    Verb(
        "allocations",
        "force-release",
        mutations.allocations_force_release,
        "ops.force_release",
        ("allocation_id",),
        options=("reason",),
        read_only=False,
    ),
    Verb(
        "resources",
        "cordon",
        mutations.resources_cordon,
        "resources.cordon",
        ("resource_id",),
        read_only=False,
    ),
    Verb(
        "resources",
        "drain",
        mutations.resources_drain,
        "resources.drain",
        ("resource_id",),
        options=("mode", "reason"),
        read_only=False,
    ),
)


def _json_parent() -> argparse.ArgumentParser:
    """A parent parser letting ``--json`` follow the verb (e.g. ``resources list --json``).

    The default is ``SUPPRESS`` so an absent post-verb ``--json`` does not clobber the
    top-level ``--json`` already parsed onto the namespace (argparse subparser-default trap).
    """
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    return parent


def _verb_parser(
    group_parser: argparse._SubParsersAction, verb: Verb, parent: argparse.ArgumentParser
) -> None:
    """Add ``verb``'s sub-subparser, declaring its positionals and ``--`` options."""
    parser = group_parser.add_parser(verb.sub, parents=[parent])
    for positional in verb.positionals:
        parser.add_argument(positional)
    for option in verb.options:
        parser.add_argument(f"--{option.replace('_', '-')}", dest=option, default=None)
    for flag in verb.flags:
        parser.add_argument(f"--{flag.replace('_', '-')}", dest=flag, action="store_true")


def add_subparsers(sub: argparse._SubParsersAction) -> None:
    """Add one subparser per registry group, with a sub-subparser per verb."""
    parent = _json_parent()
    groups: dict[str, argparse._SubParsersAction] = {}
    for verb in REGISTRY:
        group_parser = groups.get(verb.group)
        if group_parser is None:
            parser = sub.add_parser(verb.group)
            group_parser = parser.add_subparsers(dest="subcommand", required=True)
            groups[verb.group] = group_parser
        _verb_parser(group_parser, verb, parent)


async def run_verb(args: argparse.Namespace) -> int:
    """Resolve ``(command, subcommand)`` against the registry and await its handler.

    Raises:
        SystemExit: When no registry entry matches the parsed command/subcommand.
    """
    subcommand = getattr(args, "subcommand", None)
    for verb in REGISTRY:
        if verb.group == args.command and verb.sub == subcommand:
            return await verb.handler(args)
    raise SystemExit(f"unknown command: {args.command} {subcommand or ''}".rstrip())
