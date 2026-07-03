from __future__ import annotations

import argparse
import csv
import math
import sys
import tempfile
import threading
import time
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # pragma: no cover - used only on systems without Tk.
    tk = None
    filedialog = None
    messagebox = None
    ttk = None

try:
    import winsound
except ImportError:  # pragma: no cover - non-Windows fallback.
    winsound = None

try:
    import miniaudio
except ImportError:  # pragma: no cover - MP3 support is optional for source runs.
    miniaudio = None


ADPCMA_RATE = 18500
ADPCMB_MIN_RATE = 1850
ADPCMB_MAX_RATE = 55555
ADPCMB_BASE_RATE = 8_000_000 / 144

ADPCMA_STEP_SIZE = [
    16, 17, 19, 21, 23, 25, 28, 31, 34, 37,
    41, 45, 50, 55, 60, 66, 73, 80, 88, 97,
    107, 118, 130, 143, 157, 173, 190, 209, 230, 253,
    279, 307, 337, 371, 408, 449, 494, 544, 598, 658,
    724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552,
]
ADPCMA_STEP_ADJ = [-1, -1, -1, -1, 2, 5, 7, 9, -1, -1, -1, -1, 2, 5, 7, 9]
ADPCMB_STEP_SCALE = [57, 57, 57, 57, 77, 102, 128, 153, 57, 57, 57, 57, 77, 102, 128, 153]

RAW_EXTENSIONS = {"ADPCM-A": ".adpcma", "ADPCM-B": ".adpcmb"}
WAV_SUFFIXES = {"ADPCM-A": "_adpcma.wav", "ADPCM-B": "_adpcmb.wav"}
AUDIO_EXTENSIONS = {".wav", ".mp3"}
OUTPUT_FORMATS = {
    "WAV": (True, False),
    "Raw NeoGeo": (False, True),
    "WAV + Raw NeoGeo": (True, True),
}

PRESETS = {
    "Voice": {"b_rate": 11025, "low_pass": 5200, "normalize": True},
    "Low": {"b_rate": 16000, "low_pass": 6800, "normalize": True},
    "Medium": {"b_rate": 22050, "low_pass": 8000, "normalize": True},
    "High": {"b_rate": 33075, "low_pass": 12000, "normalize": True},
    "Max": {"b_rate": 44100, "low_pass": 16000, "normalize": True},
}

HELP_TEXT = """ADPCM-A/B WAV Converter Help

This tool converts one WAV/MP3 file or a whole audio folder through NeoGeo YM2610
ADPCM-A or ADPCM-B processing.

Input
- Audio folder: converts every WAV or MP3 file in the selected folder.
- Single audio file: converts only the selected WAV or MP3 file.
- Recursive subfolder scan: includes WAV and MP3 files in subfolders. If the source
  output option is enabled, the output folder is skipped automatically.

Output
- Output folder: destination folder for converted files.
- Use source folder and create an output subfolder: writes to an "output"
  folder next to the selected source and disables manual output selection.
- Output format:
  WAV creates audible decoded WAV files.
  Raw NeoGeo creates .adpcma or .adpcmb raw sample data.
  WAV + Raw NeoGeo creates both file types.

Codec
- ADPCM-A is the fixed-rate NeoGeo sample format. The app resamples to 18500 Hz.
- ADPCM-B is the variable-rate NeoGeo sample format. Use Sample rate to choose
  the target playback rate. The log prints the matching Delta-N value.

Quality Controls
- Preset: quick sample-rate and low-pass choices.
- Sample rate: target ADPCM-B rate. ADPCM-A is fixed.
- Volume dB: gain applied after normalization.
- Max duration s: cuts samples longer than this value.
- Fade-out on trim: fades the end when trailing trim or max duration is used.
- Low-pass pre-encode: reduces high frequencies before ADPCM compression.
- Trim leading/trailing silence: removes quiet audio edges.
- Silence threshold dBFS: level used by the silence trimmer.
- Normalize to -1 dBFS: raises peak level safely before volume gain.

Actions
- Preview: converts the first selected audio file to ADPCM and decodes it to a
  temporary WAV for listening.
- Convert: starts the batch conversion.
- Stop: requests cancellation after the current file finishes.
- Close: exits the app.

Each conversion run writes adpcm_manifest.csv in the output folder with paths,
codec settings, output sizes, sample rates, and status."""


@dataclass
class AudioData:
    sample_rate: int
    samples: list[int]


@dataclass
class ConversionOptions:
    codec: str = "ADPCM-B"
    preset: str = "Medium"
    sample_rate: int = 22050
    volume_db: float = 0.0
    max_duration_s: float | None = None
    low_pass_enabled: bool = False
    low_pass_hz: float = 8000.0
    trim_leading: bool = False
    trim_trailing: bool = False
    normalize: bool = False
    fade_on_trim: bool = False
    fade_out_s: float = 3.0
    silence_threshold_dbfs: float = -45.0
    recursive: bool = False
    write_wav: bool = True
    write_raw: bool = False


@dataclass
class ProcessInfo:
    source_rate: int
    target_rate: int
    source_samples: int
    processed_samples: int
    padded_samples: int
    trimmed_leading_samples: int
    trimmed_trailing_samples: int
    truncated: bool
    pad_bytes: int


@dataclass
class ConversionResult:
    input_path: Path
    wav_output_path: Path | None
    raw_output_path: Path | None
    codec: str
    target_rate: int
    source_rate: int
    source_duration_s: float
    processed_duration_s: float
    padded_duration_s: float
    encoded_bytes: int
    pad_bytes: int
    delta_n: int | None


def clamp(value: int | float, low: int, high: int) -> int:
    return max(low, min(high, int(round(value))))


def clamp16(value: int | float) -> int:
    return clamp(value, -32768, 32767)


