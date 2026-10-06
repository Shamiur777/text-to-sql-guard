"""`python -m text_to_sql.demo` is a shortcut for `python -m text_to_sql demo`."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main(["demo"]))
