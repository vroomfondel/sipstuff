#!/usr/bin/env python3
"""
CLI tool for recording Piper TTS training datasets from a metadata.csv file.

Reads sentences from a LJSpeech pipe-delimited metadata.csv file one by one,
displays each sentence in the terminal, and records audio while the user holds
the spacebar. After each recording the clip is played back automatically and the
user can re-record, play again, skip, or quit. Keyboard input is captured via a
global pynput listener so it works even when the terminal has focus.

Dependencies:
    pip install sounddevice soundfile pynput

Usage:
    python record_dataset.py --metadata metadata.csv --output ./wavs --start 1

Options:
    --metadata   Path to the metadata.csv file (default: metadata.csv)
    --output     Output directory for WAV files (default: ./wavs)
    --start      1-based starting line number (default: 1)
    --samplerate Sample rate in Hz (default: 22050)
    --channels   Number of channels, 1=mono (default: 1)
    --device     Audio input device index (default: system default)
"""

import argparse
import os
import queue
import sys
import threading
import time
from typing import Optional

import numpy as np
import sounddevice as sd
import soundfile as sf
from pynput import keyboard

# ─── Globaler State für Tastatur-Listener ───────────────────────────────────

space_pressed = threading.Event()
space_released = threading.Event()
_listener: Optional[keyboard.Listener] = None


def _on_press(key: Optional[keyboard.Key]) -> None:
    """pynput callback: set the space_pressed event when the spacebar is pressed."""
    if key == keyboard.Key.space:
        space_pressed.set()
        space_released.clear()


def _on_release(key: Optional[keyboard.Key]) -> None:
    """pynput callback: clear space_pressed and set space_released when the spacebar is released."""
    if key == keyboard.Key.space:
        space_pressed.clear()
        space_released.set()


def start_keyboard_listener() -> None:
    """Start the global pynput keyboard listener as a daemon thread (non-blocking)."""
    global _listener
    _listener = keyboard.Listener(on_press=_on_press, on_release=_on_release)
    _listener.daemon = True
    _listener.start()


def stop_keyboard_listener() -> None:
    """Stop the global pynput keyboard listener if it is running."""
    global _listener
    if _listener:
        _listener.stop()


# ─── Aufnahme ───────────────────────────────────────────────────────────────


def record_while_space_held(samplerate: int, channels: int, device: Optional[int] = None) -> list[np.ndarray]:
    """Record audio from the microphone for as long as the spacebar is held.

    Blocks until the spacebar is pressed, then opens an audio input stream and
    collects PCM blocks until the spacebar is released.

    Args:
        samplerate: Sample rate in Hz for the input stream.
        channels: Number of input channels (1 = mono).
        device: sounddevice input device index, or None for the system default.

    Returns:
        A list of numpy int16 arrays, one per audio block captured. Empty if the
        stream produced no data before the key was released.
    """
    audio_chunks: list[np.ndarray] = []
    blocksize = 1024

    def callback(indata: np.ndarray, frames: int, time_info: sd.CallbackFlags, status: sd.CallbackFlags) -> None:
        if status:
            print(f"  ⚠ Audio-Status: {status}", file=sys.stderr)
        audio_chunks.append(indata.copy())

    # Warte, bis Leertaste gedrückt wird
    space_released.clear()
    space_pressed.clear()

    print("  🎤 [LEERTASTE] gedrückt halten zum Aufnehmen ...", end="", flush=True)
    space_pressed.wait()

    print("\r  🔴 Aufnahme läuft ... (Leertaste loslassen zum Stoppen)    ", end="", flush=True)

    stream = sd.InputStream(
        samplerate=samplerate,
        channels=channels,
        dtype="int16",
        blocksize=blocksize,
        device=device,
        callback=callback,
    )

    with stream:
        space_released.wait()

    duration = len(audio_chunks) * blocksize / samplerate
    print(f"\r  ✅ Aufnahme beendet. Dauer: {duration:.1f}s                      ")

    return audio_chunks


def save_wav(chunks: list[np.ndarray], filepath: str, samplerate: int) -> None:
    """Concatenate audio chunks and write them to a 16-bit PCM WAV file.

    Args:
        chunks: List of numpy int16 arrays as returned by record_while_space_held.
        filepath: Destination path for the WAV file.
        samplerate: Sample rate in Hz to embed in the WAV header.
    """
    audio_data = np.concatenate(chunks, axis=0)
    sf.write(filepath, audio_data, samplerate, subtype="PCM_16")


def play_wav(filepath: str) -> None:
    """Play a WAV file synchronously via sounddevice, blocking until playback is complete.

    Args:
        filepath: Path to the WAV file to play.
    """
    data, sr = sf.read(filepath, dtype="int16")
    print("  🔊 Wiedergabe ...", end="", flush=True)
    sd.play(data, sr)
    sd.wait()
    print(" fertig.")


