#!/usr/bin/env bash
# "Trstenko", synthesized two ways: the LibriTTS-R multi-speaker generator
# (English phonemization, 904 speakers cycled for voice diversity) and the
# one Slovenian Piper voice (native pronunciation). Run inside the
# `generate` container -- see docker-compose.yml.
set -euo pipefail
cd "$(dirname "$0")/.."

WORD="${WAKE_WORD:-Trstenko}"
N_EN="${N_POSITIVE_EN:-3000}"
N_SL="${N_POSITIVE_SL:-600}"

mkdir -p data/positive_en data/positive_sl

echo "== English generator: $N_EN samples of '$WORD' =="
python3 -m piper_sample_generator "$WORD" \
    --model models/en_US-libritts_r-medium.pt \
    --max-samples "$N_EN" \
    --batch-size 100 \
    --length-scales 0.75 1.0 1.25 1.4 \
    --noise-scales 0.667 0.8 0.9 \
    --output-dir data/positive_en

echo "== Slovenian voice (sl_SI-artur): $N_SL samples of '$WORD' =="
python3 -m piper_sample_generator "$WORD" \
    --model voices/sl_SI-artur-medium.onnx \
    --max-samples "$N_SL" \
    --length-scales 0.85 1.0 1.15 1.3 \
    --noise-scales 0.667 0.8 \
    --output-dir data/positive_sl

echo "done: $(ls data/positive_en | wc -l) EN + $(ls data/positive_sl | wc -l) SL wavs"
