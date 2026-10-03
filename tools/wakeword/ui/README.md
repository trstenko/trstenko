# Training dashboard

    python -m tools.wakeword.ui

from the `code/` directory (needs Flask, already in `tools/requirements.txt`
via `firmware/requirements.txt`, plus `cryptography` for the self-signed
cert below). Opens at <https://127.0.0.1:5151> -- it's https now, self-signed,
so the PC browser will warn once; click through it.

Runs on the host, not in Docker. Every button is a `docker compose run`
call into `tools/wakeword/` -- the same commands `README.md` documents by
hand, just clicked instead of typed. It does not replace understanding those
commands; read `TRAINING_GUIDE.md` for what the numbers mean before trusting
a run it produces.

What it does:

- Dataset tile counts per category (`data/<category>/*.wav`).
- Six buttons running the pipeline in order: generate positive, generate
  negative speech, generate negative noise, build features, train, export.
  One job at a time -- the second button press while one is running gets an
  error instead of a second concurrent `docker compose run`.
- Training's `positive_class_weight` / `negative_class_weight` /
  `training_steps` fields write straight into
  `config/training_parameters.yaml` before launching (regex substitution of
  those three lines, not a full YAML rewrite -- the file's comments survive).
- A live log tail of whatever's currently running, or the last job's output.
- Run history, read from `data/runs/<timestamp>/summary.json`
  (`scripts/snapshot_run.py`, called automatically at the end of
  `train.sh`): whether the ambient (faph) eval actually ran, and the
  cutoff/frr/faph it would recommend.
- **Recording from a phone** (`positive_real` / `negative_real`, the "record
  real team sayings and real room ambience" gap `README.md`'s "Honest gaps"
  section names). The dashboard's "Record from phone" section shows a
  `https://<lan-ip>:5151/record` link and a one-time token -- open that link
  on a phone on the same wifi, paste the token in once (it's remembered),
  pick "Say Trstenko" or "Ambient", and tap to record. Recordings never
  save themselves: each upload lands in a pending queue, playable and
  Confirm/Deny-able from this same dashboard page, and only a Confirm moves
  the file into `data/positive_real/` or `data/negative_real/` as
  `real_NNNN.wav`. Deny just deletes it.
  - https, not http, because phone browsers refuse microphone access to
    any origin that isn't `localhost` unless it's a secure context --
    Flask's `ssl_context="adhoc"` self-signs a cert for this on every
    startup, so the phone (and the PC, at `127.0.0.1`) will see a
    certificate warning once per browser; that's expected, not a sign
    anything's wrong.
  - The token gates every state-changing route once the server binds
    `0.0.0.0` (needed for the phone to reach it at all) -- including the
    training buttons above, not just recording. Requests from `127.0.0.1`
    (the dashboard opened on the PC itself) skip the check; anything
    arriving from elsewhere on the wifi needs the token. Restarting the
    dashboard rotates the token.
  - The phone-side recorder captures raw PCM via the Web Audio API and
    encodes 16kHz mono WAV in-browser -- deliberately not `MediaRecorder`
    (which would hand back compressed webm/opus or mp4/aac, needing a
    server-side decode step and, on this machine, `ffmpeg`, which isn't
    installed). The server still validates every upload is actually mono
    16kHz 16-bit before accepting it.
  - Confirming clips is not the last step: `build_features.py` auto-detects
    `data/positive_real/` and `data/negative_real/` once they're non-empty,
    but `config/training_parameters.yaml` still needs its commented-out
    `positive_real`/`negative_real` `features:` blocks uncommented once
    before the next training run picks them up -- see that file's own
    comment.

What it deliberately doesn't do:

- No chart -- the run-history table is the sweep's recommended point, not
  the full curve. Read `data/runs/<timestamp>/tflite_streaming_roc.txt`
  directly (or the copy the run detail keeps) for the whole thing.
- No accounts, no HTTPS from a real CA -- the token is a shared secret for
  "my own phone, same wifi," not a login system. Don't run this on a
  network you don't trust.
- No cancel button. `docker compose run` containers are named
  `wakeword-<service>-run-*`; `docker stop`/`docker rm` them by hand if a
  run needs killing, same as before this UI existed.
