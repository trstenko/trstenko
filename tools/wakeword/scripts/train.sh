#!/usr/bin/env bash
# Trains, then quantizes and converts to the streaming model the device
# actually loads. Resumable: rerunning continues from the last checkpoint,
# restarting the configured training_steps count in the yaml.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m microwakeword.model_train_eval \
    --training_config=config/training_parameters.yaml \
    --train 1 \
    --restore_checkpoint 1 \
    --test_tf_nonstreaming 0 \
    --test_tflite_nonstreaming 0 \
    --test_tflite_nonstreaming_quantized 0 \
    --test_tflite_streaming 0 \
    --test_tflite_streaming_quantized 1 \
    --use_weights best_weights \
    mixednet \
    --pointwise_filters "64,64,64,64" \
    --repeat_in_block "1, 1, 1, 1" \
    --mixconv_kernel_sizes '[5], [7,11], [9,15], [23]' \
    --residual_connection "0,0,0,0" \
    --first_conv_filters 32 \
    --first_conv_kernel_size 5 \
    --stride 3

python3 scripts/snapshot_run.py