# ─── Metadata lesen ─────────────────────────────────────────────────────────


def read_metadata(filepath: str) -> list[tuple[str, str, str]]:
    """Read a LJSpeech-format metadata.csv file into a list of tuples.

    Each non-empty line is expected to contain at least three pipe-delimited fields:
    ``filename|raw_text|normalized_text``. Lines with fewer than three fields are
    skipped with a warning printed to stdout.

    Args:
        filepath: Path to the metadata.csv file.

    Returns:
        A list of ``(filename, raw_text, normalized_text)`` tuples, one per valid line.
    """
    entries: list[tuple[str, str, str]] = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) < 3:
                print(f"  ⚠ Zeile {line_num} übersprungen (falsches Format): {line[:60]}")
                continue
            entries.append((parts[0], parts[1], parts[2]))
    return entries


# ─── Hilfsfunktionen ────────────────────────────────────────────────────────


def clear_space_events() -> None:
    """Clear both spacebar threading events and sleep briefly to flush ghost key events."""
    space_pressed.clear()
    space_released.clear()
    time.sleep(0.3)


def show_progress(current: int, total: int) -> str:
    """Build a Unicode block progress bar string with a percentage label.

    Args:
        current: Number of items completed so far (1-based).
        total: Total number of items.

    Returns:
        A formatted string such as ``[████████░░░░░░░░░░░░░░░░░░░░░░] 8/30 (27%)``.
    """
    pct = current / total * 100
    bar_len = 30
    filled = int(bar_len * current / total)
    bar = "█" * filled + "░" * (bar_len - filled)
    return f"[{bar}] {current}/{total} ({pct:.0f}%)"


def list_audio_devices() -> None:
    """Print all available audio input devices with their channel count and default sample rate."""
    print("\n🎙  Verfügbare Eingabegeräte:")
    print("-" * 50)
    devices = sd.query_devices()
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0:
            marker = " ← Standard" if i == sd.default.device[0] else ""
            print(f"  [{i}] {dev['name']} ({dev['max_input_channels']}ch, {int(dev['default_samplerate'])}Hz){marker}")
    print("-" * 50)


# ─── Hauptprogramm ──────────────────────────────────────────────────────────


