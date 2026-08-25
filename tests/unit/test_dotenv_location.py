"""The CLI must see the same configuration from any directory.

`python -m app.cli` imports this package before runpy has given `__main__` a
`__file__`, and python-dotenv reads that as a REPL: it then searches the
current directory rather than the one the code lives in. Run from anywhere but
the project root — which is how the deployed alias runs it — and every setting
quietly fell back to its default, so `check` described a pipeline nobody had
configured while the service, which has a WorkingDirectory, read the same file
fine.
"""

from __future__ import annotations

from pathlib import Path

import app

ROOT = Path(app.__file__).resolve().parent.parent


def test_the_env_file_is_looked_for_beside_the_code():
    assert app._DOTENV == ROOT / ".env"  # noqa: SLF001 - the point of the test
