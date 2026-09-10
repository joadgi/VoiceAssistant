"""Guard against cp1252-written-as-UTF-8 mojibake in the source tree.

Why this exists as a test rather than a one-time cleanup: on 2026-09-09 an
editing tool rewrote three of the four files it touched with every em-dash
and ellipsis double-encoded (UTF-8 bytes decoded as cp1252, then re-encoded
as UTF-8). `window.py` alone had 49 corrupted sequences, `selection.py` 9,
`winapi.py` 7 — while `paste.py`, edited in the same session, came out
correct. So it is the tool, not the repo or the checkout.

The corruption is INVISIBLE to every other test in this suite:

  * the files stay valid UTF-8, so Python imports them fine;
  * the damage is in string literals and comments, not logic;
  * nothing else asserts on the exact text of a user-facing message.

The full 253-test suite passed with all 65 sequences present, while the
floating pill was showing the user "Too short â€” hold to talk" and the tray
menu read "Settingsâ€¦". A green suite proves nothing about this class of bug,
which is exactly why it needs its own gate.

Detection uses the cp1252 -> UTF-8 round-trip as its own detector instead of
a hand-written character class. A class was tried first and silently missed
U+20AC (the middle byte of a mojibaked em-dash), reporting the corrupt files
as clean.
"""

import io
import os

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SOURCE_DIRS = ("voiceassistant", "tests")
_TEXT_SUFFIXES = (".py", ".md", ".bat", ".txt")


def _source_files():
    """Every text file we author, excluding runtime state and dependencies.

    The root files are globbed rather than listed. A hardcoded list of four
    names was tried first and left setup.bat / run.bat / uninstall.bat /
    create_shortcut.bat unguarded — scripts that print text the user reads,
    and exactly the kind of file the corruption would be invisible in.
    """
    found = []
    for relative_dir in _SOURCE_DIRS:
        for dirpath, dirnames, filenames in os.walk(
                os.path.join(_REPO_ROOT, relative_dir)):
            dirnames[:] = [d for d in dirnames
                           if d not in ("__pycache__", "fixtures", "venv")]
            for name in sorted(filenames):
                if name.endswith(_TEXT_SUFFIXES):
                    found.append(os.path.join(dirpath, name))
    for name in sorted(os.listdir(_REPO_ROOT)):
        path = os.path.join(_REPO_ROOT, name)
        # settings.json is local runtime state, not authored source.
        if (os.path.isfile(path) and name.endswith(_TEXT_SUFFIXES)
                and name != "settings.json"):
            found.append(path)
    return found


def _mojibake_round_trip(text):
    """Return the repaired text if `text` is double-encoded, else None.

    A file is double-encoded exactly when re-encoding it to cp1252 and
    decoding that as UTF-8 succeeds AND changes something. Ordinary prose
    with real typography fails the cp1252 encode (or is an identity
    round-trip) and is correctly left alone.
    """
    try:
        repaired = text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None
    return repaired if repaired != text else None


@pytest.mark.parametrize("path", _source_files(),
                         ids=lambda p: os.path.relpath(p, _REPO_ROOT))
def test_source_file_is_not_double_encoded(path):
    text = io.open(path, encoding="utf-8", newline="").read()
    repaired = _mojibake_round_trip(text)
    if repaired is None:
        return

    # Report the actual damaged fragments; "file is corrupt" is not actionable.
    samples = []
    for original_line, fixed_line in zip(text.splitlines(), repaired.splitlines()):
        if original_line != fixed_line:
            samples.append("  %s\n     should be: %s"
                           % (original_line.strip()[:90], fixed_line.strip()[:90]))
        if len(samples) == 5:
            break

    pytest.fail(
        "%s is double-encoded (cp1252 written as UTF-8).\n"
        "Repair with: text.encode('cp1252').decode('utf-8'), preserving "
        "line endings via io.open(..., newline='').\nFirst offenders:\n%s"
        % (os.path.relpath(path, _REPO_ROOT), "\n".join(samples)))


def test_detector_catches_the_real_2026_09_09_corruption():
    """Pin the detector itself against the exact strings that shipped.

    Without this, a detector that silently matches nothing would make the
    gate above vacuously green — the first character-class attempt did
    exactly that.
    """
    corrupted = 'self.indicator.show_error("Too short â€” hold to talk")'
    repaired = _mojibake_round_trip(corrupted)
    assert repaired == 'self.indicator.show_error("Too short — hold to talk")'

    corrupted_ellipsis = 'QAction("Settingsâ€¦", self)'
    assert _mojibake_round_trip(corrupted_ellipsis) == 'QAction("Settings…", self)'


def test_detector_leaves_correct_typography_alone():
    """The repaired files must not be flagged on the next run (idempotence)."""
    for clean in ('pill.show_error("Too short — hold to talk")',
                  'QAction("Settings…", self)',
                  '# Worker → GUI marshalling',
                  'plain ascii only'):
        assert _mojibake_round_trip(clean) is None, clean
