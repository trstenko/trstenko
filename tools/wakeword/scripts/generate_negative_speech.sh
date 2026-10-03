#!/usr/bin/env bash
# Ordinary sentences and near-miss phonetic neighbours of "Trstenko", so the
# model learns those aren't a match instead of learning "any voice fires it."
set -euo pipefail
cd "$(dirname "$0")/.."

N_EN="${N_NEGATIVE_SPEECH_EN:-2500}"
N_SL="${N_NEGATIVE_SPEECH_SL:-500}"

mkdir -p data/negative_speech_en data/negative_speech_sl

echo "== English negative speech: $N_EN samples =="
python3 -m piper_sample_generator scripts/wordlists/negative_en.txt \
    --model models/en_US-libritts_r-medium.pt \
    --max-samples "$N_EN" \
    --batch-size 100 \
    --length-scales 0.8 1.0 1.2 \
    --noise-scales 0.667 0.8 \
    --output-dir data/negative_speech_en

echo "== Slovenian negative speech: $N_SL samples =="
python3 -m piper_sample_generator scripts/wordlists/negative_sl.txt \
    --model voices/sl_SI-artur-medium.onnx \
    --max-samples "$N_SL" \
    --length-scales 0.85 1.0 1.15 \
    --noise-scales 0.667 0.8 \
    --output-dir data/negative_speech_sl

echo "done: $(ls data/negative_speech_en | wc -l) EN + $(ls data/negative_speech_sl | wc -l) SL wavs"
