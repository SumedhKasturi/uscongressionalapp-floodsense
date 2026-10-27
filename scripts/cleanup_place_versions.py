"""Weekly cleanup: delete every places version except the latest one per ZIP.

Calls the Supabase function ``delete_old_place_versions()`` with the service-role key.
Reads ``SUPABASE_URL`` and ``SUPABASE_SERVICE_KEY`` from the environment (GitHub
Actions secrets). Exits non-zero on any error so the workflow fails visibly.

Run from ``uscongressionalapp-floodsense/``:  python scripts/cleanup_place_versions.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make uscongressionalapp-floodsense/ importable (config, data) when this file is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from data import supabase_rest  # noqa: E402

# Defined in supabase/schema.sql; only the service role may execute it.
FUNCTION = "delete_old_place_versions"


def main() -> int:
    """Run the cleanup and print how many old versions were deleted.

    Returns 0 on success and 1 when a setting is missing, Supabase fails, or the
    answer is not a row count.
    """
    url = config.get_setting("SUPABASE_URL")
    key = config.get_setting("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set", file=sys.stderr)
        return 1
    try:
        deleted = supabase_rest.rpc(FUNCTION, key, url=url)
    except (supabase_rest.SupabaseUnavailable, supabase_rest.SupabaseError) as exc:
        print(f"Cleanup failed: {exc}", file=sys.stderr)
        return 1
    # The function returns an integer count. bool is a subclass of int, so rule it out explicitly.
    if not isinstance(deleted, int) or isinstance(deleted, bool):
        print(f"Cleanup failed: unexpected response {deleted!r}", file=sys.stderr)
        return 1
    print(f"Deleted {deleted} old place versions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
