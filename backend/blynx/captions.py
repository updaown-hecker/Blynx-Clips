"""CPU Whisper transcription and burn-in captions for vertical highlights."""
import json
from pathlib import Path
from textwrap import wrap


def ass_time(seconds: float) -> str:
    centiseconds = round(seconds * 100)
    minutes, fraction = divmod(centiseconds, 6000)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{fraction // 100:02d}.{fraction % 100:02d}"


def subtitle_text(value: str) -> str:
    # Treat the transcript as text, never as ASS override tags or escape sequences.
    clean = " ".join(value.replace("{", "").replace("}", "").replace("\\", " ").split())
    return r"\N".join(wrap(clean, width=37))


def write_ass(path: Path, channel: str, duration: float, segments: list[tuple[float, float, str]],
              *, style: str = "clean", margin: int = 136) -> None:
    size, color, border, outline, shadow = {
        "clean": (43, "&H00FFFFFF", 1, 3, 0),
        "pop": (53, "&H0000E5FF", 1, 5, 1),
        "boxed": (42, "&H00FFFFFF", 3, 4, 0),
    }[style]
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 720", "PlayResY: 1280",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour,"
        " Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow,"
        " Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Credit,DejaVu Sans,30,&H00FFFFFF,&H000000FF,&H00111111,&H00111111,"
        "-1,0,0,0,100,100,0,0,1,2,0,8,32,32,56,1",
        f"Style: Caption,DejaVu Sans,{size},{color},&H000000FF,&H00111111,&H00111111,"
        f"-1,0,0,0,100,100,0,0,{border},{outline},{shadow},2,36,36,{margin},1",
        "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if channel:
        lines.append(f"Dialogue: 0,0:00:00.00,{ass_time(duration)},Credit,,0,0,0,,Twitch: {subtitle_text(channel)}")
    for start, end, content in segments:
        start, end = max(0, start), min(duration, end)
        if end - start >= 0.04 and (label := subtitle_text(content)):
            lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Caption,,0,0,0,,{label}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def word_cues(segments):
    cues = []
    for segment in segments:
        group = []
        for word in segment.words or ():
            if group and (len(group) >= 5 or word.end - group[0].start > 2.4 or
                          len("".join(item.word for item in group) + word.word) > 32 or
                          word.start - group[-1].end > 0.45):
                cues.append((group[0].start, group[-1].end, "".join(item.word for item in group).strip()))
                group = []
            group.append(word)
        if group:
            cues.append((group[0].start, group[-1].end, "".join(item.word for item in group).strip()))
    return cues


if __name__ == "__main__":
    import argparse
    import wave

    import numpy as np
    from faster_whisper import WhisperModel
    from huggingface_hub.errors import LocalEntryNotFoundError

    parser = argparse.ArgumentParser()
    parser.add_argument("audio")
    parser.add_argument("cache")
    parser.add_argument("--short-cues", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with wave.open(args.audio, "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
            raise ValueError("Whisper expects 16 kHz mono PCM")
        audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32) / 32768
    options = {"device": "cpu", "compute_type": "int8", "cpu_threads": 2, "download_root": args.cache}
    try:
        model = WhisperModel("base", local_files_only=True, **options)
    except LocalEntryNotFoundError:
        model = WhisperModel("base", **options)
    segments, _ = model.transcribe(audio, beam_size=3, vad_filter=True, condition_on_previous_text=False,
                                  word_timestamps=args.short_cues)
    result = json.dumps(word_cues(segments) if args.short_cues else
                        [(segment.start, segment.end, segment.text) for segment in segments])
    if args.output:
        args.output.write_text(result, encoding="utf-8")
    else:
        print(result)
