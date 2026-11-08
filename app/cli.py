"""Owner account management. There is no sign-up page; owners are created here.

    python -m app.cli create-owner <username>
    python -m app.cli set-password <username>
    python -m app.cli list-owners

Passwords are prompted for, or read from stdin with --password-stdin.
"""

import argparse
import getpass
import sys

from sqlalchemy import select

from app import auth
from app.db import SessionLocal
from app.models import Owner


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Repeat password: "):
        sys.exit("Passwords don't match")
    return password


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("create-owner", "set-password"):
        p = sub.add_parser(name)
        p.add_argument("username")
        p.add_argument("--password-stdin", action="store_true")
    sub.add_parser("list-owners")
    args = parser.parse_args(argv)

    with SessionLocal() as db:
        try:
            if args.command == "create-owner":
                owner = auth.create_owner(db, args.username, _read_password(args.password_stdin))
                print(f'Created owner "{owner.username}"')
            elif args.command == "set-password":
                auth.set_password(db, args.username, _read_password(args.password_stdin))
                print("Password updated")
            else:
                for owner in db.scalars(select(Owner).order_by(Owner.username)):
                    print(owner.username)
        except auth.OwnerError as e:
            sys.exit(str(e))


if __name__ == "__main__":
    main()
