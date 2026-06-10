"""Registry mapping ``(group, subcommand)`` to a CLI verb handler and its argparse shape.

The registry is the single source of truth: :func:`add_subparsers` builds the parser tree
from it and :func:`run_verb` dispatches against it, so adding a verb is one ``Verb`` entry.
Mutating verbs (a later M2.2 task) append their own entries to this same tuple (ADR-0089).
"""

from __future__ import annotations

import argparse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from kdive.cli.commands import reads


@dataclass(frozen=True)
class Verb:
    """One CLI verb: its ``group subcommand`` path, handler, MCP tool, and argparse shape.

    ``tool`` is the MCP tool the handler calls. It is declared here so the read-only gate
    test (``tests/mcp/test_read_tools_annotated.py``) can prove, from the same registry that
    drives dispatch, that no curated read verb reaches a non-read-only tool (ADR-0089).
    """

    group: str
    sub: str
    handler: Callable[[argparse.Namespace], Awaitable[int]]
    tool: str
    positionals: tuple[str, ...] = ()
    options: tuple[str, ...] = ()


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
