#!/usr/bin/env python3
"""Create, inspect, and activate local CSI OpenBase creator workspaces."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from admin_app.workspace import (  # noqa: E402
    WorkspaceError,
    activate_workspace,
    default_data_home,
    initialize_workspace,
    list_workspaces,
    load_active_workspace,
    load_workspace,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--home",
        type=Path,
        help="workspace registry root (default: CSI_OPENBASE_HOME or repository var)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="create a creator workspace")
    create.add_argument("--slug", required=True)
    create.add_argument("--display-name", required=True)
    create.add_argument("--database")
    create.add_argument("--platform", default="douyin")
    create.add_argument("--profile-url", default="")
    create.add_argument("--no-activate", action="store_true")

    activate = commands.add_parser("activate", help="select the active workspace")
    activate.add_argument("slug")

    show = commands.add_parser("show", help="show workspace metadata")
    show.add_argument("slug", nargs="?")

    commands.add_parser("list", help="list configured workspaces")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    home = (args.home or default_data_home()).resolve()
    try:
        if args.command == "create":
            workspace = initialize_workspace(
                slug=args.slug,
                display_name=args.display_name,
                database_name=args.database,
                platform=args.platform,
                profile_url=args.profile_url,
                home=home,
                activate=not args.no_activate,
            )
            print(f"Workspace created: {workspace.slug}")
            print(f"Data directory: {workspace.directory}")
            print(f"Database: {workspace.database_name}")
            return 0
        if args.command == "activate":
            workspace = activate_workspace(args.slug, home=home)
            print(f"Active workspace: {workspace.slug} ({workspace.display_name})")
            print("Restart the dashboard and worker to apply this workspace.")
            return 0
        if args.command == "show":
            workspace = (
                load_workspace(args.slug, home=home)
                if args.slug
                else load_active_workspace(home=home)
            )
            print(json.dumps(workspace.as_manifest(), ensure_ascii=False, indent=2))
            print(f"data_directory: {workspace.directory}")
            return 0
        active_slug = ""
        try:
            active_slug = load_active_workspace(home=home).slug
        except WorkspaceError:
            pass
        for workspace in list_workspaces(home=home):
            marker = "*" if workspace.slug == active_slug else " "
            print(
                f"{marker} {workspace.slug}\t{workspace.display_name}\t"
                f"{workspace.database_name}"
            )
        return 0
    except WorkspaceError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
