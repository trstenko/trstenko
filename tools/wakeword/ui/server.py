"""A local dashboard for the wake-word training pipeline.

    python -m tools.wakeword.ui

Runs on the host, not in Docker -- it shells out to the same
`docker compose run` commands documented in README.md and
TRAINING_GUIDE.md, it doesn't reimplement them. One job at a time; the lock
below is the whole concurrency story, which is all a single developer at a
single machine needs.

Dev tool only, like everything else under tools/. Never imported by
firmware/.
"""

import csv
import datetime
import functools
import glob
import io
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import wave
import zipfile

WAKEWORD_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(WAKEWORD_DIR, "config", "training_parameters.yaml")


def _data_dir():
    """Same variable docker-compose.yml uses, read the same way `docker
    compose` itself would (a .env file next to docker-compose.yml) -- this
    process isn't launched through `docker compose`, so nothing loads that
    file for it automatically. Falls back to ./data, the default before a
    machine needed to move it (see .env's own comment for why one might)."""
    env_path = os.path.join(WAKEWORD_DIR, ".env")
    if os.path.isfile(env_path):
        with open(env_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("WAKEWORD_DATA_DIR="):
                    return line.split("=", 1)[1].strip()
    return os.path.join(WAKEWORD_DIR, "data")


DATA_DIR = _data_dir()
RUNS_DIR = os.path.join(DATA_DIR, "runs")
JOB_LOG_DIR = os.path.join(DATA_DIR, "ui_jobs")
PENDING_DIR = os.path.join(DATA_DIR, "recording_uploads", "pending")

DATASET_CATEGORIES = [
    "positive_en", "positive_sl", "positive_real",
    "negative_speech_en", "negative_speech_sl",
    "negative_noise", "negative_real",
]

# One shared secret for the whole process, so a phone on the same wifi can
# authenticate without a login system. Requests from this machine itself
# (the dashboard, opened at 127.0.0.1) skip the check entirely -- see
# `_is_local`/`require_token` below.
APP_TOKEN = secrets.token_hex(4)

REAL_CATEGORIES = {"positive_real", "negative_real"}


def _lan_ip():
    """Best-effort LAN-facing address to hand a phone. The UDP connect below
    never actually sends a packet -- it just asks the OS which local
    interface would route to the internet -- so this makes no network call
    of its own."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _is_local(remote_addr):
    return remote_addr in ("127.0.0.1", "::1")


def require_token(view):
    """Gate a route to this machine, or to a caller that knows APP_TOKEN.
    Exists because the dashboard binds 0.0.0.0 so a phone can reach
    /record -- everything else on that surface (docker actions, recorded
    audio) needs to not be open to the whole wifi."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        from flask import jsonify, request
        if _is_local(request.remote_addr):
            return view(*args, **kwargs)
        supplied = (
            request.form.get("token")
            or request.headers.get("X-Record-Token")
            or (request.get_json(silent=True) or {}).get("token")
        )
        if supplied != APP_TOKEN:
            return jsonify({"error": "bad or missing token"}), 403
        return view(*args, **kwargs)
    return wrapped

ACTIONS = {
    "generate_positive": {
        "label": "Generate positive samples",
        "cmd": ["docker", "compose", "run", "--rm", "generate",
                "bash", "scripts/generate_positive.sh"],
    },
    "generate_negative_speech": {
        "label": "Generate negative speech",
        "cmd": ["docker", "compose", "run", "--rm", "generate",
                "bash", "scripts/generate_negative_speech.sh"],
    },
    "generate_negative_noise": {
        "label": "Generate negative noise",
        "cmd": ["docker", "compose", "run", "--rm", "train",
                "python", "scripts/generate_negative_noise.py"],
    },
    "build_features": {
        "label": "Build features",
        "cmd": ["docker", "compose", "run", "--rm", "train",
                "python", "scripts/build_features.py"],
    },
    "train": {
        "label": "Train",
        "cmd": ["docker", "compose", "run", "--rm", "train",
                "bash", "scripts/train.sh"],
    },
}


class JobRunner:
    """Runs at most one subprocess at a time, logging it to a file the UI
    polls. A dev-tool-grade lock: a thread + a bool, nothing fancier."""

    def __init__(self):
        self._lock = threading.Lock()
        self.action = None
        self.started_at = None
        self.proc = None
        self.log_path = os.path.join(JOB_LOG_DIR, "current.log")
        self.returncode = None
        os.makedirs(JOB_LOG_DIR, exist_ok=True)

    def status(self):
        return {
            "running": self.proc is not None and self.proc.poll() is None,
            "action": self.action,
            "started_at": self.started_at,
            "returncode": self.returncode,
        }

    def start(self, action, cmd, env_overrides=None):
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                raise RuntimeError("a job is already running: %s" % self.action)

            self.action = action
            self.started_at = time.time()
            self.returncode = None

            env = dict(os.environ)
            if env_overrides:
                env.update(env_overrides)

            log_file = open(self.log_path, "w", encoding="utf-8", errors="replace")
            self.proc = subprocess.Popen(
                cmd, cwd=WAKEWORD_DIR, stdout=log_file, stderr=subprocess.STDOUT,
                env=env)

            def _wait():
                proc = self.proc
                proc.wait()
                self.returncode = proc.returncode
                log_file.close()
                archive = os.path.join(
                    JOB_LOG_DIR,
                    "%s-%s.log" % (int(self.started_at), action))
                try:
                    with open(self.log_path, "r", encoding="utf-8", errors="replace") as src:
                        contents = src.read()
                    with open(archive, "w", encoding="utf-8") as dst:
                        dst.write(contents)
                except OSError:
                    pass

            threading.Thread(target=_wait, daemon=True).start()

    def tail(self, max_bytes=20000):
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - max_bytes))
                return fh.read()
        except OSError:
            return ""


