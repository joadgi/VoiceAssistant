"""Live gate for inline typing: real SendInput into a real focused window.

The fast suite fakes the injection layer, so it proves the arithmetic and the
state machine but says nothing about whether Windows actually delivers these
characters, whether backspaces land where the accounting thinks they do, or
whether the user's pre-existing text survives an erase. That is the part worth
proving on a real machine, because the failure mode is the user's own document.

It types into a QTextEdit owned by THIS process — never into whatever happens
to be in front. If the window cannot be brought to the foreground (an agent or
CI session usually cannot), every test SKIPS rather than typing blind: a test
that injects keystrokes into an unknown window is the exact accident this
feature exists to avoid.

Local/manual: `RUN_INLINE=1 pytest tests/integration/test_inline_typing_live.py -s`
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QTextEdit  # noqa: E402

from voiceassistant import winapi  # noqa: E402
from voiceassistant.paste import (  # noqa: E402
    INLINE_PARTIAL, INLINE_TYPED, Paster,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INLINE") != "1",
    reason="live inline-typing gate: set RUN_INLINE=1 (injects real keystrokes)",
)


def _pump(seconds=0.25):
    """Let Qt deliver the injected key events."""
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def box(qapp):
    """A real focused text box owned by this process, or SKIP."""
    edit = QTextEdit()
    edit.setWindowTitle("inline typing live gate")
    edit.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    edit.resize(600, 200)
    edit.show()
    edit.raise_()
    edit.activateWindow()
    hwnd = int(edit.winId())
    winapi.set_foreground_window(hwnd)
    _pump(0.6)
    if winapi.get_foreground_window() != hwnd:
        edit.close()
        pytest.skip("could not take the foreground — refusing to type blind "
                    "(run this from an interactive desktop session)")
    yield edit, hwnd
    edit.close()
    _pump(0.1)


@pytest.fixture
def paster():
    p = Paster()
    yield p
    p.shutdown()


def _run(p, job, *args):
    """Run one worker job synchronously and let Qt see the keystrokes."""
    job(*args)
    _pump(0.35)


def test_streamed_words_land_exactly(box, paster):
    edit, hwnd = box
    paster._begin_inline_job(hwnd, 1)
    for draft in ("Here is", "Here is the first", "Here is the first point"):
        _run(paster, paster._type_to_job, hwnd, 1, draft)
    assert edit.toPlainText() == "Here is the first point"
    assert paster._typed.certain is True
    assert paster._typed.text == edit.toPlainText(), "the record drifted from the window"
    print(f"\n  streamed -> {edit.toPlainText()!r}")


def test_a_revision_replaces_only_the_changed_words(box, paster):
    edit, hwnd = box
    paster._begin_inline_job(hwnd, 1)
    _run(paster, paster._type_to_job, hwnd, 1, "The second point is the first")
    _run(paster, paster._type_to_job, hwnd, 1, "The second point follows from it")
    assert edit.toPlainText() == "The second point follows from it"
    assert paster._typed.text == edit.toPlainText()
    print(f"\n  revised  -> {edit.toPlainText()!r}")


def test_final_reconciliation_leaves_the_accurate_text(box, paster):
    edit, hwnd = box
    paster._begin_inline_job(hwnd, 1)
    _run(paster, paster._type_to_job, hwnd, 1, "Here is the frist point")
    done = []
    _run(paster, paster._finalize_inline_job, hwnd, 1, "Here is the first point.",
         lambda outcome, hwnd_, text: done.append(outcome))
    assert done == [INLINE_TYPED]
    assert edit.toPlainText() == "Here is the first point."
    print(f"\n  finalized -> {edit.toPlainText()!r}")


def test_erase_removes_our_draft_and_nothing_else(box, paster):
    """The one that matters most: the user's own text must survive."""
    edit, hwnd = box
    existing = "USER NOTE THAT MUST SURVIVE. "
    edit.setPlainText(existing)
    cursor = edit.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    edit.setTextCursor(cursor)
    _pump(0.1)

    paster._begin_inline_job(hwnd, 1)
    _run(paster, paster._type_to_job, hwnd, 1, "A draft that gets dropped")
    assert edit.toPlainText() == existing + "A draft that gets dropped"
    _run(paster, paster._cancel_inline_job, 1, True)
    assert edit.toPlainText() == existing, "erase did not stop at our own text"
    print(f"\n  after erase -> {edit.toPlainText()!r}")


def test_punctuation_and_unicode_survive_the_trip(box, paster):
    edit, hwnd = box
    text = "Ship 12 units — cost $69.99, 30% margin (ASIN B0C1234XYZ)."
    paster._begin_inline_job(hwnd, 1)
    _run(paster, paster._type_to_job, hwnd, 1, text)
    assert edit.toPlainText() == text
    print(f"\n  unicode -> {edit.toPlainText()!r}")


def test_typing_refuses_when_focus_is_elsewhere(box, paster):
    """Injection must stop the moment the target is not in front."""
    edit, hwnd = box
    paster._begin_inline_job(hwnd, 1)
    _run(paster, paster._type_to_job, hwnd, 1, "Before")
    before = edit.toPlainText()
    # Claim a different, non-existent target: the guard compares against the
    # real foreground window, so this is the same condition as focus moving.
    ok, sent = winapi.send_text("SHOULD NOT APPEAR", expected_hwnd=hwnd + 1)
    _pump(0.2)
    assert (ok, sent) == (False, 0), "injection proceeded for the wrong window"
    assert edit.toPlainText() == before
    assert "SHOULD NOT APPEAR" not in edit.toPlainText()


def test_uncertain_state_never_erases(box, paster, monkeypatch):
    """After a partial batch the count is unknown, so no backspace may follow."""
    edit, hwnd = box
    existing = "KEEP ME. "
    edit.setPlainText(existing)
    cursor = edit.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    edit.setTextCursor(cursor)
    _pump(0.1)

    paster._begin_inline_job(hwnd, 1)
    real_send = winapi.send_text

    def partial(text, expected_hwnd=None):
        real_send(text[:3], expected_hwnd=expected_hwnd)
        return False, None

    monkeypatch.setattr(winapi, "send_text", partial)
    _run(paster, paster._type_to_job, hwnd, 1, "draft text")
    monkeypatch.setattr(winapi, "send_text", real_send)
    after_partial = edit.toPlainText()
    assert paster._typed.certain is False

    _run(paster, paster._cancel_inline_job, 1, True)
    assert edit.toPlainText() == after_partial, "erased against an unknown count"
    assert edit.toPlainText().startswith(existing), "the user's text was touched"

    paster._begin_inline_job(hwnd, 2)
    monkeypatch.setattr(winapi, "send_text", partial)
    _run(paster, paster._type_to_job, hwnd, 2, "more draft")
    monkeypatch.setattr(winapi, "send_text", real_send)
    done = []
    _run(paster, paster._finalize_inline_job, hwnd, 2, "Final text.",
         lambda outcome, hwnd_, text: done.append(outcome))
    assert done == [INLINE_PARTIAL]
    print(f"\n  uncertain left in place -> {edit.toPlainText()!r}")
