"""List PortAudio output devices, check PulseAudio support, and optionally play a test WAV file.

Enumerates all PortAudio output devices with their channel count, sample rate,
and host API.  Default devices are highlighted with loguru color markup.  When
a WAV file path is given as the first CLI argument, the user is prompted to
select an output device and the file is played back via ``sounddevice``.

Before listing devices the module checks whether the system's PortAudio build
includes PulseAudio support.  If not, a warning with rebuild instructions is
logged.

Example:
    Run as a standalone script::

        $ python -m sipstuff.snddevice_list
        $ python -m sipstuff.snddevice_list /tmp/test.wav
"""

from __future__ import annotations

import ctypes.util
import os
import subprocess
import sys
import threading
from typing import Any

import numpy as np
import numpy.typing as npt
import sounddevice as sd
import soundfile as sf
from loguru import logger

from sipstuff import configure_logging, print_banner


def check_portaudio_pulse_support() -> bool | None:
    """Check if the system's libportaudio is linked against libpulse.

    Runs ``ldd`` on the PortAudio shared library and searches for ``libpulse``
    in its output.

    Returns:
        ``True`` if PulseAudio support is detected, ``False`` if not, or
        ``None`` if the library could not be located or inspected.

    Note:
        When ``ctypes.util.find_library`` returns a short name that ``ldd``
        cannot resolve, the function falls back to well-known absolute paths
        (``/usr/lib/``, ``/usr/lib/x86_64-linux-gnu/``, ``/lib/x86_64-linux-gnu/``).
    """
    lib_path = ctypes.util.find_library("portaudio")
    if not lib_path:
        return None  # can't determine
    try:
        result = subprocess.run(["ldd", lib_path], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            # ldd needs full path, try common locations
            for candidate in [
                "/usr/lib/libportaudio.so.2",
                "/usr/lib/x86_64-linux-gnu/libportaudio.so.2",
                "/lib/x86_64-linux-gnu/libportaudio.so.2",
            ]:
                result = subprocess.run(["ldd", candidate], capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    break
            else:
                return None
        return "libpulse" in result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def main() -> None:
    """List PortAudio output devices and optionally play a WAV file on a chosen device.

    Initialises loguru logging, prints the sipstuff startup banner, then
    enumerates PortAudio output devices.  If a WAV file path is provided as
    ``sys.argv[1]``, prompts the user for a device number and plays the file
    using a ``sounddevice.OutputStream`` callback.

    Raises:
        SystemExit: With code 0 when no WAV path is given (device list only),
            or code 1 when the selected device is not a valid output device.
    """
    os.environ.setdefault("LOGURU_LEVEL", "INFO")
    configure_logging()
    print_banner()

    has_pulse: bool | None = check_portaudio_pulse_support()
    if has_pulse is False:
        api_names: list[str] = [api["name"] for api in sd.query_hostapis()]
        if "PulseAudio" not in api_names:
            logger.warning(
                "\u26a0  PortAudio wurde ohne PulseAudio-Support kompiliert (nur ALSA).\n"
                "   PipeWire/PulseAudio-Devices werden nicht angezeigt.\n"
                "\n"
                "   Fix: PortAudio mit PulseAudio-Backend neu bauen:\n"
                "\n"
                "     sudo apt install libpulse-dev cmake\n"
                "     git clone https://github.com/PortAudio/portaudio.git /tmp/portaudio\n"
                "     cd /tmp/portaudio\n"
                "     cmake -B build -DCMAKE_INSTALL_PREFIX=/usr -DPA_USE_PULSEAUDIO=ON -DPA_USE_ALSA=ON\n"
                "     cmake --build build -j$(nproc)\n"
                "     sudo cmake --install build\n"
                "     sudo ldconfig"
            )

    devices: sd.DeviceList = sd.query_devices()
    hostapis: tuple[dict[str, Any], ...] = sd.query_hostapis()
    default_out: int = sd.default.device[1]  # (input_default, output_default)
    api_defaults: set[int] = {api["default_output_device"] for api in hostapis if api["default_output_device"] >= 0}

    output_devices: list[int] = []
    for i, dev in enumerate(devices):
        if dev["max_output_channels"] > 0:
            output_devices.append(i)
            api_name: str = hostapis[dev["hostapi"]]["name"]
            if i == default_out:
                suffix = " <green>\u2714 DEFAULT</green>"
            elif i in api_defaults:
                suffix = f" <yellow>\u2714 {api_name} default</yellow>"
            else:
                suffix = ""
            logger.opt(colors=True).info(
                "[{:>2}] {:<45} ch: {:<3} sr: {:.0f}  [{}]" + suffix,
                i,
                dev["name"],
                dev["max_output_channels"],
                dev["default_samplerate"],
                api_name,
            )

    if len(sys.argv) < 2:
        logger.info("Usage: python snddevice_list.py <wav_file>  \u2014 to play a test file")
        sys.exit(0)

    wav_path: str = sys.argv[1]
    logger.info(f"WAV: {wav_path}")

    choice: str = input(f"Device-Nr [{default_out}]: ").strip()
    device_id: int = int(choice) if choice else default_out

    if device_id not in output_devices:
        logger.warning(f"Error: Device {device_id} ist kein Output-Device.")
        sys.exit(1)

    data: npt.NDArray[np.float32]
    samplerate: int
    data, samplerate = sf.read(wav_path, dtype="float32")
    if data.ndim == 1:
        data = data.reshape(-1, 1)
    dev_info: dict[str, Any] = sd.query_devices(device_id)
    logger.info(f"Spiele ab auf [{device_id}] {dev_info['name']} (sr: {samplerate}) ...")

    done: threading.Event = threading.Event()
    pos: int = 0

    def callback(outdata: npt.NDArray[np.float32], frames: int, time_info: object, status: sd.CallbackFlags) -> None:
        """Fill *outdata* with the next chunk of audio samples.

        Args:
            outdata: Output buffer to fill with audio data.
            frames: Number of frames requested.
            time_info: PortAudio timing information (cffi ``CData`` struct).
            status: Stream status flags.

        Raises:
            sd.CallbackStop: When all audio data has been consumed.
        """
        nonlocal pos
        remaining: int = len(data) - pos
        if remaining <= 0:
            outdata[:] = 0
            done.set()
            raise sd.CallbackStop
        chunk: int = min(frames, remaining)
        outdata[:chunk] = data[pos : pos + chunk]
        if chunk < frames:
            outdata[chunk:] = 0
        pos += chunk

    with sd.OutputStream(
        samplerate=samplerate,
        device=device_id,
        channels=data.shape[1],
        callback=callback,
        finished_callback=done.set,
    ):
        done.wait()

    logger.info("Fertig.")


if __name__ == "__main__":
    main()