JOBS = JobRunner()


def dataset_counts():
    counts = {}
    for name in DATASET_CATEGORIES:
        path = os.path.join(DATA_DIR, name)
        if os.path.isdir(path):
            counts[name] = len([f for f in os.listdir(path) if f.endswith(".wav")])
        else:
            counts[name] = 0
    return counts


def docker_images_present():
    try:
        out = subprocess.check_output(
            ["docker", "images", "--format", "{{.Repository}}"],
            cwd=WAKEWORD_DIR, text=True, timeout=15)
    except (subprocess.SubprocessError, OSError):
        return {"generate": False, "train": False}
    return {
        "generate": "wakeword-generate" in out,
        "train": "wakeword-train" in out,
    }


def list_runs():
    if not os.path.isdir(RUNS_DIR):
        return []
    runs = []
    for name in sorted(os.listdir(RUNS_DIR), reverse=True):
        summary_path = os.path.join(RUNS_DIR, name, "summary.json")
        if not os.path.isfile(summary_path):
            continue
        try:
            with open(summary_path, "r", encoding="utf-8") as fh:
                summary = json.load(fh)
        except (OSError, ValueError):
            continue
        summary["timestamp"] = name
        runs.append(summary)
    return runs


def run_detail(timestamp):
    safe = re.sub(r"[^0-9A-Za-z_-]", "", timestamp)
    run_dir = os.path.join(RUNS_DIR, safe)
    summary_path = os.path.join(run_dir, "summary.json")
    if not os.path.isfile(summary_path):
        return None
    with open(summary_path, "r", encoding="utf-8") as fh:
        summary = json.load(fh)
    config_path = os.path.join(run_dir, "training_parameters.yaml")
    config_text = ""
    if os.path.isfile(config_path):
        with open(config_path, "r", encoding="utf-8") as fh:
            config_text = fh.read()
    summary["config"] = config_text
    return summary


def list_pending():
    if not os.path.isdir(PENDING_DIR):
        return []
    clips = []
    for name in os.listdir(PENDING_DIR):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(PENDING_DIR, name), "r", encoding="utf-8") as fh:
            clips.append(json.load(fh))
    clips.sort(key=lambda c: c["created_at"], reverse=True)
    return clips


def _safe_clip_id(clip_id):
    return re.sub(r"[^0-9a-f]", "", clip_id)


