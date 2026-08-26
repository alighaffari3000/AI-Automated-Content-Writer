# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Settings are read at import time, so the .env has to land first.
import os  # noqa: E402
from pathlib import Path  # noqa: E402

from dotenv import dotenv_values, load_dotenv  # noqa: E402

# Named explicitly rather than searched for. `python -m app.cli` imports this
# package while runpy is still resolving the module, before `__main__` has a
# `__file__` — which python-dotenv reads as a REPL, so it searches the current
# directory instead of this one. Run the CLI from anywhere but the project root
# and every setting silently falls back to its default: the operator's `check`
# then reports a pipeline nobody configured, while the service, which has a
# WorkingDirectory, reads the file fine.
_DOTENV = Path(__file__).resolve().parent.parent / ".env"

# What the machine owns, whatever the file says. Two things end up in a
# process's environment and they mean opposite things. The service unit sets
# DB_PATH because the database belongs to the machine and not to the checkout,
# and HOME and UV_CACHE_DIR because uv needs somewhere writable that this user
# owns; those are deployment, and a file copied from `.env.example` must not
# quietly move the database back into the code directory.
#
# Everything else in the environment is somebody's shell — and on a developer's
# machine that shell is shared with every other tool. A GEMINI_API_KEY exported
# once for something unrelated is not a decision about this pipeline, but it
# used to beat the file that was, and the only symptom was somebody else's
# quota running out. The file wins now, which is also what the deployment has
# always assumed: one file, one format, no second way of configuring things.
ENVIRONMENT_OWNS = frozenset({"DB_PATH", "HOME", "UV_CACHE_DIR"})


def _load(path: Path | None) -> None:
    if path is None:
        # Installed somewhere the file does not sit beside the code; let
        # python-dotenv look, and leave precedence as it finds it.
        load_dotenv()
        return
    for name, value in dotenv_values(path, encoding="utf-8").items():
        if value is None or (name in ENVIRONMENT_OWNS and name in os.environ):
            continue
        os.environ[name] = value


_load(_DOTENV if _DOTENV.exists() else None)

from .agent import app  # noqa: E402

__all__ = ["app"]
