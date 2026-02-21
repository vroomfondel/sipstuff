# Eigene Stimme für Piper TTS erstellen – Komplettanleitung

> Diese Anleitung beschreibt den gesamten Workflow: vom Erstellen eines Trainingsdatensatzes über die Audioaufnahme mit einer GUI-Anwendung bis hin zum Finetuning und Export eines eigenen Piper-TTS-Sprachmodells.

---

## Inhaltsverzeichnis

1. [Überblick: Wie Piper TTS funktioniert](#1-überblick-wie-piper-tts-funktioniert)
2. [Zwei Wege zur eigenen Stimme](#2-zwei-wege-zur-eigenen-stimme)
3. [Daten vorbereiten: Das LJSpeech-Format](#3-daten-vorbereiten-das-ljspeech-format)
4. [Aufnahme-Setup: Audio-Hardware & EasyEffects](#4-aufnahme-setup-audio-hardware--easyeffects)
5. [Aufnahme-Tool: PySide6 GUI Recorder](#5-aufnahme-tool-pyside6-gui-recorder)
6. [Training: Finetuning eines bestehenden Modells](#6-training-finetuning-eines-bestehenden-modells)
7. [Training: Modell von Grund auf](#7-training-modell-von-grund-auf)
8. [ONNX-Export & die .onnx.json Konfiguration](#8-onnx-export--die-onnxjson-konfiguration)
9. [Tipps für gute Ergebnisse](#9-tipps-für-gute-ergebnisse)
10. [Nützliche Ressourcen](#10-nützliche-ressourcen)

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

## 3. Daten vorbereiten: Das LJSpeech-Format

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

### metadata.csv Format

Die Datei ist **Pipe-getrennt** (`|`), hat **keinen Header** und drei Spalten:

```
dateiname|rohe_transkription|normalisierte_transkription
```

- **Spalte 1**: Dateiname **ohne Pfad und ohne `.wav`-Endung**
- **Spalte 2**: Text so wie er roh vorliegt (mit Zahlen, Abkürzungen)
- **Spalte 3**: Text vollständig ausgeschrieben, so wie er gesprochen wird

### Beispiele

```
satz_0001|Am 3. Januar 2024 waren es -5 °C in Berlin.|Am dritten Januar zweitausendvierundzwanzig waren es minus fünf Grad Celsius in Berlin.
satz_0002|Dr. Müller hat ca. 250 Patienten pro Monat.|Doktor Müller hat circa zweihundertfünfzig Patienten pro Monat.
satz_0003|Die A7 ist mit 962,2 km die längste Autobahn Deutschlands.|Die A sieben ist mit neunhundertzweiundsechzig Komma zwei Kilometern die längste Autobahn Deutschlands.
```

Wenn der Text bereits ausgeschrieben ist, können Spalte 2 und 3 identisch sein:

```
satz_0050|Heute ist ein schöner Tag.|Heute ist ein schöner Tag.
```

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
- **Zahlen & Abkürzungen** (roh in Spalte 2, ausgeschrieben in Spalte 3)
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

## 6. Training: Finetuning eines bestehenden Modells

### Umgebung einrichten

```bash
git clone https://github.com/rhasspy/piper.git
cd piper/src/python
pip install -e .
```

### Pretrained Checkpoint herunterladen

Checkpoints gibt es unter: https://huggingface.co/rhasspy/piper-checkpoints

Für deutsches Finetuning eignet sich der **thorsten**-Checkpoint besonders gut.

### Preprocessing

```bash
python -m piper_train.preprocess \
  --language de \
  --input-dir /pfad/zu/deinem/dataset \
  --output-dir /pfad/zu/output \
  --dataset-format ljspeech \
  --sample-rate 22050
```

Dies erzeugt im Output-Verzeichnis u.a.:

```
/pfad/zu/output/
  config.json          ← Modell-Metadaten inkl. Phonem-Mapping
  phonemes.jsonl
  audio/
  ...
```

### Finetuning starten

```bash
python -m piper_train \
  --dataset-dir /pfad/zu/output \
  --accelerator gpu \
  --devices 1 \
  --batch-size 16 \
  --validation-split 0.05 \
  --max-epochs 1000 \
  --resume_from_checkpoint /pfad/zum/pretrained/checkpoint.ckpt \
  --precision 32
```

Der entscheidende Parameter ist `--resume_from_checkpoint` – damit wird das vortrainierte Modell als Startpunkt verwendet.

### Export nach ONNX

```bash
python -m piper_train.export_onnx \
  /pfad/zum/trainierten/checkpoint.ckpt \
  /pfad/zur/ausgabe.onnx
```

Beim Finetuning kann die `.onnx.json` des Basis-Modells einfach kopiert und wiederverwendet werden.

---

## 7. Training: Modell von Grund auf

Gleicher Ablauf wie Finetuning, aber **ohne** `--resume_from_checkpoint`:

```bash
python -m piper_train \
  --dataset-dir /pfad/zu/output \
  --accelerator gpu \
  --devices 1 \
  --batch-size 16 \
  --validation-split 0.05 \
  --max-epochs 5000 \
  --precision 32
```

Unterschiede zum Finetuning:

- Deutlich mehr Daten nötig (mehrere Stunden)
- Längere Trainingszeit
- Die `.onnx.json` wird automatisch beim ONNX-Export aus der `config.json` des Preprocessings generiert – muss nicht manuell erstellt werden

---

## 8. ONNX-Export & die .onnx.json Konfiguration

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
    "quality": "medium"
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

### Woher kommt die Datei?

| Methode | Quelle der .onnx.json |
|---------|----------------------|
| **Finetuning** | Vom Basis-Modell kopieren |
| **Von Grund auf** | Wird automatisch beim ONNX-Export aus der `config.json` erzeugt |

Die gesamte Pipeline (Preprocess → Train → Export) reicht die Konfiguration automatisch durch. Manuelle Anpassungen sind nur nötig, wenn man z.B. `length_scale` für schnelleres/langsameres Sprechen ändern will.

---

## 9. Tipps für gute Ergebnisse

### Aufnahmequalität

- **Audioqualität ist der wichtigste Faktor** – schlechte Aufnahmen kann kein Training retten
- Kein Hall, kein Rauschen, gleichmäßiger Pegel
- Konsistente Sprechweise: gleiches Tempo, gleiche Emotion
- 2–3 Testaufnahmen vor dem Start machen und mit Kopfhörern prüfen

### Training

- **GPU**: NVIDIA mit mindestens 8 GB VRAM, Finetuning geht auch mit 6 GB bei kleiner Batch-Size
- **Epochen**: Beim Finetuning reichen oft 500–2000 Epochen
- **Regelmäßig Samples generieren** und anhören, um Über-/Untertraining zu erkennen
- **Deutsche Modelle**: Als Basis für deutsches Finetuning den `thorsten`-Checkpoint verwenden

---

## 10. Nützliche Ressourcen

| Ressource | URL |
|-----------|-----|
| Piper GitHub | https://github.com/rhasspy/piper |
| Piper Training Docs | https://github.com/rhasspy/piper/blob/master/TRAINING.md |
| Piper Checkpoints | https://huggingface.co/rhasspy/piper-checkpoints |
| Piper Recording Studio | https://github.com/rhasspy/piper-recording-studio |
| EasyEffects | https://github.com/wwmm/easyeffects |

---

## Enthaltene Dateien

| Datei | Beschreibung |
|-------|--------------|
| `metadata.csv` | 250 deutsche Sätze im LJSpeech-Format, phonetisch vielfältig |
| `record_gui.py` | PySide6 GUI-Recorder mit VU-Meter, Wellenform, Fortschrittsbalken |
| `record_dataset.py` | Alternatives Terminal-basiertes Aufnahme-Tool |
| `piper-tts-recording.json` | EasyEffects Input-Preset für saubere Mikrofon-Aufnahmen |

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

# 5. Piper-Training vorbereiten
git clone https://github.com/rhasspy/piper.git
cd piper/src/python && pip install -e .

# 6. Preprocessing
python -m piper_train.preprocess \
  --language de \
  --input-dir /pfad/zu/mein-dataset \
  --output-dir /pfad/zu/output \
  --dataset-format ljspeech \
  --sample-rate 22050

# 7. Finetuning (mit vortrainiertem Checkpoint)
python -m piper_train \
  --dataset-dir /pfad/zu/output \
  --accelerator gpu \
  --devices 1 \
  --batch-size 16 \
  --max-epochs 1000 \
  --resume_from_checkpoint /pfad/zum/checkpoint.ckpt

# 8. ONNX-Export
python -m piper_train.export_onnx \
  /pfad/zum/trainierten/checkpoint.ckpt \
  /pfad/zur/meine-stimme.onnx

# 9. Testen
echo "Hallo, das ist meine eigene Stimme!" | \
  piper --model meine-stimme.onnx --output_file test.wav
```
