#!/usr/bin/env bash
# Training script: Fine-tuning a custom voice with piper1-gpl
#
# Starting point:
#   - Custom high-quality checkpoint (originally from rhasspy/piper, thorsten-high)
#   - Architecture: resblock=1, upsample_initial_channel=512, upsample_rates=[8,8,2,2]
#
# Strategy:
#   --model.vocoder_warmstart_ckpt loads only the model weights (non-strict),
#   so the epoch counter starts at 0. This is required because --ckpt_path
#   (strict loading) is incompatible with old rhasspy/piper checkpoints:
#
#   1. Architecture mismatch: rhasspy/piper uses ResBlock with two separate
#      ModuleLists (convs1 + convs2, 6 convolutions per block), while piper1-gpl
#      uses a single flat list (convs, 2 per block). This causes a state_dict
#      key mismatch that strict loading cannot resolve.
#
#   2. Stale hyperparameters: old checkpoints store trainer/config keys
#      (e.g. sample_bytes, quality, gpus, auto_lr_find) that Lightning 2.x
#      tries to parse as CLI arguments and rejects as unknown.
#
#   3. PyTorch 2.6+ defaults to weights_only=True. Old checkpoints contain
#      pathlib.PosixPath objects which fail deserialization. Fix by re-saving:
#        python3 -c "import torch; ckpt = torch.load('CKPT', map_location='cpu', weights_only=False); torch.save(ckpt, 'CKPT')"
#
# ONNX export:
#   PyTorch 2.6+ defaults to the new dynamo-based ONNX exporter, which fails on
#   VITS due to data-dependent assert statements in transforms.py (rational_quadratic_spline).
#   Patch piper1-gpl's export_onnx.py: add dynamo=False to the torch.onnx.export() call.
#
# Progress bar:
#   piper1-gpl's self.log() calls lack prog_bar=True, so loss values don't appear
#   in the terminal progress bar. Patch lightning.py: add prog_bar=True to the
#   self.log() calls for loss_g, loss_d, and val_loss.
#
set -euo pipefail

# ─── Configuration ──────────────────────────────────────────────────
PIPER1_GPL_DIR="$HOME/piper1-gpl"

if ! [ -d "${PIPER1_GPL_DIR}" ] ; then
  echo check setup_my_env.sh in howto_voice_train.md on how to set up train environment
  exit 123
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BASE_DIR="${SCRIPT_DIR}/training.local/training-piper1-gpl"

# Input
CSV_PATH="$BASE_DIR/meine-stimme/metadata_piper1.csv"
AUDIO_DIR="$BASE_DIR/meine-stimme/wavs"

# Output
CACHE_DIR="$BASE_DIR/training-cache"
OUTPUT_DIR="$BASE_DIR/meine-stimme-out"
CONFIG_PATH="$OUTPUT_DIR/de_DE-meine-stimme-high.onnx.json"

# ─── Checkpoint selection (automatic) ─────────────────────────────
#   1. If a piper1-gpl checkpoint exists in OUTPUT_DIR → resume with --ckpt_path
#   2. Otherwise → initial warmstart from rhasspy/piper checkpoint with --model.vocoder_warmstart_ckpt

# Warmstart checkpoint (rhasspy/piper). Auto-downloaded if not present.
WARMSTART_CHECKPOINT="${SCRIPT_DIR}/training.local/training-piper/meine-stimme-out/lightning_logs/version_2/checkpoints/epoch=3664-step=1210050.ckpt"

WARMSTART_FALLBACK_CKPT_URL="https://huggingface.co/datasets/rhasspy/piper-checkpoints/resolve/main/de/de_DE/thorsten/high/epoch%3D2665-step%3D1182078.ckpt"
WARMSTART_FALLBACK_CONFIG_URL="https://huggingface.co/datasets/rhasspy/piper-checkpoints/resolve/main/de/de_DE/thorsten/high/config.json"

# Auto-detect latest piper1-gpl checkpoint
LATEST_CKPT="$(find "${OUTPUT_DIR}/lightning_logs" -name '*.ckpt' -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)"
if [ -n "$LATEST_CKPT" ]; then
  CHECKPOINT="$LATEST_CKPT"
  CKPT_MODE="resume"
else
  # No piper1-gpl checkpoint yet — use warmstart from rhasspy/piper
  if [ ! -f "$WARMSTART_CHECKPOINT" ]; then
    echo "No piper1-gpl checkpoint found and '${WARMSTART_CHECKPOINT}' does not exist."
    echo "Downloading rhasspy/piper thorsten-high checkpoint + config..."
    WARMSTART_CHECKPOINT="${BASE_DIR}/thorsten-high.ckpt"
    wget -q --show-progress -O "$WARMSTART_CHECKPOINT" "$WARMSTART_FALLBACK_CKPT_URL"
    wget -q --show-progress -O "${BASE_DIR}/thorsten-high-config.json" "$WARMSTART_FALLBACK_CONFIG_URL"
  fi
  CHECKPOINT="$WARMSTART_CHECKPOINT"
  CKPT_MODE="warmstart"
