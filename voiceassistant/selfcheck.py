"""Self-diagnostic — `python main.py --check`.

Answers "will this actually work on this machine?" without a human and without
the slow/heavy Whisper model download. Each probe is isolated so one failure
never aborts the report. Exit code: 0 if nothing REQUIRED failed, else 1.

Categories:
  REQUIRED — dictation (the primary feature) can't work without it.
  OPTIONAL — a secondary feature degrades but the app still runs.
Use `--check --deep` to also synthesize a real phrase with the neural voice
   (proves edge-tts is reachable, not just importable — an unreachable service now
   produces an honest error without changing voices) and to load the Whisper model (slow; first
run downloads ~1GB), proving the GPU/CPU transcription path end-to-end.
"""

import importlib
import sys

# (label, required?, fn) — fn returns (ok: bool, detail: str)
def _check_python():
    v = sys.version_info
    ok = v >= (3, 10)
    return ok, f"{v.major}.{v.minor}.{v.micro}" + ("" if ok else " (need 3.10+)")


def _check_import(mod):
    def probe():
        try:
            m = importlib.import_module(mod)
            return True, getattr(m, "__version__", "ok")
        except Exception as e:
            return False, f"{e.__class__.__name__}: {e}"
    return probe


def _check_cuda_runtime():
    """Are the CUDA DLLs CTranslate2 needs reachable (nvidia wheels or torch)?

    Presence only — not a load. WARN (not fail) because CPU fallback works.
    """
    import importlib.util

    have = []
    for mod in ("nvidia.cublas", "nvidia.cudnn"):
        try:
            if importlib.util.find_spec(mod) is not None:
                have.append(mod.split(".")[1])
        except Exception:
            pass
    if have:
        return True, "nvidia wheels: " + ", ".join(have)
    try:
        if importlib.util.find_spec("torch") is not None:
            return True, "provided by torch"
    except Exception:
        pass
    return False, "no CUDA runtime found — GPU transcription will fall back to CPU"


def _check_native_ocr():
    try:
        try:
            import winsdk._winrt as _winrt
            _winrt.init_apartment()
        except Exception:
            pass
        from winsdk.windows.media.ocr import OcrEngine
        eng = OcrEngine.try_create_from_user_profile_languages()
        if eng is None:
            return False, "winsdk present but no OCR language pack"
        return True, "Windows.Media.Ocr ready"
    except Exception as e:
        return False, f"unavailable ({e.__class__.__name__}) — install easyocr to use OCR"


def _check_vlc():
    try:
        import vlc
        inst = vlc.Instance("--no-video", "--quiet")
        if inst is None:
            return False, "python-vlc imported but VLC engine missing"
        inst.release()
        return True, "VLC ready (neural read-aloud playback)"
    except Exception as e:
        return False, f"no VLC ({e.__class__.__name__}) — install: winget install VideoLAN.VLC"


def _check_offline_tts():
    try:
        import pyttsx3
        eng = pyttsx3.init()
        n = len(eng.getProperty("voices"))
        eng.stop()
        return True, f"SAPI ready ({n} offline voices)"
    except Exception as e:
        return False, f"pyttsx3 unavailable ({e.__class__.__name__})"


def _check_neural_tts():
    """Actually reach the edge-tts service, don't just import the package.

    An importable edge_tts says nothing about whether synthesis works — and
    when it doesn't, read-aloud cannot use the selected neural voice. This is
    the check that names that cause instead of leaving the user to guess.

    DEEP-ONLY: it makes a real network call, so it must stay out of CHECKS
    (tests/test_selfcheck.py runs every entry there, and the fast suites must
    not depend on the network).
    """
    try:
        import asyncio
        import os
        import tempfile
        import edge_tts
        from .config import Config
        from .tts import NEURAL_META

        voice = Config().get("tts_voice", "en-US-AndrewNeural")
        if voice not in NEURAL_META:
            voice = "en-US-AndrewNeural"

        async def probe(path):
            # Consume the utterance fully through edge-tts's public save path.
            # Breaking after the first stream packet leaves its aiohttp response
            # closing asynchronously, so closing our loop immediately afterward
            # emits "Task was destroyed" / "Unclosed client session" noise.
            await edge_tts.Communicate("Test.", voice).save(path)
            return os.path.getsize(path)

        handle = tempfile.NamedTemporaryFile(
            prefix="voiceassist_check_", suffix=".mp3", delete=False
        )
        path = handle.name
        handle.close()

        loop = asyncio.new_event_loop()
        try:
            got = loop.run_until_complete(asyncio.wait_for(probe(path), timeout=10))
        finally:
            loop.close()
            try:
                os.remove(path)
            except OSError:
                pass
        if not got:
            return False, "connected but returned no audio - neural read-aloud unavailable"
        return True, f"neural synthesis OK ({voice}, {got} bytes)"
    except Exception as e:
        return False, (f"unreachable ({e.__class__.__name__}: {e}) - "
                       "selected neural voice will report an error")


