# TODO: PJSIP Telefonate simulieren

Zusammenfassung der Konversation zur Einrichtung einer PJSIP-Testumgebung mit Asterisk, Python und Piper TTS.

---

## 1. Testaufbau-Optionen (Überblick)

Es wurden mehrere Möglichkeiten besprochen, Python-basierte PJSIP-Telefonate zu simulieren:

| Ansatz | Beschreibung |
|--------|-------------|
| **PJSUA2 + Asterisk** | Zwei Python-Clients registrieren sich an einem lokalen SIP-Server |
| **Peer-to-Peer** | Zwei PJSUA2-Instanzen rufen sich direkt an (ohne Registrar) |
| **unittest.mock** | PJSUA2-Objekte mocken für Unit-Tests ohne SIP-Infra |
| **SIPp** | SIP-Traffic-Generator für Lasttests und Protokoll-Konformität |
| **Docker-Compose** | Asterisk + Caller + Callee als reproduzierbares Setup |

**Empfehlung:** Asterisk in Docker + PJSUA2-Python-Clients für Integrationstests, Mocking für Unit-Tests, SIPp für Lasttests.

---

## 2. Script 1: Einfacher Auto-Answer Client

**Datei:** `pjsip_autoanswer.py`

Funktionalität:
- Registriert sich via PJSUA2 an einem SIP-Server
- Wartet in einer Event-Loop auf eingehende Anrufe
- Nimmt Anrufe automatisch nach konfigurierbarer Verzögerung an
- Verbindet Audio (Mikrofon ↔ Lautsprecher)
- Unterstützt Null-Audio-Device für headless Betrieb

Konfiguration über `CONFIG`-Dict oder Umgebungsvariablen (`SIP_DOMAIN`, `SIP_USER`, `SIP_PASSWORD`, `SIP_PORT`).

---

## 3. Script 2: Auto-Answer mit WAV + Piper TTS

**Datei:** `pjsip_autoanswer_tts.py`

Erweiterung um Audio-Wiedergabe in den Call:

- `--mode wav`: Spielt eine WAV-Datei ab (automatische Konvertierung auf 16kHz Mono 16-bit)
- `--mode tts`: Synthetisiert Text mit **Piper TTS** in eine WAV-Datei, dann Wiedergabe
- Nutzt `AudioMediaPlayer` von PJSUA2 zum Einspeisen in den Call-Audio-Stream

Beispiel:
```bash
python pjsip_autoanswer_tts_n_wav.py --mode tts \
    --tts-text "Willkommen!" \
    --piper-model ./de_DE-thorsten-high.onnx
```

---

## 4. Script 3: Echtzeit-TTS mit Producer-Consumer Pattern

**Datei:** `pjsip_realtime_tts.py`

Architektur:
```
┌─────────────────┐   text_queue   ┌──────────────────┐   audio_queue   ┌──────────────┐
│  Konsole /      │───(Strings)───>│  PiperTTSProducer │───(PCM-Chunks)─>│ TTSMediaPort │──> Call
│  Anruf-Trigger  │                │  (eigener Thread) │                 │ (Consumer)   │
└─────────────────┘                └──────────────────┘                 └──────────────┘
```

Kernkomponenten:

- **PiperTTSProducer** (eigener Thread): Nimmt Text-Aufträge über `text_queue` entgegen, synthetisiert mit Piper in ein In-Memory-WAV, resampled auf 16kHz, zerlegt in 20ms-Chunks, schreibt PCM-Bytes in `audio_queue`
- **TTSMediaPort** (Custom `AudioMediaPort`): PJSIP ruft alle 20ms `onFrameRequested()` auf – liest nächsten Chunk aus Queue oder liefert Stille
- **Queue mit Backpressure**: `maxsize=500` (~10s Buffer)
- **`speak()` ist non-blocking**: Jederzeit neuen Text einspeisen möglich

Drei Modi:
```bash
# Feste Ansage
python pjsip_realtime_tts.py --tts-text "Willkommen!" --piper-model ./de_DE-thorsten-high.onnx

# Interaktiv (Text per Konsole eingeben → live gesprochen)
python pjsip_realtime_tts.py --interactive --piper-model ./de_DE-thorsten-high.onnx

# Beides kombiniert
python pjsip_realtime_tts.py --tts-text "Hallo!" --interactive --piper-model ./de_DE-thorsten-high.onnx
```

