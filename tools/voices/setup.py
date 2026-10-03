"""Install Piper and Trstenko's voices on a development PC, outside OneDrive.

    python -m tools.voices.setup              # Piper and the English voice
    python -m tools.voices.setup sl_SI-artur-medium

What it makes, under `%LOCALAPPDATA%\\Trstenko` (or `~/.local/share/trstenko`):

    piper-venv/    a virtual environment with piper-tts, pinned
    voices/        the voice files, which are tens of megabytes each

Why not in the project folder: the project lives in OneDrive, which would sync a
165MB virtual environment and, as the wake-word training data showed, can crash
Explorer doing it. And why a separate environment at all: the firmware talks to
Piper through a pipe (`firmware/assistant/tts.py`), so Piper can have its own
Python and its own pinned onnxruntime, whatever Python the firmware runs on.

Set `TRSTENKO_PIPER` to the environment's python, and `TRSTENKO_VOICES` to the
voices folder, if you put them somewhere else. The emulator finds the defaults.

Voices are fetched from the Piper project's voice index. Only voices in
`firmware/assistant/voices.json` are accepted, because that file is where their
licences are recorded, and a voice with a research-only licence must never be
installed by accident. The checksum of each file is printed so a change is
noticed.
"""

import hashlib
import os
import subprocess
import sys

PIPER = "piper-tts==1.8.0"


def base_dir():
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(root, "Trstenko")
    return os.path.join(os.path.expanduser("~"), ".local", "share",
                        "trstenko")


def venv_python(base):
    sub = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    return os.path.join(base, "piper-venv", *sub)


def main(argv=None):
    from firmware.assistant import voices

    wanted = (argv if argv is not None else sys.argv[1:]) or [
        "en_US-ljspeech-medium"]
    registry = voices.VoiceRegistry.load()
    for name in wanted:
        if registry.get(name) is None:
            print("%s is not in firmware/assistant/voices.json. Add it there, "
                  "with its licence, first." % name)
            return 2

    base = base_dir()
    os.makedirs(base, exist_ok=True)
    python = venv_python(base)
    if not os.path.exists(python):
        print("creating %s" % os.path.join(base, "piper-venv"))
        subprocess.check_call([sys.executable, "-m", "venv",
                               os.path.join(base, "piper-venv")])
    subprocess.check_call([python, "-m", "pip", "install", "--quiet", PIPER])

    folder = os.environ.get("TRSTENKO_VOICES") or os.path.join(base, "voices")
    os.makedirs(folder, exist_ok=True)
    for name in wanted:
        subprocess.check_call([python, "-m", "piper.download_voices", name,
                               "--download-dir", folder])
        for path in registry.get(name).files(folder):
            digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
            print("%s  %s" % (digest, os.path.basename(path)))
    print("\nPiper:  %s\nVoices: %s" % (python, folder))
    return 0


if __name__ == "__main__":
    sys.exit(main())