def save_pending_upload(category, label, audio_file):
    if category not in REAL_CATEGORIES:
        raise ValueError("category must be one of %s" % sorted(REAL_CATEGORIES))

    os.makedirs(PENDING_DIR, exist_ok=True)
    clip_id = secrets.token_hex(16)
    wav_path = os.path.join(PENDING_DIR, clip_id + ".wav")
    audio_file.save(wav_path)

    try:
        with wave.open(wav_path, "rb") as w:
            if w.getnchannels() != 1 or w.getframerate() != 16000 or w.getsampwidth() != 2:
                raise ValueError(
                    "expected mono 16kHz 16-bit WAV, got %d ch / %d Hz / %d-bit -- "
                    "the phone page should never send anything else, so this "
                    "means the upload was corrupted in transit"
                    % (w.getnchannels(), w.getframerate(), w.getsampwidth() * 8))
            duration = w.getnframes() / float(w.getframerate())
    except (wave.Error, ValueError):
        os.remove(wav_path)
        raise

    meta = {
        "id": clip_id,
        "category": category,
        "label": (label or "")[:80],
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "duration": round(duration, 2),
    }
    with open(os.path.join(PENDING_DIR, clip_id + ".json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh)
    return meta


def _next_sequence_name(target_dir):
    existing = os.listdir(target_dir) if os.path.isdir(target_dir) else []
    nums = [int(m.group(1)) for m in
            (re.match(r"real_(\d+)\.wav$", f) for f in existing) if m]
    return "real_%04d.wav" % (max(nums, default=0) + 1)


def confirm_pending(clip_id):
    safe = _safe_clip_id(clip_id)
    meta_path = os.path.join(PENDING_DIR, safe + ".json")
    wav_path = os.path.join(PENDING_DIR, safe + ".wav")
    if not os.path.isfile(meta_path) or not os.path.isfile(wav_path):
        return None
    with open(meta_path, "r", encoding="utf-8") as fh:
        meta = json.load(fh)
    target_dir = os.path.join(DATA_DIR, meta["category"])
    os.makedirs(target_dir, exist_ok=True)
    dest_name = _next_sequence_name(target_dir)
    os.rename(wav_path, os.path.join(target_dir, dest_name))
    os.remove(meta_path)
    return {"saved_as": dest_name, "category": meta["category"]}


def deny_pending(clip_id):
    safe = _safe_clip_id(clip_id)
    found = False
    for ext in (".wav", ".json"):
        p = os.path.join(PENDING_DIR, safe + ext)
        if os.path.isfile(p):
            os.remove(p)
            found = True
    return found


# voice-collector's admin export zips `<category>/<id>.wav`, where category
# is "positive"/"negative" -- these are the real-recording dirs that maps to.
ZIP_CATEGORY_MAP = {"positive": "positive_real", "negative": "negative_real"}


def import_recordings_zip(file_storage):
    """Unpacks a voice-collector export directly into data/positive_real
    and data/negative_real, renaming each clip into the same real_NNNN.wav
    sequence `confirm_pending` uses so both sources of real recordings don't
    collide. Skips (and reports) anything that isn't a mono/16kHz/16-bit WAV
    under a recognized top-level folder, rather than failing the whole
    import over one bad file."""
    added = {name: 0 for name in ZIP_CATEGORY_MAP.values()}
    skipped = []
    imported = []   # (recording_id, local_path, contributor_id) for import map
    with zipfile.ZipFile(file_storage) as zf:
        # manifest.csv (recording_id, category, contributor_id, ...) comes
        # with newer voice-collector exports. It isn't a clip, so it's read
        # rather than reported as skipped -- and used below to remember which
        # local file came from which recording/contributor, so a later
        # consent withdrawal can be honoured on the local training copy.
        manifest = {}
        if "manifest.csv" in zf.namelist():
            with zf.open("manifest.csv") as fh:
                for row in csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8")):
                    manifest[row.get("recording_id")] = row
        for info in zf.infolist():
            if info.is_dir() or info.filename == "manifest.csv":
                continue
            parts = info.filename.split("/")
            if len(parts) != 2 or not parts[1].lower().endswith(".wav"):
                skipped.append(info.filename)
                continue
            target_category = ZIP_CATEGORY_MAP.get(parts[0])
            if target_category is None:
                skipped.append(info.filename)
                continue

            target_dir = os.path.join(DATA_DIR, target_category)
            os.makedirs(target_dir, exist_ok=True)
            dest_path = os.path.join(target_dir, _next_sequence_name(target_dir))
            with zf.open(info) as src, open(dest_path, "wb") as dst:
                dst.write(src.read())

            try:
                with wave.open(dest_path, "rb") as w:
                    valid = (w.getnchannels() == 1 and w.getframerate() == 16000
                              and w.getsampwidth() == 2)
            except wave.Error:
                valid = False
            if not valid:
                os.remove(dest_path)
                skipped.append(info.filename)
                continue

            added[target_category] += 1
            rec_id = os.path.splitext(parts[1])[0]
            imported.append((
                rec_id,
                os.path.relpath(dest_path, DATA_DIR),
                (manifest.get(rec_id) or {}).get("contributor_id", ""),
            ))

    if imported:
        map_path = os.path.join(DATA_DIR, "real_import_map.csv")
        is_new = not os.path.isfile(map_path)
        with open(map_path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if is_new:
                writer.writerow(["recording_id", "local_file", "contributor_id"])
            writer.writerows(imported)
    return added, skipped


def set_class_weights(positive_weight, negative_weight, training_steps):
    """Regex-substitutes the three scalar-list fields in the yaml in place.
    Not a full YAML round-trip on purpose: this file is hand-maintained and
    commented, and a parse-then-dump would throw the comments away."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        text = fh.read()

    def _sub(field, value, text):
        pattern = re.compile(r"^%s:\s*\[.*\]" % re.escape(field), re.MULTILINE)
        replacement = "%s: [%s]" % (field, value)
        new_text, count = pattern.subn(replacement, text, count=1)
        if count == 0:
            raise ValueError("could not find %r in training_parameters.yaml" % field)
        return new_text

    text = _sub("positive_class_weight", positive_weight, text)
    text = _sub("negative_class_weight", negative_weight, text)
    text = _sub("training_steps", training_steps, text)

    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        fh.write(text)


def create_app():
    from flask import Flask, jsonify, request, send_from_directory

    app = Flask(__name__, static_folder=None)
    static_dir = os.path.join(os.path.dirname(__file__), "static")

    @app.get("/")
    def index():
        return send_from_directory(static_dir, "index.html")

    @app.get("/record")
    def record_page():
        return send_from_directory(static_dir, "record.html")

    @app.get("/api/record/info")
    def record_info():
        # Reveals APP_TOKEN, so this itself must not be reachable from the
        # LAN -- there'd be no point gating it behind the token it hands out.
        if not _is_local(request.remote_addr):
            return jsonify({"error": "forbidden"}), 403
        return jsonify({
            "lan_url": "https://%s:5151/record" % _lan_ip(),
            "token": APP_TOKEN,
        })

    @app.get("/api/record/pending")
    @require_token
    def record_pending_list():
        return jsonify(list_pending())

    @app.get("/api/record/pending/<clip_id>/audio")
    @require_token
    def record_pending_audio(clip_id):
        safe = _safe_clip_id(clip_id)
        return send_from_directory(PENDING_DIR, safe + ".wav", mimetype="audio/wav")

    @app.post("/api/record/upload")
    @require_token
    def record_upload():
        category = request.form.get("category", "")
        audio = request.files.get("audio")
        if audio is None:
            return jsonify({"error": "missing audio file"}), 400
        try:
            meta = save_pending_upload(category, request.form.get("label"), audio)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(meta)

    @app.post("/api/record/pending/<clip_id>/confirm")
    @require_token
    def record_confirm(clip_id):
        result = confirm_pending(clip_id)
        if result is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(result)

    @app.post("/api/record/pending/<clip_id>/deny")
    @require_token
    def record_deny_route(clip_id):
        if not deny_pending(clip_id):
            return jsonify({"error": "not found"}), 404
        return jsonify({"deleted": True})

    @app.post("/api/import-zip")
    @require_token
    def import_zip_route():
        upload = request.files.get("zip")
        if upload is None:
            return jsonify({"error": "missing zip file"}), 400
        try:
            added, skipped = import_recordings_zip(upload)
        except zipfile.BadZipFile:
            return jsonify({"error": "not a valid zip file"}), 400
        return jsonify({"added": added, "skipped": skipped})

    @app.get("/api/status")
    def status():
        return jsonify({
            "datasets": dataset_counts(),
            "images": docker_images_present(),
            "job": JOBS.status(),
        })

    @app.get("/api/log")
    def log():
        return {"text": JOBS.tail()}

    @app.get("/api/runs")
    def runs():
        return jsonify(list_runs())

    @app.get("/api/runs/<timestamp>")
    def run_detail_route(timestamp):
        detail = run_detail(timestamp)
        if detail is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(detail)

    @app.post("/api/actions/<name>")
    @require_token
    def run_action(name):
        if name not in ACTIONS:
            return jsonify({"error": "unknown action %r" % name}), 400

        body = request.get_json(silent=True) or {}
        if name == "train":
            try:
                set_class_weights(
                    body.get("positive_class_weight", 1),
                    body.get("negative_class_weight", 8),
                    body.get("training_steps", 10000),
                )
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400

        try:
            JOBS.start(name, ACTIONS[name]["cmd"])
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"started": name})

    @app.post("/api/actions/export")
    @require_token
    def run_export():
        body = request.get_json(silent=True) or {}
        cutoff = str(body.get("probability_cutoff", 0.5))
        cmd = ["docker", "compose", "run", "--rm",
               "-e", "PROBABILITY_CUTOFF=%s" % cutoff,
               "train", "python", "scripts/export_model_manifest.py"]
        try:
            JOBS.start("export", cmd)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"started": "export"})

    @app.get("/api/config")
    def get_config():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                return {"text": fh.read()}
        except OSError:
            return {"text": ""}

    return app


def main():
    app = create_app()
    print("wake-word training dashboard: https://127.0.0.1:5151")
    print("phone recording page (same wifi): https://%s:5151/record" % _lan_ip())
    print("recording token: %s" % APP_TOKEN)
    print(
        "(https is self-signed -- phones need it to grant microphone access "
        "to anything that isn't localhost, so accept the browser warning "
        "once per device.)"
    )
    # 0.0.0.0 so a phone on the same wifi can reach /record; require_token
    # gates everything else that binding would otherwise expose.
    app.run(host="0.0.0.0", port=5151, debug=False, ssl_context="adhoc")


if __name__ == "__main__":
    main()
