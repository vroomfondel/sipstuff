#!/usr/bin/env bash
# Training script: Fine-tuning a custom voice with piper1-gpl
#
# Starting point:
#   - Custom high-quality checkpoint (originally from rhasspy/piper, thorsten-high)
#   - Architecture: resblock=1, upsample_initial_channel=512, upsample_rates=[8,8,2,2]
#
# Strategy:
#   --ckpt_path loads the checkpoint directly (same high-quality architecture).
#   The epoch counter is restored.
#   max_epochs must therefore be set as an ABSOLUTE value: checkpoint epoch + desired additional epochs.
#
#   If Lightning 2.x cannot load the old pytorch_lightning 1.9.5 checkpoint,
#   fallback: replace --ckpt_path with --model.vocoder_warmstart_ckpt
#   (then epoch starts at 0 and max_epochs can be set directly).
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

# Checkpoint to resume from | TODO set to your own training checkpoint or the original ones from e.g.
# wget "https://huggingface.co/datasets/rhasspy/piper-checkpoints/resolve/main/de/de_DE/thorsten/high/config.json" -O thorsten-high-config.json
# wget "https://huggingface.co/datasets/rhasspy/piper-checkpoints/resolve/main/de/de_DE/thorsten/high/epoch%3D2665-step%3D1182078.ckpt" -O thorsten-high.ckpt
CHECKPOINT="${SCRIPT_DIR}/training.local/training-piper/meine-stimme-out/lightning_logs/version_2/checkpoints/epoch=3664-step=1210050.ckpt"

# Input
CSV_PATH="$BASE_DIR/meine-stimme/metadata_piper1.csv"
AUDIO_DIR="$BASE_DIR/meine-stimme/wavs"

# Output
CACHE_DIR="$BASE_DIR/training-cache"
OUTPUT_DIR="$BASE_DIR/meine-stimme-out"
CONFIG_PATH="$OUTPUT_DIR/de_DE-meinname-high.onnx.json"


# Training parameters
ADDITIONAL_EPOCHS=1000       # How many NEW epochs to train
BATCH_SIZE=32                # RTX 4090 (24 GB): 16–32 for high quality
VOICE_NAME="de_DE-meinname-high"

# ─── Extract epoch from checkpoint ─────────────────────────────────
extract_epoch() {
  # 1. Try: extract from filename (epoch=3664-step=1210050.ckpt → 3664)
  local filename
  filename="$(basename "$1")"
  if [[ "$filename" =~ epoch=([0-9]+) ]]; then
    echo "${BASH_REMATCH[1]}"
    return
  fi
  # 2. Fallback: read from checkpoint file via torch
  source "$PIPER1_GPL_DIR/.venv/bin/activate"
  python3 -c "
import torch
ckpt = torch.load('$1', map_location='cpu', weights_only=False)
e = ckpt.get('epoch', '')
if e == '': raise SystemExit(1)
print(e)
" 2>/dev/null
}

CHECKPOINT_EPOCH="$(extract_epoch "$CHECKPOINT")"
if [ -z "$CHECKPOINT_EPOCH" ]; then
  echo "ERROR: Could not extract epoch from checkpoint: $CHECKPOINT"
  exit 1
fi

MAX_EPOCHS=$((CHECKPOINT_EPOCH + ADDITIONAL_EPOCHS))

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
echo "  Checkpoint epoch: $CHECKPOINT_EPOCH (read from checkpoint)"
echo "  Additional epochs: +$ADDITIONAL_EPOCHS"
echo "  max_epochs:    $MAX_EPOCHS (absolute)"
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
  --trainer.max_epochs "$MAX_EPOCHS" \
  --trainer.accelerator gpu \
  --trainer.devices 1 \
  --trainer.precision 32 \
  --trainer.default_root_dir "$OUTPUT_DIR" \
  --model.resblock 1 \
  --model.resblock_kernel_sizes '[3, 7, 11]' \
  --model.resblock_dilation_sizes '[[1, 3, 5], [1, 3, 5], [1, 3, 5]]' \
  --model.upsample_rates '[8, 8, 2, 2]' \
  --model.upsample_initial_channel 512 \
  --model.upsample_kernel_sizes '[16, 16, 4, 4]' \
  --ckpt_path "$CHECKPOINT"
