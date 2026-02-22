# Eigene Stimme für Piper TTS erstellen – Komplettanleitung

> Diese Anleitung beschreibt den gesamten Workflow: vom Erstellen eines Trainingsdatensatzes über die Audioaufnahme mit einer GUI-Anwendung bis hin zum Finetuning und Export eines eigenen Piper-TTS-Sprachmodells.
>
> **Stand: Februar 2026** — basiert auf [OHF-Voice/piper1-gpl](https://github.com/OHF-Voice/piper1-gpl) v1.4.1 (Nachfolger des archivierten `rhasspy/piper`).

---

## Inhaltsverzeichnis

1. [Überblick: Wie Piper TTS funktioniert](#1-überblick-wie-piper-tts-funktioniert)
2. [Zwei Wege zur eigenen Stimme](#2-zwei-wege-zur-eigenen-stimme)
3. [Daten vorbereiten: Das CSV-Format](#3-daten-vorbereiten-das-csv-format)
4. [Aufnahme-Setup: Audio-Hardware & EasyEffects](#4-aufnahme-setup-audio-hardware--easyeffects)
5. [Aufnahme-Tool: PySide6 GUI Recorder](#5-aufnahme-tool-pyside6-gui-recorder)
6. [Training-Umgebung einrichten](#6-training-umgebung-einrichten)
7. [Training: Finetuning eines bestehenden Modells](#7-training-finetuning-eines-bestehenden-modells)
8. [Training: Modell von Grund auf](#8-training-modell-von-grund-auf)
9. [Medium vs. High Quality](#9-medium-vs-high-quality)
10. [ONNX-Export & die .onnx.json Konfiguration](#10-onnx-export--die-onnxjson-konfiguration)
11. [Tipps für gute Ergebnisse](#11-tipps-für-gute-ergebnisse)
12. [Nützliche Ressourcen](#12-nützliche-ressourcen)

---

## 1. Überblick: Wie Piper TTS funktioniert

Piper basiert auf **VITS** (Conditional Variational Autoencoder with Adversarial Learning). Die fertigen Modelle werden als ONNX-Dateien ausgeliefert, das Training selbst findet jedoch auf PyTorch-Checkpoints statt.

Jedes Piper-Modell besteht aus zwei Dateien:

```
de_DE-thorsten-medium.onnx        ← das neuronale Netz
de_DE-thorsten-medium.onnx.json   ← Konfiguration (Phoneme, Samplerate, Inference-Parameter)
```

Piper braucht **beide Dateien**, um Sprache zu erzeugen.

---

## 2. Zwei Wege zur eigenen Stimme

| Methode | Datenbedarf | Trainingszeit | Qualität |
|---------|-------------|---------------|----------|
| **Finetuning** (empfohlen) | 30–60 Min. Audio | Kürzer (500–2000 Epochen) | Gut bis sehr gut |
| **Von Grund auf** | Mehrere Stunden Audio | Deutlich länger | Abhängig von Datenmenge |

Finetuning ist fast immer der bessere Weg, da ein vortrainiertes Modell bereits die Grundlagen der Sprachsynthese gelernt hat.

---

## 3. Daten vorbereiten: Das CSV-Format

### Verzeichnisstruktur

```
mein-dataset/
  metadata.csv
  wavs/
    satz_0001.wav
    satz_0002.wav
    satz_0003.wav
    ...
```

### metadata.csv Format (piper1-gpl)

Die Datei ist **Pipe-getrennt** (`|`), hat **keinen Header** und **zwei Spalten**:

```
dateiname.wav|normalisierte_transkription
```

- **Spalte 1**: Dateiname **mit `.wav`-Endung**, ohne Pfad
- **Spalte 2**: Text vollständig ausgeschrieben, so wie er gesprochen wird

### Beispiele

```
satz_0001.wav|Am dritten Januar zweitausendvierundzwanzig waren es minus fünf Grad Celsius in Berlin.
satz_0002.wav|Doktor Müller hat circa zweihundertfünfzig Patienten pro Monat.
satz_0003.wav|Die A sieben ist mit neunhundertzweiundsechzig Komma zwei Kilometern die längste Autobahn Deutschlands.
```

### Konvertierung vom alten 3-Spalten-Format

Falls eine bestehende `metadata.csv` im alten rhasspy/piper-Format (3 Spalten, Dateiname ohne `.wav`) vorliegt:

```bash
python convert_metadata.py /pfad/zu/alte/metadata.csv -o /pfad/zu/metadata_piper1.csv
```

Das Script `convert_metadata.py` liegt im piper1-gpl Repo und übernimmt die normalisierte dritte Spalte als Text und ergänzt die `.wav`-Endung.

### Regeln

- Keine Anführungszeichen um die Felder
- Kein Header in der ersten Zeile
- UTF-8 Encoding
- Jede WAV-Datei = ein Satz, idealerweise 1–15 Sekunden lang

### WAV-Format für Piper

| Parameter | Wert |
|-----------|------|
| Samplerate | 22050 Hz |
| Kanäle | 1 (Mono) |
| Bit-Tiefe | 16-bit PCM |
| Format | WAV |

### Inhaltliche Tipps für die Sätze

Für ein gutes TTS-Modell sollten die Trainingssätze abdecken:

- **Phonetische Vielfalt**: Umlaute (ä, ö, ü), ß, ch-Laute, pf, sch, st/sp, zw, qu
- **Zahlen & Abkürzungen** (im CSV bereits ausgeschrieben)
- **Verschiedene Satztypen**: Aussagen, Aufforderungen, Fragen, formelle/informelle Sprache
- **Natürliche Alltagssprache**: Verschiedene Themen und Satzlängen

Im Repository ist eine fertige `metadata.csv` mit 250 Sätzen enthalten, die all diese Kriterien erfüllt.

---

## 4. Aufnahme-Setup: Audio-Hardware & EasyEffects

### Hardware-Tipps

- Kein Raumhall – kleiner, möblierter Raum oder Decke als Dämpfung
- Gleichmäßiger Abstand zum Mikro (~15–20 cm, leicht seitlich)
- Kein Hintergrundrauschen – Fenster zu, Lüfter aus
- Pop-Schutz verwenden, falls vorhanden
- Gleichmäßiger Pegel – nicht flüstern, nicht schreien

### EasyEffects Konfiguration

Für saubere Aufnahmen wird [EasyEffects](https://github.com/wwmm/easyeffects) als Echtzeit-Audio-Effektkette auf dem Mikrofon-Eingang verwendet. Die Effektkette in der richtigen Reihenfolge:

#### Kette: Filter → RNNoise → Gate → Compressor → De-Esser

| # | Effekt | Zweck |
|---|--------|-------|
| 1 | **Filter (Highpass 80 Hz)** | Entfernt tiefes Rumpeln (Trittschall, Lüfter-Brummen) |
| 2 | **RNNoise** | KI-basierte Rauschentfernung in Echtzeit |
| 3 | **Gate** | Drückt Signal in Sprechpausen runter |
| 4 | **Compressor** | Gleicht Lautstärkeschwankungen aus |
| 5 | **De-Esser** | Zähmt scharfe S/Z/Sch-Laute |

#### Wichtige Parameter

**Filter (Highpass):**
- Frequenz: 80 Hz
- Mode: 12dB/oct Highpass
- Resonance: -3.0

**Gate:**
- Threshold: -36 dB (niedriger = weniger aggressive Stille-Erkennung)
- Reduction: -24 dB (nicht -∞, damit kein harter "digitaler Void"-Effekt entsteht)
- Attack: 5 ms
- Release: 50 ms

**Compressor:**
- Ratio: 3:1
- Threshold: -12 dB
- Attack: 15 ms (lässt Konsonanten-Transienten durch)
- Release: 100 ms
- Makeup: +3 dB

**De-Esser:**
- Threshold: -18 dB
- Frequenzbereich: 4500–6000 Hz
- Ratio: 3:0
- Mode: Wide
- `sc-listen`: Temporär auf `true` setzen zum Einstellen – dann hört man nur was der De-Esser wegschneidet

#### Installation des Presets

```bash
# Native EasyEffects
cp piper-tts-recording.json ~/.config/easyeffects/input/

# Flatpak-Version
cp piper-tts-recording.json ~/.var/app/com.github.wwmm.easyeffects/config/easyeffects/input/
```

Dann in EasyEffects auf den **Input**-Tab wechseln und das Preset `piper-tts-recording` laden.

#### Feintuning

- **Gate-Threshold**: Leise Silben werden abgeschnitten → weiter runter (z.B. -42 dB). Rauschen in Pausen → höher (z.B. -30 dB).
- **De-Esser**: Stimme klingt dumpf → `bypass` auf `true` oder Threshold tiefer.
- **Immer 2–3 Testaufnahmen machen** und mit Kopfhörern anhören, bevor es losgeht.

#### Was man bewusst weglassen sollte

- Reverb/Hall – absolut kontraproduktiv für TTS-Training
- Exciter/Enhancer – verfälscht die Stimme
- Limiter – der Compressor reicht
- Stereo-Effekte – Aufnahme ist Mono

Im Repository liegt eine fertige `piper-tts-recording.json` mit all diesen Einstellungen.

---

## 5. Aufnahme-Tool: PySide6 GUI Recorder

### Voraussetzungen

```bash
pip install PySide6 sounddevice soundfile numpy
```

### Starten

```bash
# Standard (metadata.csv im aktuellen Ordner)
python record_gui.py

# Mit Parametern
python record_gui.py --metadata metadata.csv --output ./wavs

# Bestimmtes Mikrofon (Geräte-ID)
python record_gui.py --device 3
```

### Kommandozeilen-Parameter

| Parameter | Standard | Beschreibung |
|-----------|----------|--------------|
| `--metadata` | `metadata.csv` | Pfad zur Metadata-Datei |
| `--output` | `./wavs` | Ausgabeordner für WAV-Dateien |
| `--samplerate` | `22050` | Samplerate in Hz |
| `--channels` | `1` | Kanäle (1 = Mono) |
| `--device` | System-Standard | Audio-Eingabegerät ID |

### Funktionen

- **Scrollbare Tabelle** mit allen Sätzen – Status (✓ / —) zeigt was schon aufgenommen ist
- **Großer gelber Satz-Text** zum Vorlesen, darunter der Roh-Text
- **VU-Meter** mit Gradient (grün → gelb → rot) und Peak-Marker
- **Wellenform-Anzeige** nach der Aufnahme, mit Playback-Position
- **Fortschrittsbalken** (x/250 mit Prozent)
- **Dark Mode** durchgängig
- **Gerätewahl** per Dropdown
- Springt automatisch zum ersten nicht aufgenommenen Satz

### Tastenkürzel

| Taste | Aktion |
|-------|--------|
| **Leertaste halten** | Aufnehmen |
| **Leertaste loslassen** | Aufnahme stoppen + speichern |
| **Enter** | Weiter zum nächsten Satz |
| **P** | Abspielen / Stopp |

Zum erneuten Aufnehmen einfach nochmal Leertaste halten – die alte Datei wird überschrieben.

### Alternatives Terminal-Tool

Für Systeme ohne GUI liegt auch ein Terminal-basiertes Aufnahme-Script bei (`record_dataset.py`), das mit `pynput` arbeitet und den gleichen Workflow bietet:

```bash
pip install sounddevice soundfile pynput

python record_dataset.py --metadata metadata.csv --output ./wavs --start 1
python record_dataset.py --list-devices
```

---

## 6. Training-Umgebung einrichten

### Voraussetzungen

piper1-gpl benötigt **Python 3.10–3.13** (3.13 empfohlen). Python 3.14 funktioniert für Inferenz, aber `lightning` listet es noch nicht offiziell für Training.

Im Gegensatz zum alten `rhasspy/piper` wird **kein `piper-phonemize`** mehr benötigt — espeak-ng ist direkt in das `piper-tts` Wheel eingebettet (stable ABI). Ebenso entfällt der separate Preprocessing-Schritt: Phonemisierung, Silence-Trimming und Spektrogramm-Berechnung passieren automatisch beim Training-Start und werden im Cache-Verzeichnis gespeichert.

### Setup

```bash
git clone https://github.com/OHF-Voice/piper1-gpl.git
cd piper1-gpl
```

Das Setup-Script `setup_my_env.sh` automatisiert die gesamte Einrichtung:

```bash
./setup_my_env.sh
```

Das Script macht folgendes:

1. **System-Build-Abhängigkeiten** installieren (`build-essential`, `cmake`, `ninja-build`, etc.)
2. **Python 3.13** via pyenv installieren
3. **venv** erstellen
4. **`pip install -e '.[train]'`** — installiert PyTorch, Lightning, librosa, Cython, etc.
5. **scikit-build + cmake + ninja** nachinstallieren (für C-Extension Build)
6. **`python setup.py build_ext --inplace`** — espeak-ng Bridge kompilieren
7. **`build_monotonic_align.sh`** — VITS Alignment C-Modul kompilieren

### Manuelles Setup (falls venv bereits existiert)

```bash
cd ~/piper1-gpl
source .venv/bin/activate
pip install -e '.[train]'
pip install "scikit-build<1" "cmake>=3.18,<4" "ninja>=1,<2"
python setup.py build_ext --inplace
./build_monotonic_align.sh
```

<details>
<summary><b>setup_my_env.sh</b> (Inhalt zum Nachvollziehen)</summary>

```bash
#!/usr/bin/env bash
# Setup-Script für Piper1-GPL TTS Training auf Python 3.13 (via pyenv)
# Basiert auf OHF-Voice/piper1-gpl (Nachfolger von rhasspy/piper)
# Getestet auf Debian Trixie/Sid mit NVIDIA RTX 4090
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_VERSION=3.13

echo "=== 1/5: System-Build-Abhängigkeiten installieren ==="
sudo apt install -y build-essential cmake ninja-build \
  libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev \
  libncursesw5-dev xz-utils tk-dev libxml2-dev libxmlsec1-dev \
  libffi-dev liblzma-dev

echo "=== 2/5: Python $PYTHON_VERSION via pyenv installieren ==="
if ! command -v pyenv &>/dev/null; then
  echo "pyenv nicht gefunden. Installiere pyenv..."
  curl https://pyenv.run | bash
  export PYENV_ROOT="$HOME/.pyenv"
  export PATH="$PYENV_ROOT/bin:$PATH"
  eval "$(pyenv init -)"
fi

if ! pyenv versions --bare | grep -q "^${PYTHON_VERSION}"; then
  pyenv install "$PYTHON_VERSION"
fi

PYTHON_BIN="$(pyenv prefix "$PYTHON_VERSION")/bin/python"
echo "Verwende: $PYTHON_BIN"

echo "=== 3/5: venv erstellen ==="
cd "$SCRIPT_DIR"
rm -rf .venv
"$PYTHON_BIN" -m venv .venv
source .venv/bin/activate

echo "=== 4/5: piper1-gpl + Training-Abhängigkeiten installieren ==="
pip install --upgrade pip wheel setuptools
pip install -e '.[train]'

# scikit-build + cmake + ninja werden für build_ext benötigt (in [dev], nicht in [train])
pip install "scikit-build<1" "cmake>=3.18,<4" "ninja>=1,<2"

# Dev-Build für C-Extension (espeak-ng bridge)
python setup.py build_ext --inplace

echo "=== 5/5: monotonic_align C-Modul kompilieren ==="
bash "$SCRIPT_DIR/build_monotonic_align.sh"
```

</details>

### Pretrained Checkpoint herunterladen

Checkpoints gibt es unter: https://huggingface.co/datasets/rhasspy/piper-checkpoints

Für deutsches Finetuning eignet sich der **thorsten**-Checkpoint besonders gut:

```bash
# Medium-Checkpoint (~846 MB)
wget "https://huggingface.co/datasets/rhasspy/piper-checkpoints/resolve/main/de/de_DE/thorsten/medium/epoch%3D3135-step%3D2702056.ckpt" \
  -O thorsten-medium.ckpt

# High-Checkpoint (~951 MB)
wget "https://huggingface.co/datasets/rhasspy/piper-checkpoints/resolve/main/de/de_DE/thorsten/high/epoch%3D2665-step%3D1182078.ckpt" \
  -O thorsten-high.ckpt
```

**Wichtig:** Diese Checkpoints stammen vom archivierten rhasspy/piper und sind **nicht direkt mit `--ckpt_path` in piper1-gpl ladbar** — es gibt drei Inkompatibilitäten: (1) Architektur-Mismatch bei den ResBlock-Layern (`convs1`/`convs2` vs. `convs`), (2) veraltete Hyperparameter die Lightning 2.x ablehnt, (3) `pathlib.PosixPath`-Objekte die PyTorch 2.6+ nicht deserialisieren kann. Stattdessen **immer `--model.vocoder_warmstart_ckpt`** verwenden — das lädt nur die Modell-Weights (non-strict), der Epoch-Zähler startet bei 0. Details siehe [Abschnitt 9](#9-medium-vs-high-quality).

---

## 7. Training: Finetuning eines bestehenden Modells

### Kein separates Preprocessing nötig

Anders als beim alten `rhasspy/piper` gibt es **keinen separaten Preprocessing-Schritt** mehr. Beim Start des Trainings werden automatisch erzeugt und im `--data.cache_dir` gespeichert:

- Phoneme (via eingebettetes espeak-ng)
- Silence-getrimmtes Audio (via Silero VAD)
- Mel-Spektrogramme

### Das Cache-Verzeichnis (`--data.cache_dir`)

Der Cache enthält vorberechnete `.pt`-Tensoren für jede Äußerung (Phoneme, getrimmtes Audio, Spektrogramme). Er wird beim nächsten Training-Start wiederverwendet ("skip if exists") und spart erheblich Zeit.

**Empfohlener Pfad**: Neben dem Dataset, z.B. `mein-dataset/cache/`

**Wann löschen**: Nur wenn sich die Eingaben ändern (anderer `espeak_voice`, korrigierte Audio-Dateien, andere `trim_silence`-Einstellung). Bei Änderungen an `batch_size`, `max_epochs` oder Modell-Architektur bleibt der Cache gültig.

### Finetuning starten (Medium Quality)

```bash
python -m piper.train fit \
  --data.voice_name "de_DE-meinname-medium" \
  --data.csv_path /pfad/zu/metadata_piper1.csv \
  --data.audio_dir /pfad/zu/wavs/ \
  --model.sample_rate 22050 \
  --data.espeak_voice de \
  --data.cache_dir /pfad/zu/mein-dataset/cache/ \
  --data.config_path /pfad/zu/output/de_DE-meinname-medium.onnx.json \
  --data.batch_size 32 \
  --trainer.max_epochs 2000 \
  --trainer.accelerator gpu \
  --trainer.devices 1 \
  --trainer.precision 32 \
  --model.vocoder_warmstart_ckpt /pfad/zu/thorsten-medium.ckpt
```

### Wichtige Parameter

| Parameter | Bedeutung |
|-----------|-----------|
| `--trainer.max_epochs` | Gesamtzahl der Epochen. Bei `--model.vocoder_warmstart_ckpt` startet der Zähler bei 0 — `2000` sind wirklich 2000 Epochen. Default: `-1` (unendlich — Training läuft bis Ctrl+C). |
| `--data.batch_size` | Default: `32`. Wird automatisch auch an `model.batch_size` gekoppelt. |
| `--data.validation_split` | Default: `0.1` (10% des Datasets für Validierung). |
| `--data.num_test_examples` | Default: `5`. Reserviert N Äußerungen für Audio-Sample-Generierung in TensorBoard. |
| `--model.vocoder_warmstart_ckpt` | Checkpoint zum Finetunen. Lädt nur die Modell-Weights (non-strict), kompatibel mit rhasspy/piper-Checkpoints. **Nicht** `--ckpt_path` verwenden — das scheitert an Architektur-Inkompatibilitäten (siehe [Abschnitt 9](#9-medium-vs-high-quality)). |
| `--data.config_path` | Hier schreibt das Training die `.onnx.json` Konfiguration hin — wird beim ONNX-Export gebraucht. |

### Batch-Size-Empfehlung

| GPU VRAM | Medium Quality | High Quality |
|----------|---------------|--------------|
| 8 GB | 8–12 | 4–8 |
| 12 GB | 16–24 | 8–16 |
| 24 GB (RTX 3090/4090) | **32–48** | **16–32** |
| 48 GB (A6000) | 64 | 32–48 |

High Quality benötigt mehr VRAM pro Sample (512 vs. 256 Kanäle im Upsample-Netz). Bei OOM die Batch-Size reduzieren.

### Training überwachen

In einem zweiten Terminal:

```bash
tensorboard --logdir lightning_logs/
# → http://localhost:6006 im Browser öffnen
```

#### Scalars: Die wichtigsten Metriken

| Metrik | Bedeutung |
|--------|-----------|
| **loss_d** | **Discriminator Loss** — wie gut der Discriminator echte von generierten Samples unterscheidet. Sollte sich bei stabilem Wert einpendeln (nicht gegen 0 gehen). |
| **loss_g** | **Generator Loss** — wie gut das TTS-Modell den Discriminator täuscht. Setzt sich zusammen aus Adversarial Loss, Mel-Spectrogram Loss, Feature Matching Loss und KL-Divergence. Sollte über die Zeit sinken. |
| **val_loss** | **Validation Loss** — Mel-Spectrogram Loss auf dem Validation-Set (ungesehene Daten). **Wichtigster Indikator** für tatsächliche Qualität. Wenn val_loss steigt während loss_g sinkt → Overfitting. |
| **loss_mel** | Mel-Spectrogram Reconstruction Loss — wie nah das generierte Audio am Original ist. |
| **loss_kl** | KL-Divergence Loss — wie gut die latente Repräsentation zur Prior-Verteilung passt. |
| **loss_fm** | Feature Matching Loss — Ähnlichkeit der internen Discriminator-Features. |
| **learning_rate** | Hilfreich um zu prüfen ob der LR-Scheduler richtig arbeitet. |

#### Images & Audio

- **Mel-Spectrogramme**: Vergleich Ground-Truth vs. generiert. Je ähnlicher, desto besser. Unscharfe/verschmierte Bereiche = schlechte Qualität.
- **Alignment-Plots**: Zeigen wie Text auf die Zeitachse gemappt wird. Eine klare, monoton steigende Diagonale = gutes Alignment. Chaotische Diagonale = Probleme mit Aussprache.
- **Audio-Samples**: Generierte Samples direkt im Browser anhörbar — der direkteste Qualitätscheck.

#### Worauf achten

- `val_loss` ist der beste Indikator für hörbare Verbesserung
- `loss_d` und `loss_g` sollten sich gegenseitig balancieren (GAN-Equilibrium)
- Wenn `loss_d` auf 0 fällt, dominiert der Discriminator und das Training stagniert
- Alignment-Plots sind am aufschlussreichsten — saubere Diagonale = gute Aussprache

### Training fortsetzen

Einfach mit höherem `max_epochs`-Wert neu starten. Lightning findet den letzten Checkpoint automatisch in `lightning_logs/`:

```bash
# Weitermachen bis Epoch 4000 (war vorher auf 2000 begrenzt)
python -m piper.train fit \
  ... (gleiche Parameter) \
  --trainer.max_epochs 4000
```

Alternativ `--trainer.max_epochs -1` für unbegrenztes Training (manuell mit Ctrl+C stoppen).

---

## 8. Training: Modell von Grund auf

Gleicher Ablauf wie Finetuning, aber **ohne** `--model.vocoder_warmstart_ckpt`:

```bash
python -m piper.train fit \
  --data.voice_name "de_DE-meinname-medium" \
  --data.csv_path /pfad/zu/metadata_piper1.csv \
  --data.audio_dir /pfad/zu/wavs/ \
  --model.sample_rate 22050 \
  --data.espeak_voice de \
  --data.cache_dir /pfad/zu/mein-dataset/cache/ \
  --data.config_path /pfad/zu/output/de_DE-meinname-medium.onnx.json \
  --data.batch_size 32 \
  --trainer.max_epochs 5000 \
  --trainer.accelerator gpu \
  --trainer.devices 1 \
  --trainer.precision 32
```

Unterschiede zum Finetuning:

- Deutlich mehr Daten nötig (mehrere Stunden)
- Längere Trainingszeit (5000+ Epochen)
- Für High Quality kann `--model.vocoder_warmstart_ckpt` genutzt werden — kopiert nur die Vocoder-Gewichte aus einem bestehenden Medium-Checkpoint, ohne die vollständige Architektur-Übereinstimmung zu verlangen

---

## 9. Medium vs. High Quality

piper1-gpl hat **keinen `--quality`-Schalter**. Quality ergibt sich aus 6 Modell-Parametern, die die Vocoder-Architektur definieren:

| Parameter | Medium (Default) | High |
|-----------|-----------------|------|
| `--model.resblock` | `"2"` | `"1"` |
| `--model.resblock_kernel_sizes` | `[3, 5, 7]` | `[3, 7, 11]` |
| `--model.resblock_dilation_sizes` | `[[1, 2], [2, 6], [3, 12]]` | `[[1, 3, 5], [1, 3, 5], [1, 3, 5]]` |
| `--model.upsample_rates` | `[8, 8, 4]` | `[8, 8, 2, 2]` |
| `--model.upsample_initial_channel` | `256` | `512` |
| `--model.upsample_kernel_sizes` | `[16, 16, 8]` | `[16, 16, 4, 4]` |

Medium lässt man weg (sind die Defaults). Für High müssen alle 6 Parameter explizit gesetzt werden.

### High-Quality Finetuning

**Problem:** Die Checkpoints auf HuggingFace sind Medium-Architektur. Ein Medium-Checkpoint kann nicht mit `--ckpt_path` in ein High-Quality-Training geladen werden (Shape-Mismatch: 256 vs. 512 Kanäle, 3 vs. 4 Upsample-Stufen).

**Lösung:** `--model.vocoder_warmstart_ckpt` statt `--ckpt_path` verwenden. Das kopiert nur die Vocoder-Gewichte (shape-tolerant) und überspringt das Phonem-Embedding. Der Epoch-Zähler startet bei 0 — `--trainer.max_epochs 2000` sind hier wirklich 2000 Epochen:

```bash
python -m piper.train fit \
  --data.voice_name "de_DE-meinname-high" \
  --data.csv_path /pfad/zu/metadata_piper1.csv \
  --data.audio_dir /pfad/zu/wavs/ \
  --model.sample_rate 22050 \
  --data.espeak_voice de \
  --data.cache_dir /pfad/zu/mein-dataset/cache/ \
  --data.config_path /pfad/zu/output/de_DE-meinname-high.onnx.json \
  --data.batch_size 32 \
  --trainer.max_epochs 2000 \
  --trainer.accelerator gpu \
  --trainer.devices 1 \
  --trainer.precision 32 \
  --model.resblock 1 \
  --model.resblock_kernel_sizes '[3, 7, 11]' \
  --model.resblock_dilation_sizes '[[1, 3, 5], [1, 3, 5], [1, 3, 5]]' \
  --model.upsample_rates '[8, 8, 2, 2]' \
  --model.upsample_initial_channel 512 \
  --model.upsample_kernel_sizes '[16, 16, 4, 4]' \
  --model.vocoder_warmstart_ckpt /pfad/zu/thorsten-medium.ckpt
```

### High-Quality Finetuning mit High-Checkpoint (rhasspy/piper)

> **Achtung:** `--ckpt_path` (strict loading) funktioniert **nicht** mit alten rhasspy/piper-Checkpoints. Es gibt drei Inkompatibilitäten:
>
> 1. **Architektur-Mismatch:** rhasspy/piper verwendet ResBlocks mit zwei getrennten ModuleLists (`convs1` + `convs2`, je 3 Convolutions = 6 pro Block). piper1-gpl verwendet eine einzelne flache Liste (`convs`, 2 pro Block). Das führt zu `Missing key` / `Unexpected key` Fehlern beim Laden des `state_dict`.
>
> 2. **Veraltete Hyperparameter:** Alte Checkpoints speichern Trainer/Config-Keys (z.B. `sample_bytes`, `quality`, `gpus`, `auto_lr_find`), die Lightning 2.x als unbekannte CLI-Argumente ablehnt.
>
> 3. **PyTorch 2.6+ `weights_only=True`:** Alte Checkpoints enthalten `pathlib.PosixPath`-Objekte, die bei der Deserialisierung scheitern.
>
> **Lösung:** Auch bei High→High muss `--model.vocoder_warmstart_ckpt` verwendet werden. Der Epoch-Zähler startet bei 0.

```bash
python -m piper.train fit \
  ... (gleiche Parameter wie oben) \
  --trainer.max_epochs 2000 \
  --model.resblock 1 \
  --model.resblock_kernel_sizes '[3, 7, 11]' \
  --model.resblock_dilation_sizes '[[1, 3, 5], [1, 3, 5], [1, 3, 5]]' \
  --model.upsample_rates '[8, 8, 2, 2]' \
  --model.upsample_initial_channel 512 \
  --model.upsample_kernel_sizes '[16, 16, 4, 4]' \
  --model.vocoder_warmstart_ckpt /pfad/zu/thorsten-high.ckpt
```

### Wann Medium, wann High?

| | Medium | High |
|---|---|---|
| **Audioqualität** | Gut | Besser, natürlicher |
| **Modellgröße** | ~60 MB (ONNX) | ~80 MB (ONNX) |
| **Inferenz-Geschwindigkeit** | Schneller | Langsamer |
| **VRAM-Bedarf (Training)** | Weniger | ~2x mehr |
| **Checkpoint-Verfügbarkeit** | Viele auf HuggingFace | Wenige, Warmstart nötig |
| **Empfehlung** | Echtzeit-TTS, eingebettete Systeme | Maximale Sprachqualität |

---

## 10. ONNX-Export & die .onnx.json Konfiguration

### Export

> **PyTorch 2.6+ Patch nötig:** Der ONNX-Export schlägt mit dem neuen Dynamo-basierten Exporter fehl (`GuardOnDataDependentSymNode` in `transforms.py:rational_quadratic_spline`). Fix: In `piper1-gpl/src/piper/train/export_onnx.py` den Parameter `dynamo=False` zum `torch.onnx.export()`-Aufruf hinzufügen, um den Legacy TorchScript-Exporter zu erzwingen.

```bash
python -m piper.train.export_onnx \
  --checkpoint lightning_logs/version_X/checkpoints/epoch=XXXX-step=XXXXXXX.ckpt \
  --output-file /pfad/zu/de_DE-meinname-high.onnx
```

Die `.onnx.json` Konfiguration wurde bereits während des Trainings nach `--data.config_path` geschrieben. Sie muss neben die ONNX-Datei gelegt werden:

```bash
# Falls config_path nicht schon den richtigen Namen hat:
cp /pfad/zu/output/de_DE-meinname-high.onnx.json /pfad/zu/de_DE-meinname-high.onnx.json
```

Testen:

```bash
echo "Hallo, das ist meine eigene Stimme!" | \
  piper --model de_DE-meinname-high.onnx --output_file test.wav
```

### Was ist die .onnx.json?

Die `.onnx.json` ist die Konfigurationsdatei, die jedes Piper-Modell begleitet. Sie enthält:

- **audio**: Sample-Rate, Fenstergröße, Hop-Length
- **espeak**: Sprache/Stimme für die Phonem-Konvertierung
- **inference**: Noise-Scale, Length-Scale, Noise-W (Sprechgeschwindigkeit/Variation)
- **phoneme_id_map**: Zuordnung von Phonemen zu numerischen IDs
- **num_speakers / speaker_id_map**: Bei Multi-Speaker-Modellen

### Beispielstruktur

```json
{
  "audio": {
    "sample_rate": 22050,
    "quality": "high"
  },
  "espeak": {
    "voice": "de"
  },
  "inference": {
    "noise_scale": 0.667,
    "length_scale": 1.0,
    "noise_w": 0.8
  },
  "phoneme_id_map": {
    "_": [0],
    "a": [1],
    "b": [2]
  }
}
```

Manuelle Anpassungen sind nur nötig, wenn man z.B. `length_scale` für schnelleres/langsameres Sprechen ändern will.

---

## 11. Tipps für gute Ergebnisse

### Aufnahmequalität

- **Audioqualität ist der wichtigste Faktor** – schlechte Aufnahmen kann kein Training retten
- Kein Hall, kein Rauschen, gleichmäßiger Pegel
- Konsistente Sprechweise: gleiches Tempo, gleiche Emotion
- 2–3 Testaufnahmen vor dem Start machen und mit Kopfhörern prüfen

### Training

- **GPU**: NVIDIA mit mindestens 8 GB VRAM, Finetuning geht auch mit 6 GB bei kleiner Batch-Size
- **Epochen**: Beim Finetuning reichen oft 500–2000 Epochen
- **Batch-Size**: So groß wie möglich (VRAM-Limit), größere Batches stabilisieren das Training
- **Regelmäßig Samples generieren** und anhören, um Über-/Untertraining zu erkennen
- **Deutsche Modelle**: Als Basis für deutsches Finetuning den `thorsten`-Checkpoint verwenden
- **TensorBoard** immer mitlaufen lassen zum Monitoring

---

## 12. Nützliche Ressourcen

| Ressource | URL |
|-----------|-----|
| Piper1-GPL GitHub (aktiv) | https://github.com/OHF-Voice/piper1-gpl |
| Piper1-GPL Training Docs | https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/TRAINING.md |
| Piper Checkpoints | https://huggingface.co/datasets/rhasspy/piper-checkpoints |
| Piper Recording Studio | https://github.com/rhasspy/piper-recording-studio |
| EasyEffects | https://github.com/wwmm/easyeffects |
| rhasspy/piper (archiviert) | https://github.com/rhasspy/piper |

---

## Enthaltene Dateien

| Datei | Beschreibung |
|-------|--------------|
| `metadata.csv` | 250 deutsche Sätze im LJSpeech-Format, phonetisch vielfältig |
| `record_gui.py` | PySide6 GUI-Recorder mit VU-Meter, Wellenform, Fortschrittsbalken |
| `record_dataset.py` | Alternatives Terminal-basiertes Aufnahme-Tool |
| `piper-tts-recording.json` | EasyEffects Input-Preset für saubere Mikrofon-Aufnahmen |
| `convert_metadata.py` | Konvertiert alte 3-Spalten metadata.csv ins neue 2-Spalten-Format (im piper1-gpl Repo) |

---

## Kurzanleitung (Quick Start)

```bash
# 1. Abhängigkeiten installieren
pip install PySide6 sounddevice soundfile numpy

# 2. EasyEffects-Preset laden
cp piper-tts-recording.json ~/.config/easyeffects/input/
# → EasyEffects öffnen → Input → Preset laden

# 3. Aufnahme starten
python record_gui.py --metadata metadata.csv --output ./wavs

# 4. Alle 250 Sätze einsprechen (Leertaste halten = aufnehmen)

# 5. piper1-gpl Training-Umgebung einrichten
git clone https://github.com/OHF-Voice/piper1-gpl.git
cd piper1-gpl
./setup_my_env.sh
source .venv/bin/activate

# 6. metadata.csv konvertieren (falls altes 3-Spalten-Format)
python convert_metadata.py /pfad/zu/metadata.csv -o /pfad/zu/metadata_piper1.csv

# 7. Finetuning (Medium Quality, mit thorsten-Checkpoint)
python -m piper.train fit \
  --data.voice_name "de_DE-meinname-medium" \
  --data.csv_path /pfad/zu/metadata_piper1.csv \
  --data.audio_dir /pfad/zu/wavs/ \
  --model.sample_rate 22050 \
  --data.espeak_voice de \
  --data.cache_dir /pfad/zu/mein-dataset/cache/ \
  --data.config_path /pfad/zu/output/de_DE-meinname-medium.onnx.json \
  --data.batch_size 32 \
  --trainer.max_epochs 2000 \
  --trainer.accelerator gpu \
  --trainer.devices 1 \
  --trainer.precision 32 \
  --model.vocoder_warmstart_ckpt /pfad/zu/thorsten-medium.ckpt

# 8. ONNX-Export
python -m piper.train.export_onnx \
  --checkpoint lightning_logs/version_0/checkpoints/best.ckpt \
  --output-file /pfad/zu/de_DE-meinname-medium.onnx

# 9. Testen
echo "Hallo, das ist meine eigene Stimme!" | \
  piper --model de_DE-meinname-medium.onnx --output_file test.wav
```
