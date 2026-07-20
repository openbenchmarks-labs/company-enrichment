"""Compatibility entry point for the reproducible firmographic v2 policy refresh.

The implementation lives in ``refresh_firmographic_headcount_rule`` to retain
the original command path used while the exact-count change was developed.
Use this v2-named entry point for release and future reruns.
"""
from refresh_firmographic_headcount_rule import main


if __name__ == "__main__":
    raise SystemExit(main())
