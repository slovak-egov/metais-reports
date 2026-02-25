#!/usr/bin/env python3
import argparse
import getpass
import os
import sys

from metais.auth.metais_auth import (
    DEFAULT_BASE,
    DEFAULT_CLIENT_ID,
    bearer_from_user_pass_plain,
)

def main() -> int:
    ap = argparse.ArgumentParser(description="Interactive MetaIS login -> prints Bearer access_token.")

    ap.add_argument("--base", default=os.environ.get("METAIS_BASE", DEFAULT_BASE),
                    help="Base env label (test/prod/metais/...) or full URL.")
    ap.add_argument("--client-id", default=os.environ.get("METAIS_CLIENT_ID", DEFAULT_CLIENT_ID))
    ap.add_argument("--redirect-uri", default=os.environ.get("METAIS_REDIRECT_URI"),
                    help="Optional. If omitted, defaults to {base}/auth-success.")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--debug-html", default=None, help="Optional path to save the login HTML for debugging.")
    args = ap.parse_args()

    username = os.environ.get("METAIS_USER") or input("MetaIS username / email: ").strip()
    password = os.environ.get("METAIS_PASS") or getpass.getpass("MetaIS password: ")

    tok = bearer_from_user_pass_plain(
        username=username,
        password=password,
        verbose=args.verbose,
        base=args.base,
        client_id=args.client_id,
        redirect_uri=args.redirect_uri,
        debug_html_path=args.debug_html,
    )

    print(tok)
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)