def build_adpcma_table() -> list[int]:
    table: list[int] = []
    for step in ADPCMA_STEP_SIZE:
        for nibble in range(16):
            value = (2 * (nibble & 0x07) + 1) * step // 8
            table.append(-value if (nibble & 0x08) else value)
    return table


ADPCMA_TABLE = build_adpcma_table()


def read_wav_pcm(path: Path) -> AudioData:
    with wave.open(str(path), "rb") as wav:
        if wav.getcomptype() != "NONE":
            raise ValueError("Compressed WAV files are not supported")
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        frame_count = wav.getnframes()
        data = wav.readframes(frame_count)

    if channels < 1:
        raise ValueError("WAV file has no audio channels")

    samples = decode_pcm_bytes(data, sample_width)
    if len(samples) % channels:
        samples = samples[: len(samples) - (len(samples) % channels)]
    if channels > 1:
        samples = mix_to_mono(samples, channels)
    return AudioData(sample_rate=sample_rate, samples=samples)


def read_audio_file(path: Path) -> AudioData:
    suffix = path.suffix.lower()
    if suffix == ".wav":
        return read_wav_pcm(path)
    if suffix == ".mp3":
        return read_mp3_pcm(path)
    raise ValueError(f"Unsupported input file type: {path.suffix}")


def read_mp3_pcm(path: Path) -> AudioData:
    if miniaudio is None:
        raise ValueError("MP3 input requires the miniaudio package. Use the EXE build or install it with: python -m pip install miniaudio")

    try:
        info = miniaudio.get_file_info(str(path))
        sample_rate = int(info.sample_rate) if info.sample_rate else 44100
        decoded = miniaudio.decode_file(
            str(path),
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=1,
            sample_rate=sample_rate,
        )
    except Exception as exc:
        raise ValueError(f"Could not decode MP3 file: {exc}") from exc

    samples = decoded.samples.tolist() if hasattr(decoded.samples, "tolist") else list(decoded.samples)
    return AudioData(sample_rate=sample_rate, samples=[clamp16(sample) for sample in samples])


def decode_pcm_bytes(data: bytes, sample_width: int) -> list[int]:
    if sample_width == 1:
        return [(byte - 128) << 8 for byte in data]

    if sample_width == 2:
        pcm = array("h")
        pcm.frombytes(data)
        if sys.byteorder != "little":
            pcm.byteswap()
        return pcm.tolist()

    if sample_width == 3:
        samples: list[int] = []
        for index in range(0, len(data) - 2, 3):
            raw = data[index] | (data[index + 1] << 8) | (data[index + 2] << 16)
            if raw & 0x800000:
                raw -= 0x1000000
            samples.append(clamp16(raw >> 8))
        return samples

    if sample_width == 4:
        pcm = array("i")
        pcm.frombytes(data)
        if sys.byteorder != "little":
            pcm.byteswap()
        return [clamp16(value >> 16) for value in pcm]

    raise ValueError(f"Unsupported WAV sample width: {sample_width * 8} bit")


def mix_to_mono(samples: list[int], channels: int) -> list[int]:
    mono: list[int] = []
    for index in range(0, len(samples), channels):
        frame = samples[index:index + channels]
        mono.append(clamp16(sum(frame) / len(frame)))
    return mono


def write_wav_pcm(path: Path, sample_rate: int, samples: Iterable[int]) -> None:
    pcm = array("h", (clamp16(sample) for sample in samples))
    if sys.byteorder != "little":
        pcm.byteswap()
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.tobytes())


def trim_silence(samples: list[int], threshold_dbfs: float, trim_leading: bool, trim_trailing: bool) -> tuple[list[int], int, int]:
    if not samples or (not trim_leading and not trim_trailing):
        return samples, 0, 0

    threshold = 32767 * (10 ** (threshold_dbfs / 20))
    start = 0
    end = len(samples)

    if trim_leading:
        while start < end and abs(samples[start]) <= threshold:
            start += 1

    if trim_trailing:
        while end > start and abs(samples[end - 1]) <= threshold:
            end -= 1

    if start >= end:
        return [0], start, len(samples) - end

    return samples[start:end], start, len(samples) - end


def apply_gain(samples: list[int], gain: float) -> list[int]:
    if gain == 1.0:
        return samples
    return [clamp16(sample * gain) for sample in samples]


def normalize_to_dbfs(samples: list[int], target_dbfs: float = -1.0) -> list[int]:
    peak = max((abs(sample) for sample in samples), default=0)
    if peak <= 0:
        return samples
    target_peak = 32767 * (10 ** (target_dbfs / 20))
    return apply_gain(samples, target_peak / peak)


def low_pass_filter(samples: list[int], sample_rate: int, cutoff_hz: float) -> list[int]:
    if not samples or cutoff_hz <= 0 or cutoff_hz >= sample_rate / 2:
        return samples

    rc = 1.0 / (2.0 * math.pi * cutoff_hz)
    dt = 1.0 / sample_rate
    alpha = dt / (rc + dt)
    output: list[int] = []
    current = float(samples[0])
    for sample in samples:
        current += alpha * (sample - current)
        output.append(clamp16(current))
    return output


def resample_linear(samples: list[int], source_rate: int, target_rate: int) -> list[int]:
    if source_rate == target_rate or not samples:
        return samples[:]
    if target_rate <= 0:
        raise ValueError("Target sample rate must be greater than zero")

    output_len = max(1, int(round(len(samples) * target_rate / source_rate)))
    position_step = source_rate / target_rate
    output: list[int] = []

    for out_index in range(output_len):
        position = out_index * position_step
        left = int(position)
        fraction = position - left
        if left >= len(samples) - 1:
            output.append(samples[-1])
        else:
            mixed = samples[left] * (1.0 - fraction) + samples[left + 1] * fraction
            output.append(clamp16(mixed))
    return output


