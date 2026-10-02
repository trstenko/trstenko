"""Building the assistant for a device, in one place.

The meter, the tools, the worker, the audio thread, the speech thread and the
pipeline need the device's clocks, its inbox, its settings and its speaker, and
the device needs them back. This is the one function that joins them, so `main.py`
on the robot and the emulator's rigs build exactly the same thing, in the same
way `scheduler/wiring.py` does for reminders.

**It must never stop the device booting.** Without a key, or a voice, or a wake
word model, the device still has a face, a mute switch and reminders. Each missing
piece is a status the page can show ("no voice installed", "wake word model
missing"), not an exception, and the pieces that are there still work: a device
with no wake word model still answers the button in the emulator, and one with no
voice still shows what it would have said in the log.

**What is supervised.** The assistant worker, the audio thread and the speech
thread each post `WorkerDied` from a `finally` if they stop, and the conductor's
supervisor restarts them after 1s, 5s and 30s, then leaves one off and says so
(`firmware/supervisor.py`).

**What is attached to the device:**

- the pipeline, as `device.pipeline`, with handlers for every event the other
  threads post;
- the commands the page will need: `usage.summary`, `assistant.status`,
  `memories.*`, and the volume and announcement commands the tools use;
- a status block, `device.summary()["assistant"]`;
- two chores in the scheduler's list, to write usage and to roll it up;
- a settings listener, so a changed voice, language, limit or price takes effect
  without a restart;
- a closer that runs *before* the database closes, so the last usage is written.
"""

import logging
import os
import threading

from .. import events as ev
from . import lang as lang_mod
from . import memories as memories_mod
from . import meter as meter_mod
from . import session as session_mod
from . import stt, table, tools, tts as tts_mod, voices as voices_mod
from . import audio as audio_mod
from . import vad as vad_mod
from .pipeline import AssistantPipeline
from .worker import Worker

log = logging.getLogger("trstenko.assistant")

# Plan 06: how sure the wake word has to be, by the owner's setting. The model
# was trained with a cutoff of 0.99, which is "normal".
# TODO tune on the board with the real room and the real speaker (plan 08).
WAKE_CUTOFFS = {"low": 0.995, "normal": None, "high": 0.95}

WAKE_MODEL = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "wakeword", "models", "trstenko.json")


class NeverDetector:
    """A wake word detector that never fires."""

    def process(self, chunk):
        return False

    def close(self):
        pass


class Assistant:
    """Everything `wire_assistant` built, and what the page asks about it."""

    def __init__(self):
        self.device = None
        self.pipeline = None
        self.worker = None
        self.audio = None
        self.tts = None
        self.meter = None
        self.memories = None
        self.session = None
        self.vad = None
        self.voice_note = ""
        self.audio_note = ""
        self.tables_note = ""

    def status(self):
        device = self.device
        settings = device.settings.data["assistant"]
        lang = device.settings.data["owner"]["language"]
        plan = (stt.plan(self.providers, settings["provider"],
                         self._has_key, lang, settings["stt_provider"])
                if self.providers is not None else None)
        alerts = []
        if self.tables_note:
            alerts.append({"kind": "tables", "detail": self.tables_note})
        if self.providers is not None and not self._has_key(
                settings["provider"]):
            alerts.append({"kind": "no_key", "provider": settings["provider"]})
        if plan is not None and not plan.ok:
            alerts.append({"kind": "no_stt", "reason": plan.reason})
        if self.voice_note:
            alerts.append({"kind": "no_voice", "detail": self.voice_note})
        if self.audio_note:
            alerts.append({"kind": "no_wake_word", "detail": self.audio_note})
        return {
            "ready": not alerts,
            "provider": settings["provider"],
            "stt": plan.mode if plan else None,
            "wake": self.audio.state if self.audio else None,
            "vad": self.vad.name if self.vad else None,
            "voice": self.tts.status() if self.tts else None,
            "turns": self.worker.turns if self.worker else 0,
            "last_error": self.worker.last_error if self.worker else "",
            "false_wakes": self.audio.false_wakes if self.audio else 0,
            "alerts": alerts,
            "banners": self.meter.summary()["banners"] if self.meter else [],
        }

    def _has_key(self, name):
        row = self.providers.get(name)
        secrets = self.device.secrets
        return (not row.needs_key) or (secrets is not None
                                       and secrets.has_key(name))

    providers = None

    def status_off(self):
        """What the page sees when the tables would not load."""
        return {"ready": False, "alerts": [{"kind": "tables",
                                            "detail": self.tables_note}]}

    def keys_changed(self, provider=None):
        """The owner set or removed a key: forget what was learned from the old
        one (a banner, a cooldown) and try again at once."""
        self.meter.clear(provider)

    def close(self):
        for part in (self.audio, self.worker, self.tts):
            if part is not None:
                try:
                    part.close()
                except Exception:
                    log.exception("closing %s failed", type(part).__name__)
        try:
            self.meter.flush(self.device.db)
        except Exception:
            log.exception("the last usage could not be written")


def _open_detector(cutoff_name):
    from ..wakeword.base import open_detector

    cutoff = WAKE_CUTOFFS.get(cutoff_name)
    config = {"engine": "micro_wakeword", "model_path": WAKE_MODEL}
    if cutoff is not None:
        config["probability_cutoff"] = cutoff
    return open_detector(config)


