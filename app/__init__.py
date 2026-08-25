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
from pathlib import Path  # noqa: E402

from dotenv import load_dotenv  # noqa: E402

# Named explicitly rather than searched for. `python -m app.cli` imports this
# package while runpy is still resolving the module, before `__main__` has a
# `__file__` — which python-dotenv reads as a REPL, so it searches the current
# directory instead of this one. Run the CLI from anywhere but the project root
# and every setting silently falls back to its default: the operator's `check`
# then reports a pipeline nobody configured, while the service, which has a
# WorkingDirectory, reads the file fine.
_DOTENV = Path(__file__).resolve().parent.parent / ".env"
if _DOTENV.exists():
    load_dotenv(_DOTENV)
else:
    # Installed somewhere the file does not sit beside the code; let it look.
    load_dotenv()

from .agent import app  # noqa: E402

__all__ = ["app"]