---

## 5. Podman Kube Play Stack

**Verzeichnis:** `pjsip-stack/`

Komplettes Deployment mit `podman kube play`:

### Architektur
```
  LAN (Softphone)                    Pod: pjsip-stack
  ┌──────────┐          ┌──────────────────────────────────────────┐
  │ Linphone │──SIP────>│  ┌───────────┐       ┌──────────────┐   │
  │ / Zoiper │   :5060  │  │ Asterisk  │──SIP──│ PJSIP TTS    │   │
  │          │<──RTP────│  │ SIP-Server│       │ Client       │   │
  │ Ext 1002 │  10000-  │  │           │       │ (Piper TTS)  │   │
  └──────────┘  10100   │  └───────────┘       │ Ext 1001     │   │
                        │                      └──────────────┘   │
                        └──────────────────────────────────────────┘
```

### Dateien

| Datei | Zweck |
|-------|-------|
| `pjsip-stack.yaml` | Podman Kube Play Definition (Pod mit 2 Containern, hostNetwork) |
| `Containerfile.asterisk` | Asterisk Image (Debian + Asterisk + deutsche Sounds) |
| `Containerfile.pjsip-tts` | TTS-Client Image (Python + pjsua2 + piper-tts) |
| `asterisk/conf/pjsip.conf` | SIP-Accounts (1001=TTS, 1002=Softphone) |
| `asterisk/conf/extensions.conf` | Wählplan (1001, 1002, *99=Echo-Test) |
| `asterisk/conf/rtp.conf` | RTP Ports 10000–10100 |
| `asterisk/conf/modules.conf` | chan_sip deaktiviert, nur PJSIP |
| `pjsip-tts/entrypoint.sh` | Wartet auf Asterisk, startet TTS-Client |

### SIP-Accounts

| Extension | Passwort | Zweck |
|-----------|----------|-------|
| 1001 | geheim1001 | TTS-Client (auto-registriert) |
| 1002 | geheim1002 | Externes Softphone |

### Schnellstart

```bash
# 1. Piper-Modell herunterladen
mkdir -p piper-models
wget -P piper-models/ https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/high/de_DE-thorsten-high.onnx
wget -P piper-models/ https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/high/de_DE-thorsten-high.onnx.json

# 2. Images bauen
podman build -t localhost/asterisk-sip:latest -f Containerfile.asterisk .
podman build -t localhost/pjsip-tts:latest   -f Containerfile.pjsip-tts .

# 3. Stack starten
podman kube play pjsip-stack.yaml

# 4. Testen: Softphone mit 1002/geheim1002 registrieren, dann 1001 anrufen

# 5. Stack stoppen
podman kube down pjsip-stack.yaml
```

### Firewall (falls nötig)

```bash
sudo firewall-cmd --add-port=5060/udp --permanent
sudo firewall-cmd --add-port=5060/tcp --permanent
sudo firewall-cmd --add-port=10000-10100/udp --permanent
sudo firewall-cmd --reload
```

### Debugging

```bash
# Logs
podman logs pjsip-stack-asterisk
podman logs pjsip-stack-pjsip-tts

# Asterisk CLI
podman exec -it pjsip-stack-asterisk asterisk -rvvv
# > pjsip show endpoints
# > pjsip show registrations
# > core show channels
```

---

## Abhängigkeiten

| Paket | Zweck |
|-------|-------|
| `pjsua2` | Python-Bindings für PJSIP |
| `piper-tts` | Offline Text-to-Speech |
| `onnxruntime` | Runtime für Piper-Modelle |
| Piper `.onnx` Modell | z.B. `de_DE-thorsten-high` (deutsche Stimme) |
| Podman | Container-Runtime |
| Asterisk | SIP-Server |

---

## Offene Punkte / Nächste Schritte

- [ ] Piper-Modell herunterladen und testen
- [ ] Container-Images bauen
- [ ] Stack starten und mit Softphone testen
- [ ] Ggf. STT (Speech-to-Text) ergänzen für bidirektionale Kommunikation
- [ ] Ggf. weitere SIP-Accounts anlegen
- [ ] TTS-Ansagetext finalisieren
- [ ] Firewall-Regeln auf dem Host konfigurieren