def wire_assistant(device, hal, data_root, synth=None, detector=None,
                   audio=None, transport=None, start_audio=True,
                   user_agent="Trstenko", providers=None):
    """Attach the assistant to `device`, which needs its database, settings,
    secrets and scheduler in place. `synth`, `detector`, `audio` and
    `providers` are for tests and the emulator, which supply their own."""
    if device.db is None or device.settings is None:
        raise ValueError("the assistant needs the database and the settings")
    assistant = Assistant()
    assistant.device = device
    settings = device.settings

    try:
        providers = providers or table.load_providers()
        prices = table.load_prices(overrides=settings.data["prices_override"])
    except table.TableError as exc:
        # A shipped table that does not load is a release bug, found by CI
        # before it gets here. If it happens anyway the device still boots.
        assistant.tables_note = str(exc)
        log.error("the assistant is off: %s", exc)
        device.status_extra["assistant"] = assistant.status_off
        return assistant
    assistant.providers = providers

    registry = tools.build()
    store = memories_mod.MemoryStore(device.db, wall=device.wall)
    tools.memory.attach(device, store)
    session = session_mod.Session(device.mono)
    confirmations = tools.registry.Confirmations(device.mono)
    meter = meter_mod.Meter(prices, meter_mod.limits_from_settings(settings),
                            device.timezone, device.mono, device.wall,
                            providers=providers)
    meter.load(device.db)
    if device.scheduler is not None:
        meter.register_chores(device.scheduler.housekeeping, device.db)
    else:
        meter.rollup(device.db)
    assistant.meter, assistant.memories, assistant.session = (meter, store,
                                                              session)

    def send(name, args=None):
        cmd = ev.Command(name, args)
        device.events.post(cmd)
        return cmd.wait()

    def synced():
        return device.scheduler.synced if device.scheduler is not None \
            else False

    worker = Worker(
        settings, device.secrets, providers, meter, registry, store, session,
        confirmations, device.events.post, send, device.wall, device.mono,
        synced, device.timezone, db=device.db, transport=transport,
        user_agent=user_agent)
    assistant.worker = worker

    # ---- the voice ----
    folder = voices_mod.voices_dir(data_root)
    registry_v = voices_mod.VoiceRegistry.load()

    def choose_voice():
        data = settings.data
        return registry_v.choose(data["voice"]["piper_voice"],
                                 data["owner"]["language"], folder)

    if synth is None:
        synth = tts_mod.PiperSynthesizer(hal.speaker, device.events.post)
    voice, note = choose_voice()
    assistant.tts = synth
    if voice is not None:
        synth.configure(voice, folder)
        assistant.voice_note = ""
    else:
        assistant.voice_note = note or "no voice is installed"
        log.warning("no voice: %s (run python -m tools.voices.setup)",
                    assistant.voice_note)
    if device.announcer is not None:
        device.announcer.set_say(synth.say_line)

    # ---- the ear ----
    assistant.vad = vad_mod.open_vad(settings.data["audio"]["vad_cutoff"])
    if audio is None:
        if detector is None:
            try:
                detector = _open_detector(
                    settings.data["audio"]["wake_sensitivity"])
            except Exception as exc:
                assistant.audio_note = ("the wake word could not be loaded: "
                                        "%s" % exc)
                log.warning(assistant.audio_note)
        if detector is None:
            # No wake word, but the microphone and the recording still work:
            # a turn started by a button (the emulator's, or a test's) has to
            # be heard. Without this the turn waited for audio nobody read.
            detector = NeverDetector()
        if detector is not None:
            audio = audio_mod.AudioIn(
                hal.mic, device.events.post, detector, assistant.vad,
                mono=device.mono,
                speaking=lambda: hal.speaker.is_playing(),
                barge_in=lambda: bool(settings.data["voice"]["barge_in"]))
    assistant.audio = audio

    if audio is None:
        class Deaf:
            """No wake word, so no recordings; `trigger` still works."""
            state = "off"
            false_wakes = 0

            def begin_capture(self, *a, **k):
                pass

            def abort(self):
                pass

            def close(self):
                pass
        audio = Deaf()
        assistant.audio = audio

    pipeline = AssistantPipeline(hal, audio, worker, synth, device.mono)
    pipeline.attach(device)
    device.pipeline = pipeline
    assistant.pipeline = pipeline

    # ---- keeping them alive ----
    now = device.mono()
    device.supervisor.add("assistant", worker.start, now)
    if voice is not None:
        device.supervisor.add("tts", synth.start, now)
    if start_audio and audio.state != "off":
        device.supervisor.add("audio", audio.start, now)

    # ---- the page and the other threads ----
    device.command("usage.summary", lambda args: meter.summary())
    device.command("assistant.status", lambda args: assistant.status())
    device.status_extra["assistant"] = assistant.status

    def on_settings(changed):
        names = set(changed)
        if names & {"voice.piper_voice", "owner.language"}:
            new, why = choose_voice()
            if new is not None and (synth.voice is None
                                    or new.id != synth.voice.id):
                if synth.state in ("stopped", "down"):
                    synth.configure(new, folder)
                else:
                    synth.set_voice(new, folder)
            assistant.voice_note = "" if new is not None else why
        if "owner.language" in names:
            session.reset()
        if "prices_override" in names:
            meter.set_prices(table.load_prices(
                overrides=settings.data["prices_override"]))
        if any(n.startswith(("assistant.", "budget.", "prices_override"))
               for n in names):
            meter.clear()
        if "audio.vad_cutoff" in names:
            assistant.vad.set_cutoff(settings.data["audio"]["vad_cutoff"])

    device.on_settings(on_settings)
    device.add_closer("assistant", assistant.close, first=True)
    return assistant