def fade_out(samples: list[int], sample_rate: int, duration_s: float) -> list[int]:
    if not samples or duration_s <= 0:
        return samples
    fade_count = min(len(samples), int(round(sample_rate * duration_s)))
    if fade_count <= 1:
        return samples

    output = samples[:]
    start = len(output) - fade_count
    for offset in range(fade_count):
        scale = 1.0 - (offset / (fade_count - 1))
        output[start + offset] = clamp16(output[start + offset] * scale)
    return output


def target_rate_for_options(options: ConversionOptions) -> int:
    if options.codec == "ADPCM-A":
        return ADPCMA_RATE
    return clamp(options.sample_rate, ADPCMB_MIN_RATE, ADPCMB_MAX_RATE)


def preprocess_audio(audio: AudioData, options: ConversionOptions) -> tuple[list[int], ProcessInfo]:
    target_rate = target_rate_for_options(options)
    samples = audio.samples[:]
    source_samples = len(samples)

    samples, leading_trim, trailing_trim = trim_silence(
        samples,
        options.silence_threshold_dbfs,
        options.trim_leading,
        options.trim_trailing,
    )

    truncated = False
    if options.max_duration_s and options.max_duration_s > 0:
        max_samples = max(1, int(round(options.max_duration_s * audio.sample_rate)))
        if len(samples) > max_samples:
            samples = samples[:max_samples]
            truncated = True

    if options.fade_on_trim and (trailing_trim > 0 or truncated):
        samples = fade_out(samples, audio.sample_rate, options.fade_out_s)

    if options.normalize:
        samples = normalize_to_dbfs(samples, -1.0)

    if options.volume_db:
        samples = apply_gain(samples, 10 ** (options.volume_db / 20))

    if options.low_pass_enabled:
        cutoff = min(options.low_pass_hz, target_rate * 0.45)
        samples = low_pass_filter(samples, audio.sample_rate, cutoff)

    samples = resample_linear(samples, audio.sample_rate, target_rate)

    if options.low_pass_enabled:
        cutoff = min(options.low_pass_hz, target_rate * 0.45)
        samples = low_pass_filter(samples, target_rate, cutoff)

    samples, pad_bytes = pad_samples_to_256_byte_adpcm(samples)
    info = ProcessInfo(
        source_rate=audio.sample_rate,
        target_rate=target_rate,
        source_samples=source_samples,
        processed_samples=len(samples) - (pad_bytes * 2),
        padded_samples=len(samples),
        trimmed_leading_samples=leading_trim,
        trimmed_trailing_samples=trailing_trim,
        truncated=truncated,
        pad_bytes=pad_bytes,
    )
    return samples, info


def pad_samples_to_256_byte_adpcm(samples: list[int]) -> tuple[list[int], int]:
    padded = samples[:] if samples else [0]
    if len(padded) % 2:
        padded.append(0)

    raw_bytes = len(padded) // 2
    pad_bytes = (-raw_bytes) % 256
    if pad_bytes:
        padded.extend([0] * (pad_bytes * 2))
    return padded, pad_bytes


class AdpcmAState:
    def __init__(self) -> None:
        self.acc = 0
        self.decstep = 0
        self.prevsample = 0
        self.previndex = 0

    def decode_nibble_12bit(self, code: int) -> int:
        self.acc += ADPCMA_TABLE[self.decstep + code]
        self.acc &= 0xFFF
        if self.acc & 0x800:
            self.acc |= ~0xFFF

        self.decstep += ADPCMA_STEP_ADJ[code & 7] * 16
        self.decstep = clamp(self.decstep, 0, 48 * 16)
        return self.acc

    def encode_sample_12bit(self, sample: int) -> int:
        predsample = self.prevsample
        index = self.previndex
        step = ADPCMA_STEP_SIZE[index]

        diff = sample - predsample
        if diff >= 0:
            code = 0
        else:
            code = 8
            diff = -diff

        tempstep = step
        if diff >= tempstep:
            code |= 4
            diff -= tempstep
        tempstep >>= 1
        if diff >= tempstep:
            code |= 2
            diff -= tempstep
        tempstep >>= 1
        if diff >= tempstep:
            code |= 1

        predsample = self.decode_nibble_12bit(code)
        index += ADPCMA_STEP_ADJ[code]
        index = clamp(index, 0, 48)

        self.prevsample = predsample
        self.previndex = index
        return code


def encode_adpcma(samples: list[int]) -> bytes:
    state = AdpcmAState()
    output = bytearray()
    for index in range(0, len(samples), 2):
        first = clamp(samples[index] >> 4, -2048, 2047)
        second = clamp(samples[index + 1] >> 4, -2048, 2047)
        output.append((state.encode_sample_12bit(first) << 4) | state.encode_sample_12bit(second))
    return bytes(output)


def decode_adpcma(data: bytes) -> list[int]:
    state = AdpcmAState()
    samples: list[int] = []
    for byte in data:
        for nibble in ((byte >> 4) & 0x0F, byte & 0x0F):
            samples.append(clamp16(state.decode_nibble_12bit(nibble) << 4))
    return samples


def encode_adpcmb(samples: list[int]) -> bytes:
    output = bytearray()
    xn = 0
    step_size = 127

    for index in range(0, len(samples), 2):
        high, xn, step_size = encode_adpcmb_nibble(samples[index], xn, step_size)
        low, xn, step_size = encode_adpcmb_nibble(samples[index + 1], xn, step_size)
        output.append((high << 4) | low)
    return bytes(output)


def encode_adpcmb_nibble(sample: int, xn: int, step_size: int) -> tuple[int, int, int]:
    delta = sample - xn
    magnitude = (abs(delta) << 16) // (step_size << 14)
    if magnitude > 7:
        magnitude = 7

    code = int(magnitude)
    adjustment = (code * 2 + 1) * step_size // 8
    if delta < 0:
        code |= 0x08
        xn -= adjustment
    else:
        xn += adjustment

    xn = clamp16(xn)
    step_size = (ADPCMB_STEP_SCALE[code] * step_size) // 64
    step_size = clamp(step_size, 127, 24576)
    return code, xn, step_size


