# PJSIP Stack – Asterisk + Realtime TTS Client

Podman-Setup mit einem Asterisk SIP-Server und einem PJSIP
Python-Client der eingehende Anrufe mit Piper TTS beantwortet.

## Architektur

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

Asterisk läuft über das `andrius/asterisk`-Image, der TTS-Client über
das lokal gebaute `xomoxcc/sipstuff:latest`-Image (`make build`).

## Schnellstart

### 1. sipstuff-Image bauen (Projekt-Root)

```bash
make build
```

### 2. Piper Voice-Modell herunterladen

```bash
mkdir -p piper-models
wget -P piper-models/ \
  https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/high/de_DE-thorsten-high.onnx
wget -P piper-models/ \
  https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/high/de_DE-thorsten-high.onnx.json
```

### 3. Stack starten

**Option A – podman kube play (via stack.sh):**

```bash
./stack.sh start
```

**Option B – podman-compose:**

```bash
podman-compose up -d
```

### 4. Testen

Softphone (Linphone, Zoiper, MicroSIP, ...) konfigurieren:

| Einstellung | Wert                       |
|-------------|----------------------------|
| Server      | IP deines Hosts            |
| Port        | 5060                       |
| Benutzer    | 1002                       |
| Passwort    | geheim1002                 |
| Transport   | UDP                        |

Dann **1001** anrufen → Der TTS-Client nimmt ab und spricht die Ansage.

### 5. Stack stoppen

```bash
# Option A
./stack.sh stop

# Option B
podman-compose down
```

## stack.sh Befehle

| Befehl               | Funktion                              |
|----------------------|---------------------------------------|
| `./stack.sh start`   | Pod starten (podman kube play)        |
| `./stack.sh stop`    | Pod entfernen (podman kube down)      |
| `./stack.sh logs`    | Logs aller Container anzeigen         |
| `./stack.sh logs asterisk` | Nur Asterisk-Logs               |
| `./stack.sh logs tts`| Nur TTS-Client-Logs                   |
| `./stack.sh status`  | Pod-Status anzeigen                   |
| `./stack.sh exec`    | Asterisk CLI öffnen                   |

## SIP-Accounts

| Extension | Passwort     | Zweck                          |
|-----------|-------------|--------------------------------|
| 1001      | geheim1001  | TTS-Client (auto-registriert)  |
| 1002      | geheim1002  | Externes Softphone zum Testen  |

## Sondernummern

| Nummer | Funktion                              |
|--------|---------------------------------------|
| 1001   | Anruf an TTS-Client (Anrufbeantworter)|
| 1002   | Anruf an externes Softphone           |
| *99    | Echo-Test (hört sich selbst)          |

## Konfiguration anpassen

### SIP-Credentials / TTS-Text ändern

Die SIP-Credentials, das Piper-Modell und der TTS-Text sind direkt in
`pjsip-stack.yaml` (bzw. `docker-compose.yml`) konfiguriert. Dort die
`args`-Sektion (YAML) oder `command` (Compose) anpassen.

Bei Änderung der SIP-Accounts auch `pjsip.conf` aktualisieren.

### Weitere SIP-Accounts anlegen

In `pjsip.conf` nach dem Muster der bestehenden Accounts neue Einträge
hinzufügen und in `extensions.conf` die Wählregeln ergänzen.

**Wichtig:** Endpoint-, Auth- und AOR-Sektionen müssen den gleichen Namen
tragen (z.B. alle `[1001]` mit unterschiedlichem `type=`). Die Asterisk
PJSIP-Registrierung extrahiert den Benutzernamen aus dem To-Header des
REGISTER-Requests und sucht eine AOR-Sektion mit diesem Namen. Stimmen die
Namen nicht überein (z.B. AOR heißt `1001-aors` statt `1001`), schlägt
die Registrierung mit `AOR '' not found` / HTTP 404 fehl.

### Anderes Piper-Modell verwenden

Verfügbare Stimmen: https://rhasspy.github.io/piper-samples/

```bash
# Beispiel: Englische Stimme
wget -P piper-models/ \
  https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/high/en_US-lessac-high.onnx
wget -P piper-models/ \
  https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/high/en_US-lessac-high.onnx.json
```

Dann in `pjsip-stack.yaml` den `--piper-model`-Pfad anpassen:

```yaml
- --piper-model
- /models/en_US-lessac-high.onnx
```

## Troubleshooting

### Firewall

Falls der Asterisk nicht aus dem LAN erreichbar ist:

```bash
# Firewalld
sudo firewall-cmd --add-port=5060/udp --permanent
sudo firewall-cmd --add-port=5060/tcp --permanent
sudo firewall-cmd --add-port=10000-10100/udp --permanent
sudo firewall-cmd --reload

# UFW
sudo ufw allow 5060/udp
sudo ufw allow 5060/tcp
sudo ufw allow 10000:10100/udp
```

## Dateistruktur

```
simulate_files/
├── stack.sh              # Wrapper-Script (podman kube play)
├── pjsip-stack.yaml      # Pod-Definition (Kubernetes YAML)
├── docker-compose.yml    # Alternative: podman-compose
├── pjsip.conf            # SIP-Accounts
├── extensions.conf       # Wählplan
├── rtp.conf              # RTP Port-Range
├── modules.conf          # Asterisk Module
├── piper-models/         # Piper Voice-Modelle (manuell downloaden)
│   ├── de_DE-thorsten-high.onnx
│   └── de_DE-thorsten-high.onnx.json
└── README.md
```
