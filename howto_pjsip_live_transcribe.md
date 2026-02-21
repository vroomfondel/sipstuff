# HowTo: pjsip_live_transcribe.py

## Was macht das Skript?

Das Skript baut über PJSIP/PJSUA2 einen SIP-Anruf auf und transkribiert die Gegenseite **in Echtzeit** mit [faster-whisper](https://github.com/SYSTRAN/faster-whisper). Zusätzlich kann es beide Seiten des Gesprächs als WAV aufnehmen und am Ende optional zusammenmischen.


## Voraussetzungen

### Python-Pakete

```bash
pip install faster-whisper numpy
```

### PJSUA2 Python-Bindings

PJSIP muss mit Python-Support kompiliert sein, sodass `import pjsua2` funktioniert. Eine Anleitung dazu findet sich in der [PJSIP-Dokumentation](https://docs.pjsip.org/).


## Schnellstart

Minimaler Aufruf – ruft eine SIP-URI an und transkribiert die Gegenseite:

```bash
python pjsip_live_transcribe.py --sip-uri sip:1234@pbx.example.com
```

Das Skript registriert sich an der PBX, baut den Call auf und gibt die Transkription live auf der Konsole aus. Beendet wird mit `Ctrl+C` oder wenn die Gegenseite auflegt.


## Konfiguration

### SIP-Zugangsdaten

Entweder direkt im Skript (Konstanten am Anfang) oder per CLI:

```bash
python pjsip_live_transcribe.py \
  --sip-uri sip:1234@pbx.example.com \
  --sip-domain pbx.example.com \
  --sip-user 1000 \
  --sip-pass geheim \
  --sip-port 5060
```

Hinter NAT kann `--public-ip` die öffentliche IP setzen.

### Whisper-Modell

```bash
# Verfügbare Modelle: tiny, base, small, medium, large-v3
python pjsip_live_transcribe.py --sip-uri ... \
  --whisper-model small \
  --device cuda \
  --language de
```

| Modell      | Genauigkeit | Geschwindigkeit | VRAM (ca.) |
|-------------|-------------|-----------------|------------|
| `tiny`      | gering      | sehr schnell    | ~1 GB      |
| `base`      | okay        | schnell         | ~1 GB      |
| `small`     | gut         | mittel          | ~2 GB      |
| `medium`    | sehr gut    | langsam         | ~5 GB      |
| `large-v3`  | am besten   | am langsamsten  | ~10 GB     |

Ohne `--language` erkennt Whisper die Sprache automatisch. Für deutsche Gespräche empfiehlt sich `--language de`, da es Fehlerkennungen vermeidet.

### VAD / Chunking-Parameter

Die Voice Activity Detection steuert, wann ein Audio-Chunk zur Transkription geschickt wird:

```bash
python pjsip_live_transcribe.py --sip-uri ... \
  --silence-threshold 0.01 \
  --silence-trigger 0.3 \
  --max-chunk 5.0 \
  --min-chunk 0.5
```

| Parameter              | Default | Beschreibung                                                |
|------------------------|---------|-------------------------------------------------------------|
| `--silence-threshold`  | `0.01`  | RMS-Wert unter dem ein Frame als "Stille" gilt              |
| `--silence-trigger`    | `0.3`   | Sekunden Stille nach Sprache → Chunk wird transkribiert     |
| `--max-chunk`          | `5.0`   | Harte Obergrenze: spätestens nach X Sek. transkribieren     |
| `--min-chunk`          | `0.5`   | Chunks kürzer als X Sek. werden verworfen (Micro-Chunks)    |


## Audio-Aufnahme

### Nur Gegenseite aufnehmen (Standard)

Standardmäßig wird das empfangene Audio (RX) als WAV gespeichert:

```bash
python pjsip_live_transcribe.py --sip-uri ...
# → call_20260218_143000_rx.wav
```

### Auch eigenes Mikrofon aufnehmen

Mit `--record-tx` wird zusätzlich das gesendete Audio (TX) in eine separate Datei geschrieben:

```bash
python pjsip_live_transcribe.py --sip-uri ... --record-tx
# → call_20260218_143000_rx.wav  (Gegenseite)
# → call_20260218_143000_tx.wav  (eigenes Mikrofon)
```

### RX + TX zusammenmischen

Mit `--mix-mode` werden nach dem Call beide Spuren zu einer Datei kombiniert. Das impliziert automatisch `--record-tx`:

```bash
# Mono-Mix: beide Spuren addiert, normalisiert
python pjsip_live_transcribe.py --sip-uri ... --mix-mode mono
# → call_..._rx.wav + call_..._tx.wav + call_..._mix.wav

# Stereo: RX auf linkem Kanal, TX auf rechtem Kanal
python pjsip_live_transcribe.py --sip-uri ... --mix-mode stereo
# → call_..._rx.wav + call_..._tx.wav + call_..._stereo.wav
```

| `--mix-mode` | Ergebnis                                                      |
|--------------|---------------------------------------------------------------|
| `none`       | Kein Mischen (Default). Nur Einzeldateien.                    |
| `mono`       | Beide Spuren zu einer Mono-WAV addiert (mit Normalisierung).  |
| `stereo`     | Stereo-WAV: Links = Gegenseite (RX), Rechts = Mikrofon (TX). |

### Weitere Optionen

```bash
# Eigenen Dateinamen / Verzeichnis wählen
--wav-output /pfad/zur/datei.wav   # nur RX-Pfad
--wav-dir /pfad/zum/verzeichnis/

# Gar keine WAV-Aufnahme
--no-wav
```


## Konsolenausgabe

Während des Calls erscheinen transkribierte Segmente mit Zeitcodes:

```
============================================================
  LIVE-TRANSKRIPTION
============================================================

  [14:30:02.3–14:30:05.1] (00:02.30–00:05.10)  Hallo, hier ist Herr Müller.
  [14:30:06.0–14:30:09.8] (00:06.00–00:09.80)  Ich rufe wegen der Rechnung an.
```

Die erste Klammer zeigt die absolute Uhrzeit, die zweite die relative Position seit Call-Start.


## Wie die Transkriptions-Pipeline funktioniert

Das Skript arbeitet mit einem Producer-Consumer-Pattern über drei Stufen:

1. **Audio-Empfang:** PJSIP liefert alle 20ms einen PCM-Frame über den `TranscriptionPort`. Dieser schiebt die Bytes in den `VADAudioBuffer` und parallel in den WAV-Recorder.

2. **VAD-Chunking:** Der `VADAudioBuffer` analysiert jedes 10ms-Fenster per RMS. Erkennt er nach Sprache eine Pause (300ms Stille) oder erreicht das Maximum (5s), schneidet er einen Chunk ab und signalisiert per `threading.Event`.

3. **Transkription:** Der `TranscriptionThread` läuft als Daemon-Thread. Er lädt das Whisper-Modell einmalig beim Start und wartet dann in einer Schleife auf fertige Chunks. Sobald ein Chunk bereitsteht, wird `model.transcribe()` aufgerufen – das Modell bleibt persistent im Speicher.

Für die TX-Aufnahme (eigenes Mikrofon) hängt sich ein separater `CapturePort` an das Capture-Device von PJSIP und schreibt die Frames in einen eigenen `WavRecorder`. Das Mischen erfolgt erst nach dem Call, indem beide WAV-Dateien eingelesen, auf gleiche Länge gebracht und dann addiert (Mono) bzw. interleaved (Stereo) werden.


## Vollständige Optionsübersicht

```
python pjsip_live_transcribe.py --help
```

| Option                 | Default              | Beschreibung                          |
|------------------------|----------------------|---------------------------------------|
| `--sip-uri`            | *(pflicht)*          | SIP-Ziel-URI                          |
| `--sip-domain`         | `pbx.example.com`   | SIP-Domain / Registrar                |
| `--sip-user`           | `1000`               | SIP-Benutzername                      |
| `--sip-pass`           | `geheim`             | SIP-Passwort                          |
| `--sip-port`           | `5060`               | Lokaler SIP-Port                      |
| `--public-ip`          | *(leer)*             | Öffentliche IP für NAT                |
| `--whisper-model`      | `base`               | Whisper-Modellgröße                   |
| `--device`             | `cpu`                | `cpu` oder `cuda`                     |
| `--language`           | *(auto)*             | Sprache erzwingen (z.B. `de`, `en`)   |
| `--silence-threshold`  | `0.01`               | RMS-Schwellwert für Stille            |
| `--silence-trigger`    | `0.3`                | Sek. Stille bis Transkription         |
| `--max-chunk`          | `5.0`                | Max. Chunk-Dauer in Sekunden          |
| `--min-chunk`          | `0.5`                | Min. Chunk-Dauer in Sekunden          |
| `--wav-output`         | *(auto)*             | Eigener Pfad für RX-WAV              |
| `--wav-dir`            | `.`                  | Verzeichnis für WAV-Dateien           |
| `--no-wav`             | `false`              | Keine WAV-Aufnahme                    |
| `--record-tx`          | `false`              | TX-Audio (Mikrofon) aufnehmen         |
| `--mix-mode`           | `none`               | `none`, `mono` oder `stereo`          |