def _check_microphone():
    try:
        import sounddevice as sd
        ins = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
        if not ins:
            return False, "no input devices found"
        return True, f"{len(ins)} input device(s)"
    except Exception as e:
        return False, f"audio system error ({e.__class__.__name__})"


def _check_config():
    try:
        from .config import Config, CONFIG_FILE
        cfg = Config()
        note = "loaded" if not cfg.load_error else f"reset ({cfg.load_error})"
        return (cfg.load_error is None), f"{note} @ {CONFIG_FILE}"
    except Exception as e:
        return False, f"config error ({e.__class__.__name__}: {e})"


CHECKS = [
    ("Python 3.10+",          True,  _check_python),
    ("faster-whisper",        True,  _check_import("faster_whisper")),
    ("PySide6 (UI)",          True,  _check_import("PySide6")),
    ("sounddevice",           True,  _check_import("sounddevice")),
    ("keyboard (hotkeys)",    True,  _check_import("keyboard")),
    ("pyperclip (clipboard)", True,  _check_import("pyperclip")),
    ("microphone",            True,  _check_microphone),
    ("settings",              True,  _check_config),
    ("CUDA runtime",          False, _check_cuda_runtime),
    ("OCR (screen reader)",   False, _check_native_ocr),
    ("VLC (neural TTS)",      False, _check_vlc),
    ("offline TTS option",    False, _check_offline_tts),
    ("edge-tts (neural TTS)", False, _check_import("edge_tts")),
]


def _check_whisper_load():
    """Deep probe: actually load the model the way the app does."""
    from .config import DEFAULTS
    from .transcriber import Transcriber

    t = Transcriber(
        model_size=DEFAULTS["whisper_model"],
        device=DEFAULTS["whisper_device"],
        compute_type=DEFAULTS["whisper_compute_type"],
    )
    if t.device == "cuda":
        t._add_nvidia_dll_dirs()
    from faster_whisper import WhisperModel
    try:
        t._model = WhisperModel(t.model_size, device=t.device, compute_type=t.compute_type)
        return True, f"loaded {t.model_size} on {t.device}"
    except Exception as e:
        try:
            t._model = WhisperModel(t.model_size, device="cpu", compute_type="int8")
            return True, f"loaded {t.model_size} on CPU (GPU failed: {e.__class__.__name__})"
        except Exception as e2:
            return False, f"model load failed: {e2}"


def run_selfcheck(deep=False):
    # ASCII-only output: this runs in plain Windows consoles (cp1252), where
    # printing a stray em-dash would raise UnicodeEncodeError — a diagnostic
    # must never crash on its own output.
    print("Voice Assistant - self-check\n" + "=" * 44)
    hard_fail = False
    for label, required, fn in CHECKS:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"probe crashed: {e.__class__.__name__}"
        if ok:
            mark = "PASS"
        elif required:
            mark = "FAIL"
            hard_fail = True
        else:
            mark = "WARN"
        tag = "" if required else " (optional)"
        print(f"  [{mark}] {label}{tag}: {detail}")

    if deep:
        print("  ...synthesizing a test phrase with the neural voice...")
        ok, detail = _check_neural_tts()
        print(f"  [{'PASS' if ok else 'WARN'}] neural TTS synthesis (optional): {detail}")

        print("  ...loading Whisper model (may download ~1GB on first run)...")
        ok, detail = _check_whisper_load()
        print(f"  [{'PASS' if ok else 'FAIL'}] Whisper model load: {detail}")
        hard_fail = hard_fail or not ok

    print("=" * 44)
    if hard_fail:
        print("RESULT: FAIL - a required component is missing (see FAIL above).")
        return 1
    print("RESULT: OK - dictation is ready. (WARN = optional feature degraded.)")
    return 0
