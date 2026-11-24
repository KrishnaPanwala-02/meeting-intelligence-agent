"""
Administrator commands (run inside the api container):

    python -m src.cli reset-token user@example.com
    python -m src.cli revoke-sessions user@example.com

`reset-token` is for deployments without e-mail: it prints a single-use
password-reset token that the user submits to POST /auth/reset-password.
The token grants access to the account, so deliver it over a trusted channel.
"""

import argparse
import sys

from .models.database import User
from .models.db import SessionLocal, init_db
from .services import auth


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("reset-token", "revoke-sessions"):
        sub.add_parser(name).add_argument("email")
    args = parser.parse_args(argv)

    init_db()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == auth.normalize_email(args.email)).first()
        if user is None:
            print("No such user.", file=sys.stderr)
            return 1
        if args.command == "reset-token":
            token = auth.create_reset_token(db, user)
            print(f"Reset token for {user.email} "
                  f"(valid {auth.reset_ttl_minutes()} min, single use):\n{token}")
        else:
            print(f"Revoked {auth.revoke_all_sessions(db, user.id)} session(s) "
                  f"for {user.email}.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
