# Probe-Tests für podman kube play

Minimaler Teststack zum Verifizieren, welche Probe-Typen mit `podman kube play`
und dem Asterisk-Image (`docker.io/andrius/asterisk:latest`) funktionieren.

```bash
$ podman inspect --format='{{.Name}} {{.Config.Healthcheck}}' probe-test-exec-asterisk probe-test-tcp-asterisk probe-test-http-asterisk
probe-test-exec-asterisk {[CMD asterisk -rx core show version] 15s 0s 10s 1s 3}
probe-test-tcp-asterisk {[CMD-SHELL nc -z -v localhost 5060 || exit 1] 15s 0s 10s 1s 3}
probe-test-http-asterisk {[CMD-SHELL curl -f http://localhost:8088/ || exit 1] 15s 0s 10s 1s 3}
```

## Hintergrund

| Probe-Typ | podman kube play | Kubernetes |
|---|---|---|
| `livenessProbe` | Unterstützt | Unterstützt |
| `startupProbe` | **Ignoriert** | Unterstützt |
| `readinessProbe` | **Nicht unterstützt** ([#12417](https://github.com/containers/podman/issues/12417)) | Unterstützt |

`tcpSocket` und `httpGet` werden von podman intern zu `exec` mit `nc` bzw. `curl`
konvertiert ([#18318](https://github.com/containers/podman/issues/18318)) — beide
Tools sind im Asterisk-Image nicht vorhanden, daher schlagen diese Probes fehl.

## Tests ausführen

```bash
# exec-Probe (erwartet: healthy)
podman kube play probe-test-exec.yaml

# tcpSocket-Probe (erwartet: unhealthy — nc fehlt im Image)
podman kube play probe-test-tcp.yaml

# httpGet-Probe (erwartet: unhealthy — curl fehlt im Image)
podman kube play probe-test-http.yaml
```

## Status prüfen

```bash
# Pod-Übersicht
podman pod ps

# Health-Status eines Containers
podman inspect --format='{{json .State.Health.Status}}' probe-test-exec-asterisk
podman inspect --format='{{json .State.Health.Status}}' probe-test-tcp-asterisk
podman inspect --format='{{json .State.Health.Status}}' probe-test-http-asterisk
```

## Erwartete Ergebnisse

| Datei | Probe-Typ | Erwartet |
|---|---|---|
| `probe-test-exec.yaml` | exec | **healthy** — `asterisk -rx "core show version"` funktioniert |
| `probe-test-tcp.yaml` | tcpSocket | **unhealthy** — podman konvertiert zu `nc`, das im Image fehlt |
| `probe-test-http.yaml` | httpGet | **unhealthy** — podman konvertiert zu `curl`, das im Image fehlt |

## Aufräumen

```bash
podman kube down probe-test-exec.yaml
podman kube down probe-test-tcp.yaml
podman kube down probe-test-http.yaml
```
