from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SRTBlock:
    index: str
    timecode: str
    text_lines: list[str]


@dataclass(frozen=True)
class SRTEntry:
    index: int
    start_seconds: float
    end_seconds: float
    text: str


def is_index_line(line: str) -> bool:
    return line.strip().isdigit()


def is_timecode_line(line: str) -> bool:
    return "-->" in line


def format_timestamp(seconds: float) -> str:
    total_milliseconds = max(0, int(round(seconds * 1000.0)))
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def split_srt_blocks(srt_text: str) -> list[list[str]]:
    normalized = srt_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    blocks: list[list[str]] = []
    for raw_block in normalized.split("\n\n"):
        lines = [line.rstrip() for line in raw_block.split("\n")]
        if any(line.strip() for line in lines):
            blocks.append(lines)
    return blocks


def parse_srt_block(lines: list[str]) -> SRTBlock:
    if len(lines) < 2:
        raise ValueError(f"Invalid SRT block: {lines!r}")

    index = lines[0].strip()
    timecode = lines[1].strip()
    if not is_index_line(index):
        raise ValueError(f"Invalid SRT index line: {lines[0]!r}")
    if not is_timecode_line(timecode):
        raise ValueError(f"Invalid SRT timecode line: {lines[1]!r}")

    return SRTBlock(index=index, timecode=timecode, text_lines=lines[2:])


def parse_timestamp(timestamp: str) -> float:
    hours, minutes, seconds_milliseconds = timestamp.strip().split(":")
    seconds, milliseconds = seconds_milliseconds.split(",")
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(milliseconds) / 1000.0
    )


def parse_timecode_range(timecode_line: str) -> tuple[float, float]:
    start_str, end_str = [part.strip() for part in timecode_line.split("-->", maxsplit=1)]
    return parse_timestamp(start_str), parse_timestamp(end_str)


def extract_srt_entries(srt_text: str) -> list[SRTEntry]:
    entries: list[SRTEntry] = []
    for block_lines in split_srt_blocks(srt_text):
        block = parse_srt_block(block_lines)
        text = " ".join(line.strip() for line in block.text_lines if line.strip())
        if not text:
            continue
        start_seconds, end_seconds = parse_timecode_range(block.timecode)
        entries.append(
            SRTEntry(
                index=int(block.index),
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                text=text,
            )
        )
    return entries


def render_srt_block(block: SRTBlock) -> str:
    parts = [block.index, block.timecode, *block.text_lines]
    return "\n".join(parts).rstrip()


def translate_srt_text_lines(srt_text: str, translate_line) -> str:
    rendered_blocks: list[str] = []
    for lines in split_srt_blocks(srt_text):
        block = parse_srt_block(lines)
        translated_lines: list[str] = []
        for line in block.text_lines:
            if line.strip():
                translated_lines.append(translate_line(line))
            else:
                translated_lines.append("")
        rendered_blocks.append(render_srt_block(SRTBlock(block.index, block.timecode, translated_lines)))
    return "\n\n".join(rendered_blocks) + ("\n" if rendered_blocks else "")


def extract_text_lines_for_tts(srt_text: str) -> list[str]:
    lines_for_tts: list[str] = []
    for block_lines in split_srt_blocks(srt_text):
        block = parse_srt_block(block_lines)
        text_lines = [line.strip() for line in block.text_lines if line.strip()]
        if text_lines:
            lines_for_tts.append(" ".join(text_lines))
    return lines_for_tts