fi


# Training parameters
MAX_EPOCHS=4000              # Absolute: checkpoint epoch (499) + desired additional epochs
BATCH_SIZE=32                # RTX 4090 (24 GB): 16–32 for high quality
VOICE_NAME="de_DE-meine-stimme-high"

# ─── Validation checks ──────────────────────────────────────────────
if [ ! -f "$CSV_PATH" ]; then
  echo "ERROR: $CSV_PATH not found."
  echo "Conversion (e.g.): python -m sipstuff.training.convert_metadata_rhasspypiper_to_piper1gpl meine-stimme/metadata.csv -o meine-stimme/metadata_piper1.csv"
  exit 1
fi

if [ ! -d "$AUDIO_DIR" ]; then
  echo "ERROR: $AUDIO_DIR not found."
  exit 1
fi

if [ ! -f "$CHECKPOINT" ]; then
  echo "ERROR: Checkpoint not found: $CHECKPOINT"
  exit 1
fi

if [ ! -f "$PIPER1_GPL_DIR/.venv/bin/activate" ]; then
  echo "ERROR: piper1-gpl venv not found. Run $PIPER1_GPL_DIR/setup_my_env.sh first."
  exit 1
fi

mkdir -p "$CACHE_DIR" "$OUTPUT_DIR"

# ─── Activate venv ──────────────────────────────────────────────────
source "$PIPER1_GPL_DIR/.venv/bin/activate"

echo "================================================"
echo "  Piper1-GPL Training: $VOICE_NAME"
echo "================================================"
echo ""
echo "  Checkpoint:    $CHECKPOINT"
echo "  Mode:          $CKPT_MODE"
echo "  max_epochs:    $MAX_EPOCHS"
echo "  Batch size:    $BATCH_SIZE"
echo "  CSV:           $CSV_PATH"
echo "  Audio:         $AUDIO_DIR"
echo "  Cache:         $CACHE_DIR"
echo "  Output:        $OUTPUT_DIR"
echo "  Config:        $CONFIG_PATH"
echo ""
echo "  TensorBoard:   tensorboard --logdir $OUTPUT_DIR/lightning_logs/"
echo ""
echo "================================================"
echo ""

# ─── Start training ─────────────────────────────────────────────────
python -m piper.train fit \
  --data.voice_name "$VOICE_NAME" \
  --data.csv_path "$CSV_PATH" \
  --data.audio_dir "$AUDIO_DIR" \
  --model.sample_rate 22050 \
  --data.espeak_voice de \
  --data.cache_dir "$CACHE_DIR" \
  --data.config_path "$CONFIG_PATH" \
  --data.batch_size "$BATCH_SIZE" \
  --data.num_workers 4 \
  --trainer.max_epochs "$MAX_EPOCHS" \
  --trainer.accelerator gpu \
  --trainer.devices 1 \
  --trainer.precision 32 \
  --trainer.log_every_n_steps 1 \
  --trainer.default_root_dir "$OUTPUT_DIR" \
  --model.resblock 1 \
  --model.resblock_kernel_sizes '[3, 7, 11]' \
  --model.resblock_dilation_sizes '[[1, 3, 5], [1, 3, 5], [1, 3, 5]]' \
  --model.upsample_rates '[8, 8, 2, 2]' \
  --model.upsample_initial_channel 512 \
  --model.upsample_kernel_sizes '[16, 16, 4, 4]' \
  "$(if [ "$CKPT_MODE" = "resume" ]; then echo "--ckpt_path"; else echo "--model.vocoder_warmstart_ckpt"; fi)" "$CHECKPOINT"

# ─── Next steps ──────────────────────────────────────────────────
echo ""
echo "================================================"
echo "  Training complete!"
echo "================================================"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Find the best checkpoint:"
echo "     ls -t $OUTPUT_DIR/lightning_logs/version_*/checkpoints/*.ckpt | head -1"
echo ""
echo "  2. Export to ONNX:"
echo "     source $PIPER1_GPL_DIR/.venv/bin/activate"
echo "     python -m piper.train.export_onnx \\"
echo "       --checkpoint $OUTPUT_DIR/lightning_logs/version_X/checkpoints/epoch=XXXX-step=XXXXXXX.ckpt \\"
echo "       --output-file $OUTPUT_DIR/$VOICE_NAME.onnx"
echo ""
echo "  3. Copy the config (was written during training):"
echo "     cp $CONFIG_PATH $OUTPUT_DIR/$VOICE_NAME.onnx.json"
echo ""
echo "  4. Test the model:"
echo "     echo 'Hallo, das ist meine eigene Stimme!' | \\"
echo "       piper --model $OUTPUT_DIR/$VOICE_NAME.onnx --output_file test.wav"
echo ""
echo "================================================"