def decode_adpcmb(data: bytes) -> list[int]:
    samples: list[int] = []
    xn = 0
    step_size = 127
    for byte in data:
        for code in ((byte >> 4) & 0x0F, byte & 0x0F):
            adjustment = ((code & 0x07) * 2 + 1) * step_size // 8
            if code & 0x08:
                xn -= adjustment
            else:
                xn += adjustment
            xn = clamp16(xn)
            step_size = (ADPCMB_STEP_SCALE[code] * step_size) // 64
            step_size = clamp(step_size, 127, 24576)
            samples.append(xn)
    return samples


def encode_samples(samples: list[int], codec: str) -> bytes:
    if codec == "ADPCM-A":
        return encode_adpcma(samples)
    if codec == "ADPCM-B":
        return encode_adpcmb(samples)
    raise ValueError(f"Unsupported codec: {codec}")


def decode_samples(data: bytes, codec: str) -> list[int]:
    if codec == "ADPCM-A":
        return decode_adpcma(data)
    if codec == "ADPCM-B":
        return decode_adpcmb(data)
    raise ValueError(f"Unsupported codec: {codec}")


def delta_n_for_rate(sample_rate: int) -> int:
    return clamp(round(sample_rate * 65535 / ADPCMB_BASE_RATE), 1, 0xFFFF)


def convert_audio_file(
    input_path: Path,
    wav_output_path: Path | None,
    raw_output_path: Path | None,
    options: ConversionOptions,
) -> ConversionResult:
    if wav_output_path is None and raw_output_path is None:
        raise ValueError("At least one output type must be enabled")

    audio = read_audio_file(input_path)
    padded_samples, info = preprocess_audio(audio, options)
    encoded = encode_samples(padded_samples, options.codec)

    if raw_output_path is not None:
        raw_output_path.parent.mkdir(parents=True, exist_ok=True)
        raw_output_path.write_bytes(encoded)

    if wav_output_path is not None:
        decoded = decode_samples(encoded, options.codec)
        wav_output_path.parent.mkdir(parents=True, exist_ok=True)
        write_wav_pcm(wav_output_path, info.target_rate, decoded)

    delta_n = delta_n_for_rate(info.target_rate) if options.codec == "ADPCM-B" else None
    return ConversionResult(
        input_path=input_path,
        wav_output_path=wav_output_path,
        raw_output_path=raw_output_path,
        codec=options.codec,
        target_rate=info.target_rate,
        source_rate=info.source_rate,
        source_duration_s=info.source_samples / info.source_rate if info.source_rate else 0.0,
        processed_duration_s=info.processed_samples / info.target_rate if info.target_rate else 0.0,
        padded_duration_s=info.padded_samples / info.target_rate if info.target_rate else 0.0,
        encoded_bytes=len(encoded),
        pad_bytes=info.pad_bytes,
        delta_n=delta_n,
    )


def preview_wav(input_path: Path, options: ConversionOptions) -> Path:
    audio = read_audio_file(input_path)
    padded_samples, info = preprocess_audio(audio, options)
    encoded = encode_samples(padded_samples, options.codec)
    decoded = decode_samples(encoded, options.codec)
    preview_path = Path(tempfile.gettempdir()) / "neogeo_adpcm_preview.wav"
    write_wav_pcm(preview_path, info.target_rate, decoded)
    return preview_path


def collect_audio_files(input_path: Path, single_file: bool, recursive: bool) -> list[Path]:
    if single_file:
        if not input_path.is_file():
            raise ValueError("Input file does not exist")
        if input_path.suffix.lower() not in AUDIO_EXTENSIONS:
            raise ValueError("Input file must be a WAV or MP3 file")
        return [input_path]

    if not input_path.is_dir():
        raise ValueError("Input folder does not exist")

    pattern = "**/*" if recursive else "*"
    return sorted(path for path in input_path.glob(pattern) if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS)


def source_output_folder(input_path: Path, single_file: bool) -> Path:
    base = input_path.parent if single_file else input_path
    return base / "output"


def path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def exclude_output_folder(files: list[Path], output_root: Path) -> list[Path]:
    output_root = output_root.resolve()
    return [path for path in files if not path_is_relative_to(path.resolve(), output_root)]


def output_paths_for(
    input_file: Path,
    input_root: Path,
    output_root: Path,
    codec: str,
    single_file: bool,
    write_wav: bool,
    write_raw: bool,
) -> tuple[Path | None, Path | None]:
    if single_file:
        relative_base = Path(input_file.stem)
    else:
        relative_base = input_file.relative_to(input_root).with_suffix("")

    wav_path = output_root / relative_base.with_name(relative_base.name + WAV_SUFFIXES[codec]) if write_wav else None
    raw_path = output_root / relative_base.with_suffix(RAW_EXTENSIONS[codec]) if write_raw else None
    return wav_path, raw_path


