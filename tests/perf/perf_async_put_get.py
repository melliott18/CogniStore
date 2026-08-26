"""Compatibility entry point for the M1 scale and recovery qualification."""


def _main() -> int:
    import sys
    from pathlib import Path

    repository_root = str(Path(__file__).resolve().parents[2])
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)
    from tests.perf.qualification import main

    return main()


if __name__ == "__main__":
    raise SystemExit(_main())