def main() -> None:
    """Parse CLI arguments and run the interactive dataset recording session.

    Loads entries from metadata.csv, starts the keyboard listener, and iterates
    through each sentence starting at the specified line. After each recording the
    user can confirm, re-record, play back, skip, or quit via a simple menu prompt.
    """
    parser = argparse.ArgumentParser(
        description="Piper TTS Dataset Recorder – Nimmt Sätze aus metadata.csv auf.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--metadata", type=str, default="metadata.csv", help="Pfad zur metadata.csv (default: metadata.csv)"
    )
    parser.add_argument("--output", type=str, default="./wavs", help="Ausgabeordner für WAV-Dateien (default: ./wavs)")
    parser.add_argument("--start", type=int, default=1, help="Startzeile, 1-basiert (default: 1)")
    parser.add_argument("--samplerate", type=int, default=22050, help="Samplerate in Hz (default: 22050)")
    parser.add_argument("--channels", type=int, default=1, help="Kanäle, 1=Mono (default: 1)")
    parser.add_argument("--device", type=int, default=None, help="Audio-Eingabegerät ID (default: System-Standard)")
    parser.add_argument("--list-devices", action="store_true", help="Verfügbare Audiogeräte anzeigen und beenden")

    args = parser.parse_args()

    # Geräte auflisten
    if args.list_devices:
        list_audio_devices()
        sys.exit(0)

    # Metadata laden
    if not os.path.isfile(args.metadata):
        print(f"❌ Datei nicht gefunden: {args.metadata}")
        sys.exit(1)

    entries = read_metadata(args.metadata)
    total = len(entries)

    if total == 0:
        print("❌ Keine Einträge in der Metadata-Datei gefunden.")
        sys.exit(1)

    # Start-Index validieren
    start_idx = args.start - 1  # 0-basiert
    if start_idx < 0 or start_idx >= total:
        print(f"❌ Startzeile {args.start} ungültig. Datei hat {total} Einträge (1-{total}).")
        sys.exit(1)

    # Ausgabeordner erstellen
    os.makedirs(args.output, exist_ok=True)

    # Bereits aufgenommene Dateien zählen
    existing = sum(1 for e in entries if os.path.isfile(os.path.join(args.output, e[0] + ".wav")))

    # Info anzeigen
    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║        🎙  Piper TTS Dataset Recorder  🎙        ║")
    print("╠══════════════════════════════════════════════════╣")
    print(f"║  Metadata:    {args.metadata:<35}║")
    print(f"║  Ausgabe:     {args.output:<35}║")
    print(f"║  Samplerate:  {args.samplerate} Hz{' ' * 27}║")
    print(f"║  Kanäle:      {'Mono' if args.channels == 1 else 'Stereo':<35}║")
    print(
        f"║  Einträge:    {total} gesamt, {existing} bereits aufgenommen{' ' * (10 - len(str(total)) - len(str(existing)))}║"
    )
    print(f"║  Start bei:   Zeile {args.start:<29}║")
    print("╠══════════════════════════════════════════════════╣")
    print("║  Steuerung:                                     ║")
    print("║    [LEERTASTE] halten  →  Aufnehmen             ║")
    print("║    [W] + Enter          →  Wiedergabe            ║")
    print("║    [N] + Enter          →  Nochmal aufnehmen     ║")
    print("║    [Enter]              →  OK, weiter            ║")
    print("║    [S] + Enter          →  Überspringen          ║")
    print("║    [Q] + Enter          →  Beenden               ║")
    print("╚══════════════════════════════════════════════════╝")
    print()

    # Audio-Device testen
    try:
        dev_info = sd.query_devices(args.device or sd.default.device[0], "input")
        print(f"🎙  Eingabegerät: {dev_info['name']}")
    except Exception as e:
        print(f"❌ Audio-Gerät Fehler: {e}")
        print("   Nutze --list-devices um verfügbare Geräte zu sehen.")
        sys.exit(1)

    print()
    input("Drücke [Enter] um zu beginnen ...")
    print()

    # Keyboard-Listener starten
    start_keyboard_listener()

    try:
        i = start_idx
        while i < total:
            filename, raw_text, norm_text = entries[i]
            wav_path = os.path.join(args.output, filename + ".wav")
            already_exists = os.path.isfile(wav_path)

            # Header
            print(f"{'─' * 60}")
            print(f"  {show_progress(i + 1, total)}")
            print()
            print(f"  📄 Datei:       {filename}.wav {'(existiert bereits ⚠)' if already_exists else ''}")
            print(f"  📝 Roh:         {raw_text}")
            print()
            print(f"  🗣  VORLESEN:    \033[1;33m{norm_text}\033[0m")
            print()

            # Aufnahme-Schleife für diesen Satz
            recorded = False
            while True:
                if not recorded:
                    # Aufnehmen
                    clear_space_events()
                    chunks = record_while_space_held(
                        samplerate=args.samplerate,
                        channels=args.channels,
                        device=args.device,
                    )

                    if not chunks:
                        print("  ⚠ Keine Audio-Daten aufgenommen. Nochmal versuchen.")
                        continue

                    save_wav(chunks, wav_path, args.samplerate)
                    recorded = True
                    print(f"  💾 Gespeichert: {wav_path}")
                    print()

                    # Automatisch abspielen nach Aufnahme
                    time.sleep(0.3)
                    play_wav(wav_path)
                    print()

                # Menü
                print("  Was möchtest du tun?")
                print("    [Enter] = OK, weiter  |  [N] = Nochmal  |  [W] = Wiedergabe  |  [Q] = Beenden")
                choice = input("  > ").strip().lower()

                if choice == "":
                    # OK, nächster Satz
                    print()
                    i += 1
                    break
                elif choice == "n":
                    # Nochmal aufnehmen
                    print()
                    recorded = False
                    continue
                elif choice == "w":
                    # Wiedergabe
                    if os.path.isfile(wav_path):
                        play_wav(wav_path)
                    else:
                        print("  ⚠ Noch keine Aufnahme vorhanden.")
                    print()
                    continue
                elif choice == "s":
                    # Überspringen
                    print("  ⏭  Übersprungen.")
                    print()
                    i += 1
                    break
                elif choice == "q":
                    print()
                    print(f"  🛑 Beendet bei Zeile {i + 1}.")
                    print(f"     Fortsetzen mit: python {sys.argv[0]} --start {i + 1}")
                    print()
                    return
                else:
                    print("  ⚠ Ungültige Eingabe. Bitte Enter, N, W, S oder Q eingeben.")
                    continue

        # Fertig
        print("─" * 60)
        print()
        print("  🎉 Alle Sätze aufgenommen!")
        print(f"  📁 WAV-Dateien in: {os.path.abspath(args.output)}")
        print()

    except KeyboardInterrupt:
        print()
        print(f"\n  🛑 Abgebrochen bei Zeile {i + 1}.")
        print(f"     Fortsetzen mit: python {sys.argv[0]} --start {i + 1}")
        print()
    finally:
        stop_keyboard_listener()


if __name__ == "__main__":
    main()