def convert_batch(
    files: list[Path],
    input_root: Path,
    output_root: Path,
    options: ConversionOptions,
    single_file: bool,
    stop_requested: Callable[[], bool] | None = None,
    log: Callable[[str], None] | None = None,
) -> list[ConversionResult]:
    results: list[ConversionResult] = []
    manifest_path = output_root / "adpcm_manifest.csv"
    output_root.mkdir(parents=True, exist_ok=True)

    with manifest_path.open("w", newline="", encoding="utf-8") as manifest_file:
        writer = csv.writer(manifest_file)
        writer.writerow([
            "input",
            "wav_output",
            "raw_output",
            "codec",
            "source_rate",
            "target_rate",
            "delta_n_hex",
            "source_seconds",
            "processed_seconds",
            "padded_seconds",
            "encoded_bytes",
            "pad_bytes",
            "status",
            "error",
        ])

        for file_index, input_file in enumerate(files, start=1):
            if stop_requested and stop_requested():
                if log:
                    log("Conversion stopped by user.")
                break

            wav_output, raw_output = output_paths_for(
                input_file,
                input_root,
                output_root,
                options.codec,
                single_file,
                options.write_wav,
                options.write_raw,
            )
            display_output = wav_output or raw_output
            if log:
                log(f"[{file_index}/{len(files)}] {input_file.name} -> {display_output.name if display_output else 'output'}")

            try:
                result = convert_audio_file(input_file, wav_output, raw_output, options)
                results.append(result)
                writer.writerow([
                    str(input_file),
                    str(result.wav_output_path or ""),
                    str(result.raw_output_path or ""),
                    result.codec,
                    result.source_rate,
                    result.target_rate,
                    f"0x{result.delta_n:04X}" if result.delta_n is not None else "",
                    f"{result.source_duration_s:.4f}",
                    f"{result.processed_duration_s:.4f}",
                    f"{result.padded_duration_s:.4f}",
                    result.encoded_bytes,
                    result.pad_bytes,
                    "ok",
                    "",
                ])
                if log:
                    delta = f", Delta-N 0x{result.delta_n:04X}" if result.delta_n is not None else ""
                    outputs = []
                    if result.wav_output_path:
                        outputs.append(result.wav_output_path.name)
                    if result.raw_output_path:
                        outputs.append(result.raw_output_path.name)
                    log(f"  OK: {', '.join(outputs)}; {result.encoded_bytes} ADPCM bytes, {result.target_rate} Hz{delta}")
            except Exception as exc:  # Keep converting the remaining folder entries.
                writer.writerow([
                    str(input_file),
                    str(wav_output or ""),
                    str(raw_output or ""),
                    options.codec,
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "error",
                    str(exc),
                ])
                if log:
                    log(f"  ERROR: {exc}")

    if log:
        log(f"Manifest written: {manifest_path}")
    return results


class ConverterApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("ADPCM-A/B Audio Converter")
        self.geometry("980x760")
        self.minsize(900, 620)

        self.input_mode = tk.StringVar(value="folder")
        self.input_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.use_source_output = tk.BooleanVar(value=False)
        self.codec = tk.StringVar(value="ADPCM-B")
        self.preset = tk.StringVar(value="Medium")
        self.sample_rate = tk.StringVar(value=str(PRESETS["Medium"]["b_rate"]))
        self.volume_db = tk.StringVar(value="0.0")
        self.max_duration_s = tk.StringVar()
        self.low_pass_enabled = tk.BooleanVar(value=False)
        self.low_pass_hz = tk.StringVar(value=str(PRESETS["Medium"]["low_pass"]))
        self.trim_leading = tk.BooleanVar(value=False)
        self.trim_trailing = tk.BooleanVar(value=False)
        self.normalize = tk.BooleanVar(value=False)
        self.fade_on_trim = tk.BooleanVar(value=False)
        self.fade_out_s = tk.StringVar(value="3.0")
        self.silence_threshold_dbfs = tk.StringVar(value="-45.0")
        self.recursive = tk.BooleanVar(value=False)
        self.output_format = tk.StringVar(value="WAV")
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None

        self.create_menu()
        self.create_widgets()
        self.input_path.trace_add("write", lambda *_args: self.update_source_output_path())
        self.apply_preset()

    def create_menu(self) -> None:
        menu_bar = tk.Menu(self)
        help_menu = tk.Menu(menu_bar, tearoff=0)
        help_menu.add_command(label="GUI Help", command=self.show_help)
        menu_bar.add_cascade(label="Help", menu=help_menu)
        self.configure(menu=menu_bar)

    def create_widgets(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        input_frame = ttk.LabelFrame(self, text="Input", padding=14)
        input_frame.grid(row=0, column=0, padx=16, pady=(14, 8), sticky="ew")
        input_frame.columnconfigure(1, weight=1)

        modes = ttk.Frame(input_frame)
        modes.grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(modes, text="Audio folder", variable=self.input_mode, value="folder", command=self.update_source_output_path).pack(side="left")
        ttk.Radiobutton(modes, text="Single audio file", variable=self.input_mode, value="file", command=self.update_source_output_path).pack(side="left", padx=(8, 0))

        ttk.Label(input_frame, text="Input path").grid(row=1, column=0, sticky="w", pady=(16, 4))
        ttk.Entry(input_frame, textvariable=self.input_path).grid(row=2, column=0, columnspan=2, sticky="ew", padx=(0, 8))
        ttk.Button(input_frame, text="Browse...", command=self.browse_input).grid(row=2, column=2, sticky="e")

        ttk.Label(input_frame, text="Output folder").grid(row=3, column=0, sticky="w", pady=(14, 4))
        self.output_entry = ttk.Entry(input_frame, textvariable=self.output_path)
        self.output_entry.grid(row=4, column=0, columnspan=2, sticky="ew", padx=(0, 8))
        self.output_browse_button = ttk.Button(input_frame, text="Browse...", command=self.browse_output)
        self.output_browse_button.grid(row=4, column=2, sticky="e")
        ttk.Checkbutton(
            input_frame,
            text="Use source folder and create an output subfolder",
            variable=self.use_source_output,
            command=self.update_source_output_path,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))

        quality = ttk.LabelFrame(self, text="Quality", padding=14)
        quality.grid(row=1, column=0, padx=16, pady=8, sticky="ew")
        quality.columnconfigure(1, weight=1)
        quality.columnconfigure(4, weight=1)

        ttk.Label(quality, text="Codec").grid(row=0, column=0, sticky="w")
        codec_box = ttk.Combobox(quality, textvariable=self.codec, values=["ADPCM-B", "ADPCM-A"], state="readonly", width=14)
        codec_box.grid(row=0, column=1, sticky="w")
        codec_box.bind("<<ComboboxSelected>>", lambda event: self.on_codec_changed())

        ttk.Label(quality, text="Preset").grid(row=0, column=3, sticky="w", padx=(32, 8))
        preset_box = ttk.Combobox(quality, textvariable=self.preset, values=list(PRESETS), state="readonly")
        preset_box.grid(row=0, column=4, columnspan=3, sticky="ew")
        preset_box.bind("<<ComboboxSelected>>", lambda event: self.apply_preset())

        ttk.Label(quality, text="Sample rate").grid(row=1, column=0, sticky="w", pady=(12, 0))
        self.sample_rate_entry = ttk.Entry(quality, textvariable=self.sample_rate, width=12)
        self.sample_rate_entry.grid(row=1, column=1, sticky="w", pady=(12, 0))

        ttk.Label(quality, text="Volume dB").grid(row=1, column=3, sticky="w", padx=(32, 8), pady=(12, 0))
        ttk.Entry(quality, textvariable=self.volume_db, width=12).grid(row=1, column=4, sticky="w", pady=(12, 0))
        ttk.Label(quality, text="(-6 meno, +6 piu')").grid(row=1, column=5, sticky="w", padx=(20, 0), pady=(12, 0))

        ttk.Label(quality, text="Max duration s").grid(row=2, column=0, sticky="w", pady=(12, 0))
        ttk.Entry(quality, textvariable=self.max_duration_s, width=12).grid(row=2, column=1, sticky="w", pady=(12, 0))

        ttk.Checkbutton(quality, text="Fade-out on trim", variable=self.fade_on_trim).grid(row=2, column=3, sticky="w", padx=(32, 8), pady=(12, 0))
        ttk.Entry(quality, textvariable=self.fade_out_s, width=12).grid(row=2, column=4, sticky="w", pady=(12, 0))

        ttk.Checkbutton(quality, text="Low-pass pre-encode", variable=self.low_pass_enabled).grid(row=3, column=0, columnspan=2, sticky="w", pady=(14, 0))
        ttk.Label(quality, text="Low-pass Hz").grid(row=3, column=3, sticky="w", padx=(32, 8), pady=(12, 0))
        ttk.Entry(quality, textvariable=self.low_pass_hz, width=12).grid(row=3, column=4, sticky="w", pady=(12, 0))

        ttk.Checkbutton(quality, text="Trim leading silence", variable=self.trim_leading).grid(row=4, column=0, sticky="w", pady=(14, 0))
        ttk.Checkbutton(quality, text="Trim trailing silence", variable=self.trim_trailing).grid(row=4, column=1, sticky="w", pady=(14, 0))
        ttk.Label(quality, text="Silence threshold dBFS").grid(row=4, column=3, sticky="w", padx=(32, 8), pady=(12, 0))
        ttk.Entry(quality, textvariable=self.silence_threshold_dbfs, width=12).grid(row=4, column=4, sticky="w", pady=(12, 0))

        ttk.Checkbutton(quality, text="Normalize to -1 dBFS", variable=self.normalize).grid(row=5, column=0, columnspan=2, sticky="w", pady=(14, 0))
        ttk.Checkbutton(quality, text="Recursive subfolder scan", variable=self.recursive).grid(row=5, column=3, columnspan=2, sticky="w", padx=(32, 8), pady=(14, 0))

        ttk.Label(quality, text="Output format").grid(row=6, column=0, sticky="w", pady=(14, 0))
        ttk.Combobox(
            quality,
            textvariable=self.output_format,
            values=list(OUTPUT_FORMATS),
            state="readonly",
            width=20,
        ).grid(row=6, column=1, sticky="w", pady=(14, 0))

        log_frame = ttk.LabelFrame(self, text="Log", padding=14)
        log_frame.grid(row=2, column=0, padx=16, pady=8, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, height=12, wrap="word")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

        footer = ttk.Frame(self)
        footer.grid(row=3, column=0, padx=16, pady=(8, 14), sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.status_label = ttk.Label(footer, text="Ready.")
        self.status_label.grid(row=0, column=0, sticky="w")
        self.preview_button = ttk.Button(footer, text="Preview", command=self.start_preview)
        self.preview_button.grid(row=0, column=1, padx=(0, 8))
        self.convert_button = ttk.Button(footer, text="Convert", command=self.start_convert)
        self.convert_button.grid(row=0, column=2, padx=(0, 8))
        self.stop_button = ttk.Button(footer, text="Stop", command=self.stop_work, state="disabled")
        self.stop_button.grid(row=0, column=3, padx=(0, 8))
        ttk.Button(footer, text="Close", command=self.on_close).grid(row=0, column=4)

    def browse_input(self) -> None:
        if self.input_mode.get() == "file":
            path = filedialog.askopenfilename(filetypes=[("Audio files", "*.wav *.mp3"), ("WAV files", "*.wav"), ("MP3 files", "*.mp3"), ("All files", "*.*")])
        else:
            path = filedialog.askdirectory()
        if path:
            self.input_path.set(path)
            if not self.output_path.get():
                base = Path(path).parent if self.input_mode.get() == "file" else Path(path)
                self.output_path.set(str(base / "converted_adpcm"))

    def browse_output(self) -> None:
        path = filedialog.askdirectory()
        if path:
            self.use_source_output.set(False)
            self.update_source_output_path()
            self.output_path.set(path)

    def show_help(self) -> None:
        help_window = tk.Toplevel(self)
        help_window.title("Help")
        help_window.geometry("720x620")
        help_window.minsize(560, 420)
        help_window.transient(self)

        help_window.columnconfigure(0, weight=1)
        help_window.rowconfigure(0, weight=1)

        text = tk.Text(help_window, wrap="word", padx=14, pady=14)
        text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(help_window, orient="vertical", command=text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scrollbar.set)
        text.insert("1.0", HELP_TEXT)
        text.configure(state="disabled")

        buttons = ttk.Frame(help_window, padding=(14, 8, 14, 14))
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Close", command=help_window.destroy).grid(row=0, column=1)

    def update_source_output_path(self) -> None:
        use_source = self.use_source_output.get()
        if hasattr(self, "output_entry"):
            state = "disabled" if use_source else "normal"
            self.output_entry.configure(state=state)
            self.output_browse_button.configure(state=state)

        if not use_source:
            return

        raw_input = self.input_path.get().strip()
        if raw_input:
            single_file = self.input_mode.get() == "file"
            self.output_path.set(str(source_output_folder(Path(raw_input), single_file)))
        else:
            self.output_path.set("")

    def on_codec_changed(self) -> None:
        self.apply_preset()

    def apply_preset(self) -> None:
        preset = PRESETS[self.preset.get()]
        if self.codec.get() == "ADPCM-A":
            self.sample_rate.set(str(ADPCMA_RATE))
            self.sample_rate_entry.configure(state="disabled")
            self.low_pass_hz.set(str(min(preset["low_pass"], 8000)))
        else:
            self.sample_rate_entry.configure(state="normal")
            self.sample_rate.set(str(preset["b_rate"]))
            self.low_pass_hz.set(str(preset["low_pass"]))
        self.normalize.set(bool(preset["normalize"]))

    def read_options(self) -> ConversionOptions:
        codec = self.codec.get()
        sample_rate = parse_int(self.sample_rate.get(), "Sample rate")
        if codec == "ADPCM-A":
            sample_rate = ADPCMA_RATE
        elif sample_rate < ADPCMB_MIN_RATE or sample_rate > ADPCMB_MAX_RATE:
            raise ValueError(f"ADPCM-B sample rate must be between {ADPCMB_MIN_RATE} and {ADPCMB_MAX_RATE} Hz")

        max_duration = parse_optional_float(self.max_duration_s.get(), "Max duration")
        write_wav, write_raw = OUTPUT_FORMATS[self.output_format.get()]
        return ConversionOptions(
            codec=codec,
            preset=self.preset.get(),
            sample_rate=sample_rate,
            volume_db=parse_float(self.volume_db.get(), "Volume dB"),
            max_duration_s=max_duration,
            low_pass_enabled=self.low_pass_enabled.get(),
            low_pass_hz=parse_float(self.low_pass_hz.get(), "Low-pass Hz"),
            trim_leading=self.trim_leading.get(),
            trim_trailing=self.trim_trailing.get(),
            normalize=self.normalize.get(),
            fade_on_trim=self.fade_on_trim.get(),
            fade_out_s=parse_float(self.fade_out_s.get(), "Fade-out seconds"),
            silence_threshold_dbfs=parse_float(self.silence_threshold_dbfs.get(), "Silence threshold"),
            recursive=self.recursive.get(),
            write_wav=write_wav,
            write_raw=write_raw,
        )

    def selected_files(self, options: ConversionOptions) -> tuple[list[Path], Path, Path, bool]:
        raw_input = self.input_path.get().strip()
        if not raw_input:
            raise ValueError("Select an input file or folder")

        input_path = Path(raw_input)
        single_file = self.input_mode.get() == "file"
        input_root = input_path.parent if single_file else input_path

        if self.use_source_output.get():
            output_root = source_output_folder(input_path, single_file)
            self.output_path.set(str(output_root))
        else:
            raw_output = self.output_path.get().strip()
            output_root = Path(raw_output) if raw_output else (input_path.parent / "converted_adpcm")

        files = collect_audio_files(input_path, single_file, options.recursive)
        if not single_file:
            files = exclude_output_folder(files, output_root)
        if not files:
            raise ValueError("No WAV or MP3 files found")
        return files, input_root, output_root, single_file

    def start_preview(self) -> None:
        try:
            options = self.read_options()
            files, _input_root, _output_root, _single_file = self.selected_files(options)
        except Exception as exc:
            messagebox.showerror("Preview", str(exc))
            return

        self.start_worker("Previewing...", self.preview_worker, files[0], options)

    def preview_worker(self, input_file: Path, options: ConversionOptions) -> None:
        self.log(f"Preview: {input_file}")
        preview_path = preview_wav(input_file, options)
        self.log(f"Preview WAV written: {preview_path}")
        if winsound is None:
            self.log("winsound is not available; open the preview WAV manually.")
            return
        winsound.PlaySound(str(preview_path), winsound.SND_FILENAME | winsound.SND_ASYNC)

    def start_convert(self) -> None:
        try:
            options = self.read_options()
            files, input_root, output_root, single_file = self.selected_files(options)
        except Exception as exc:
            messagebox.showerror("Convert", str(exc))
            return

        self.start_worker("Converting...", self.convert_worker, files, input_root, output_root, options, single_file)

    def convert_worker(
        self,
        files: list[Path],
        input_root: Path,
        output_root: Path,
        options: ConversionOptions,
        single_file: bool,
    ) -> None:
        self.log(f"Converting {len(files)} audio file(s) to {options.codec}.")
        results = convert_batch(
            files,
            input_root,
            output_root,
            options,
            single_file,
            stop_requested=self.stop_event.is_set,
            log=self.log,
        )
        self.log(f"Done: {len(results)} file(s) converted.")

    def start_worker(self, status: str, target: Callable, *args: object) -> None:
        if self.worker and self.worker.is_alive():
            return

        self.stop_event.clear()
        self.set_busy(True, status)

        def runner() -> None:
            try:
                target(*args)
            except Exception as exc:
                self.log(f"ERROR: {exc}")
            finally:
                self.after(0, lambda: self.set_busy(False, "Ready."))

        self.worker = threading.Thread(target=runner, daemon=True)
        self.worker.start()

    def set_busy(self, busy: bool, status: str) -> None:
        self.status_label.configure(text=status)
        state = "disabled" if busy else "normal"
        self.preview_button.configure(state=state)
        self.convert_button.configure(state=state)
        self.stop_button.configure(state="normal" if busy else "disabled")

    def stop_work(self) -> None:
        self.stop_event.set()
        if winsound is not None:
            winsound.PlaySound(None, 0)
        self.log("Stop requested.")

    def log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")

        def append() -> None:
            self.log_text.insert("end", f"[{timestamp}] {message}\n")
            self.log_text.see("end")

        self.after(0, append)

    def on_close(self) -> None:
        self.stop_event.set()
        if winsound is not None:
            winsound.PlaySound(None, 0)
        self.destroy()


def parse_float(value: str, label: str) -> float:
    try:
        return float(value.strip())
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid number") from exc


def parse_optional_float(value: str, label: str) -> float | None:
    if not value.strip():
        return None
    parsed = parse_float(value, label)
    if parsed <= 0:
        raise ValueError(f"{label} must be greater than zero")
    return parsed


def parse_int(value: str, label: str) -> int:
    try:
        return int(value.strip())
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid integer") from exc


def launch_gui() -> None:
    if tk is None:
        raise RuntimeError("Tkinter is not available in this Python installation")
    app = ConverterApp()
    app.mainloop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert WAV or MP3 files to NeoGeo ADPCM-A or ADPCM-B.")
    parser.add_argument("--input", type=Path, help="Input WAV/MP3 file or audio folder")
    parser.add_argument("--output", type=Path, help="Output folder")
    parser.add_argument("--codec", choices=["ADPCM-A", "ADPCM-B"], default="ADPCM-B")
    parser.add_argument("--sample-rate", type=int, default=22050, help="ADPCM-B target rate; ADPCM-A is fixed to 18500")
    parser.add_argument("--recursive", action="store_true", help="Scan subfolders when input is a folder")
    parser.add_argument("--normalize", action="store_true", help="Normalize to -1 dBFS")
    parser.add_argument("--low-pass", action="store_true", help="Enable low-pass filter before encoding")
    parser.add_argument("--low-pass-hz", type=float, default=8000.0)
    parser.add_argument("--trim-leading", action="store_true")
    parser.add_argument("--trim-trailing", action="store_true")
    parser.add_argument("--silence-threshold-dbfs", type=float, default=-45.0)
    parser.add_argument("--volume-db", type=float, default=0.0)
    parser.add_argument("--max-duration-s", type=float)
    parser.add_argument("--fade-on-trim", action="store_true")
    parser.add_argument("--fade-out-s", type=float, default=3.0)
    parser.add_argument(
        "--output-format",
        choices=["wav", "raw", "both"],
        default="wav",
        help="Output format: decoded WAV, raw NeoGeo data, or both",
    )
    parser.add_argument("--no-wav", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-raw", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--self-test", action="store_true", help="Create a short WAV and verify ADPCM-A/B output")
    return parser


def options_from_args(args: argparse.Namespace) -> ConversionOptions:
    write_wav, write_raw = {
        "wav": (True, False),
        "raw": (False, True),
        "both": (True, True),
    }[args.output_format]
    if args.no_wav:
        write_wav = False
    if args.no_raw:
        write_raw = False

    return ConversionOptions(
        codec=args.codec,
        sample_rate=args.sample_rate,
        volume_db=args.volume_db,
        max_duration_s=args.max_duration_s,
        low_pass_enabled=args.low_pass,
        low_pass_hz=args.low_pass_hz,
        trim_leading=args.trim_leading,
        trim_trailing=args.trim_trailing,
        normalize=args.normalize,
        fade_on_trim=args.fade_on_trim,
        fade_out_s=args.fade_out_s,
        silence_threshold_dbfs=args.silence_threshold_dbfs,
        recursive=args.recursive,
        write_wav=write_wav,
        write_raw=write_raw,
    )


def run_cli(args: argparse.Namespace) -> int:
    if args.self_test:
        run_self_test()
        return 0

    if not args.input:
        launch_gui()
        return 0

    options = options_from_args(args)
    if not options.write_wav and not options.write_raw:
        raise ValueError("At least one output type must be enabled")
    single_file = args.input.is_file()
    output_root = args.output or (args.input.parent / "converted_adpcm")
    files = collect_audio_files(args.input, single_file, options.recursive)
    input_root = args.input.parent if single_file else args.input
    convert_batch(files, input_root, output_root, options, single_file, log=print)
    return 0


def run_self_test() -> None:
    temp_root = Path(tempfile.gettempdir()) / "neogeo_adpcm_converter_selftest"
    input_dir = temp_root / "input"
    output_dir = temp_root / "output"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    test_wav = input_dir / "sine_440.wav"
    sample_rate = 44100
    seconds = 0.5
    samples = [
        int(math.sin(2 * math.pi * 440 * index / sample_rate) * 12000)
        for index in range(int(sample_rate * seconds))
    ]
    write_wav_pcm(test_wav, sample_rate, samples)

    for codec in ("ADPCM-A", "ADPCM-B"):
        options = ConversionOptions(
            codec=codec,
            sample_rate=22050,
            normalize=True,
            low_pass_enabled=True,
            low_pass_hz=8000,
            write_raw=True,
        )
        wav_output = output_dir / f"sine_440{WAV_SUFFIXES[codec]}"
        raw_output = output_dir / f"sine_440{RAW_EXTENSIONS[codec]}"
        result = convert_audio_file(test_wav, wav_output, raw_output, options)
        if result.encoded_bytes == 0 or result.encoded_bytes % 256 != 0:
            raise AssertionError(f"{codec} output is not 256-byte aligned")
        if not result.wav_output_path or not result.wav_output_path.exists():
            raise AssertionError(f"{codec} WAV output was not created")
        if not result.raw_output_path or not result.raw_output_path.exists():
            raise AssertionError(f"{codec} raw output was not created")
        decoded = decode_samples(result.raw_output_path.read_bytes(), codec)
        if not decoded:
            raise AssertionError(f"{codec} decode preview returned no samples")
        print(f"{codec}: {result.wav_output_path} + {result.raw_output_path} ({result.encoded_bytes} bytes)")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return run_cli(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
