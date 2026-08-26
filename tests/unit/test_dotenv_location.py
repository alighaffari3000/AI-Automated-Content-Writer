"""Where the configuration comes from, and what beats what.

Two questions, and both were answered the wrong way once.

`python -m app.cli` imports this package before runpy has given `__main__` a
`__file__`, and python-dotenv reads that as a REPL: it then searches the
current directory rather than the one the code lives in. Run from anywhere but
the project root — which is how the deployed alias runs it — and every setting
quietly fell back to its default, so `check` described a pipeline nobody had
configured while the service, which has a WorkingDirectory, read the same file
fine.

Then, having found the right file, the loader let the machine's environment
beat it. On a developer's machine that environment is shared with every other
tool, so a key exported once for something unrelated won an argument it should
never have been in.
"""

from __future__ import annotations

import os
from pathlib import Path

import app

ROOT = Path(app.__file__).resolve().parent.parent


def test_the_env_file_is_looked_for_beside_the_code():
    assert app._DOTENV == ROOT / ".env"  # noqa: SLF001 - the point of the test


# ------------------------------------------------------------------ what wins


def load(tmp_path, text):
    """Run the package's own loader over a file of this test's making."""
    env = tmp_path / ".env"
    env.write_text(text, encoding="utf-8")
    app._load(env)  # noqa: SLF001 - the point of the test


def test_the_file_beats_a_variable_the_machine_already_had(monkeypatch, tmp_path):
    """The mistake this exists to stop.

    A developer's shell is shared with every other tool on the machine, so a
    GEMINI_API_KEY exported once for something unrelated used to beat the file
    that was actually about this pipeline. The only symptom was somebody
    else's quota running out, which is not a symptom anyone traces back here.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "the-machines-key")
    monkeypatch.setenv("CONTENT_TONE", "whatever the shell said")

    load(tmp_path, "GEMINI_API_KEY=the-projects-key\nCONTENT_TONE=plain\n")

    assert os.environ["GEMINI_API_KEY"] == "the-projects-key"
    assert os.environ["CONTENT_TONE"] == "plain"


def test_what_the_machine_owns_is_left_alone(monkeypatch, tmp_path):
    """The service unit sets DB_PATH because the database is not the checkout.

    Every deployed copy of that file began as `.env.example`, which says
    `DB_PATH=data/pipeline.db` — so letting the file win here would move a
    server's database into the code directory, silently, on the next update.
    """
    monkeypatch.setenv("DB_PATH", "/var/lib/content-writer/pipeline.db")

    load(tmp_path, "DB_PATH=data/pipeline.db\n")

    assert os.environ["DB_PATH"] == "/var/lib/content-writer/pipeline.db"


def test_what_the_machine_owns_still_has_a_default(monkeypatch, tmp_path):
    """Exempt from being overridden is not the same as ignored."""
    monkeypatch.delenv("DB_PATH", raising=False)

    load(tmp_path, "DB_PATH=data/pipeline.db\n")

    assert os.environ["DB_PATH"] == "data/pipeline.db"


def test_a_setting_the_file_does_not_mention_is_untouched(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "true")
    # Registered so the loader's write to it is undone with the rest.
    monkeypatch.setenv("CONTENT_TONE", "unchanged")

    load(tmp_path, "CONTENT_TONE=plain\n")

    assert os.environ["DRY_RUN"] == "true"
