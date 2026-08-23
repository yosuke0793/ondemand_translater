from __future__ import annotations

import csv
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from .srt_utils import SRTBlock, parse_srt_block, render_srt_block, split_srt_blocks

FILLER_PATTERN = re.compile(
    r"(^|\s)(えーと|えっと|えー|あのー|あの|そのー|その|まー|まあ|えっとですね)(\s|$)"
)
OUTPUT_LABEL_PATTERN = re.compile(r"^(整形後|出力|修正後|回答)[:：]\s*")
OLLAMA_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "jp_clean_ollama.txt"
OLLAMA_GLOSSARY_PATH = Path(__file__).resolve().parent / "prompts" / "jp_terms_glossary.csv"
OLLAMA_INPUT_PLACEHOLDER = "{{INPUT_TEXT}}"
OLLAMA_GLOSSARY_PLACEHOLDER = "{{GLOSSARY_HINTS}}"
# 0 means "process all lines in a single batch".
OLLAMA_BATCH_SIZE = 0
ENGLISH_CHAR_PATTERN = re.compile(r"[A-Za-z]")
JAPANESE_CHAR_PATTERN = re.compile(r"[ぁ-んァ-ン一-龥々ー]")
OLLAMA_INSTALL_HINT = (
    "ollama が見つかりません。Ubuntu では `curl -fsSL https://ollama.com/install.sh | sh`、"
    "macOS では `brew install ollama` を実行してください。"
)


def clean_line_rule(line: str) -> str:
    cleaned = line.strip()
    cleaned = FILLER_PATTERN.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.replace("なんですけど", "です")
    cleaned = cleaned.replace("っていう", "という")
    cleaned = cleaned.replace("みたいな", "のような")
    cleaned = cleaned.replace("してて", "していて")
    cleaned = cleaned.replace("となってて", "となっていて")
    
    # Remove repeated phrases (e.g., "Aということで、Aということで、..." → "Aということで")
    cleaned = remove_repeated_phrases(cleaned)
    
    return cleaned


def remove_repeated_phrases(text: str) -> str:
    """Remove consecutive repeated phrases from text."""
    # Match patterns like "ということで、ということで、..." or "方程式ということで、方程式ということで、..."
    # Try to detect and collapse repeated 6+ character phrases separated by punctuation
    sentences = re.split(r'(、|。)', text)
    result: list[str] = []
    last_phrase = ""
    
    for segment in sentences:
        if segment in ("、", "。"):
            result.append(segment)
        elif segment.strip():
            # Collapse if this phrase (or a long substring of it) was just added
            if last_phrase and len(segment.strip()) >= 6:
                # Check if this segment is similar to the last one (simple heuristic)
                if segment.strip() in last_phrase or last_phrase in segment.strip() or (
                    len(set(segment.split()) & set(last_phrase.split())) >= 2 and len(segment.strip()) > 10
                ):
                    # Skip adding a near-duplicate
                    continue
            result.append(segment)
            last_phrase = segment.strip()
    
    return "".join(result)


def ensure_ollama() -> None:
    ollama_bin = shutil.which("ollama")
    if ollama_bin is None:
        raise FileNotFoundError(OLLAMA_INSTALL_HINT)

    run_command([ollama_bin, "--version"], "jp_clean_ollama")


def run_command(command: list[str], step_name: str) -> None:
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"{step_name} failed") from exc


def ensure_ollama_model(model_name: str) -> None:
    ollama_bin = shutil.which("ollama")
    if ollama_bin is None:
        raise FileNotFoundError(OLLAMA_INSTALL_HINT)

    result = subprocess.run(
        [ollama_bin, "list"],
        check=True,
        capture_output=True,
        text=True,
    )
    installed_models = result.stdout
    if model_name in installed_models:
        return

    print(f"[Ollama] モデル {model_name} を取得します")
    run_command([ollama_bin, "pull", model_name], "jp_clean_ollama")


@lru_cache(maxsize=1)
def load_ollama_prompt_template() -> str:
    if not OLLAMA_PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"Ollama プロンプトファイルが見つかりません: {OLLAMA_PROMPT_PATH}"
        )

    template = OLLAMA_PROMPT_PATH.read_text(encoding="utf-8").strip()
    if OLLAMA_INPUT_PLACEHOLDER not in template:
        raise ValueError(
            f"Ollama プロンプトファイルには {OLLAMA_INPUT_PLACEHOLDER} を含めてください: {OLLAMA_PROMPT_PATH}"
        )
    return template


@lru_cache(maxsize=1)
def load_ollama_glossary_entries() -> list[tuple[str, str]]:
    if not OLLAMA_GLOSSARY_PATH.exists():
        return []

    entries: list[tuple[str, str]] = []
    with OLLAMA_GLOSSARY_PATH.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            first = row[0].strip()
            if not first or first.startswith("#"):
                continue
            if len(row) < 2:
                continue
            wrong = first
            correct = row[1].strip()
            if not correct:
                continue
            if wrong.lower() == "wrong" and correct.lower() == "correct":
                continue
            entries.append((wrong, correct))
    return entries


def build_glossary_hints(input_text: str, max_items: int = 12) -> str:
    entries = load_ollama_glossary_entries()
    if not entries:
        return "(用語集エントリなし)"

    matched: list[tuple[str, str]] = []
    for wrong, correct in entries:
        if wrong in input_text or correct in input_text:
            matched.append((wrong, correct))
        if len(matched) >= max_items:
            break

    if not matched:
        return "(該当なし)"

    return "\n".join(f"- {wrong} -> {correct}" for wrong, correct in matched)


def build_ollama_prompt(input_text: str) -> str:
    template = load_ollama_prompt_template()
    prompt = template.replace(OLLAMA_INPUT_PLACEHOLDER, input_text)
    glossary_hints = build_glossary_hints(input_text)
    if OLLAMA_GLOSSARY_PLACEHOLDER in prompt:
        return prompt.replace(OLLAMA_GLOSSARY_PLACEHOLDER, glossary_hints)
    return f"{prompt}\n\n[用語集]\n{glossary_hints}"


def clean_line_with_ollama(line: str, model_name: str) -> str:
    normalized = clean_line_rule(line)
    if not normalized:
        return normalized

    ollama_bin = shutil.which("ollama")
    if ollama_bin is None:
        raise FileNotFoundError(OLLAMA_INSTALL_HINT)

    prompt = build_ollama_prompt(normalized)
    result = subprocess.run(
        [ollama_bin, "run", model_name, prompt],
        check=True,
        capture_output=True,
        text=True,
    )
    output = normalize_ollama_output(result.stdout, normalized)
    if looks_english_output(output):
        return normalized
    return output


def build_ollama_batch_prompt(indexed_lines: list[tuple[int, str]]) -> str:
    joined_input = "\n".join(f"{index}\t{text}" for index, text in indexed_lines)
    glossary_hints = build_glossary_hints("\n".join(text for _, text in indexed_lines), max_items=20)
    return (
        "次の日本語字幕（複数行）を、意味を変えずに自然な書き言葉に整えてください。"
        "フィラーや言い淀みは除去し、短く明瞭にしてください。\n"
        "翻訳は絶対に行わず、日本語のまま出力してください。\n"
        "記号・数式・変数（例: K, T, P, Q）や固有名詞は、文脈上必要な場合を除いて勝手に変えないでください。\n"
        "用語集は文脈に合うときのみ適用してください。\n\n"
        "出力形式を厳守してください。説明文は不要です。\n"
        "各行を1行で返し、入力と同じIDを先頭に付けてください。\n"
        "形式: ID<TAB>整形後テキスト\n\n"
        f"[用語集]\n{glossary_hints}\n\n"
        f"[入力]\n{joined_input}\n\n"
        "[出力]"
    )


def normalize_ollama_candidate_text(text: str) -> str:
    normalized_text = text.strip()
    normalized_text = OUTPUT_LABEL_PATTERN.sub("", normalized_text).strip()
    normalized_text = normalized_text.replace("\n", " ").replace("\r", " ")
    normalized_text = re.sub(r"\s+", " ", normalized_text).strip()
    if (normalized_text.startswith("\"") and normalized_text.endswith("\"")) or (
        normalized_text.startswith("「") and normalized_text.endswith("」")
    ):
        normalized_text = normalized_text[1:-1].strip()
    return normalized_text


def looks_english_output(text: str) -> bool:
    english_chars = len(ENGLISH_CHAR_PATTERN.findall(text))
    japanese_chars = len(JAPANESE_CHAR_PATTERN.findall(text))
    if english_chars == 0:
        return False
    if japanese_chars == 0:
        return True
    return english_chars > japanese_chars


def parse_ollama_batch_output(raw_output: str, expected_ids: list[int]) -> dict[int, str] | None:
    id_set = set(expected_ids)
    parsed: dict[int, str] = {}
    for line in raw_output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in {"出力:", "整形後:", "修正後:", "回答:"}:
            continue

        match = re.match(r"^(?:[-*]\s*)?(\d+)\s*[\t:：]\s*(.+)$", stripped)
        if not match:
            continue

        line_id = int(match.group(1))
        if line_id not in id_set:
            continue

        candidate = normalize_ollama_candidate_text(match.group(2))
        if candidate:
            parsed[line_id] = candidate

    if not all(line_id in parsed for line_id in expected_ids):
        return None
    return parsed


def clean_lines_with_ollama(lines: list[str], model_name: str) -> list[str]:
    if not lines:
        return []

    normalized_lines = [clean_line_rule(line) for line in lines]
    output_lines = normalized_lines.copy()

    ollama_bin = shutil.which("ollama")
    if ollama_bin is None:
        raise FileNotFoundError(OLLAMA_INSTALL_HINT)

    batch_size = OLLAMA_BATCH_SIZE if OLLAMA_BATCH_SIZE > 0 else len(lines)
    total_batches = (len(lines) + batch_size - 1) // batch_size
    for batch_start in range(0, len(lines), batch_size):
        batch_index = (batch_start // batch_size) + 1
        batch_end = min(len(lines), batch_start + batch_size)
        batch_lines = normalized_lines[batch_start:batch_end]
        indexed_lines = [(i + 1, text) for i, text in enumerate(batch_lines)]
        prompt = build_ollama_batch_prompt(indexed_lines)

        print(f"[字幕整形] Ollama batch {batch_index}/{total_batches} ({len(batch_lines)} lines)")
        result = subprocess.run(
            [ollama_bin, "run", model_name, prompt],
            check=True,
            capture_output=True,
            text=True,
        )

        expected_ids = [index for index, _ in indexed_lines]
        parsed = parse_ollama_batch_output(result.stdout, expected_ids)
        if parsed is None:
            # Fall back to per-line inference for this batch if format parsing fails.
            for offset, original_line in enumerate(lines[batch_start:batch_end]):
                fallback = clean_line_with_ollama(original_line, model_name)
                output_lines[batch_start + offset] = fallback if fallback else original_line.strip()
            continue

        for offset, line_id in enumerate(expected_ids):
            candidate = parsed.get(line_id, "")
            if candidate:
                if looks_english_output(candidate):
                    output_lines[batch_start + offset] = batch_lines[offset]
                    continue
                output_lines[batch_start + offset] = candidate

    return output_lines


def normalize_ollama_output(raw_output: str, fallback: str) -> str:
    # Keep SRT one-line subtitle text stable even when the model emits labels or extra lines.
    lines = [line.strip() for line in raw_output.splitlines() if line.strip()]
    if not lines:
        return fallback

    cleaned_candidates: list[str] = []
    for line in lines:
        if line in {"出力:", "整形後:", "修正後:"}:
            continue
        if line.startswith("入力:"):
            continue
        normalized_line = OUTPUT_LABEL_PATTERN.sub("", line).strip()
        if normalized_line:
            cleaned_candidates.append(normalized_line)

    if not cleaned_candidates:
        return fallback

    candidate = cleaned_candidates[0]
    candidate = candidate.replace("\n", " ").replace("\r", " ")
    candidate = re.sub(r"\s+", " ", candidate).strip()
    if (candidate.startswith("\"") and candidate.endswith("\"")) or (
        candidate.startswith("「") and candidate.endswith("」")
    ):
        candidate = candidate[1:-1].strip()
    return candidate if candidate else fallback


def clean_jp_srt(
    jp_srt: Path,
    jp_clean_srt: Path,
    mode: str,
    ollama_model: str,
    force: bool,
) -> None:
    if jp_clean_srt.exists() and not force:
        print(f"[skip] cleaned jp subtitle already exists: {jp_clean_srt}")
        return

    source_text = jp_srt.read_text(encoding="utf-8")

    if mode == "off":
        jp_clean_srt.write_text(source_text, encoding="utf-8")
        print(f"[字幕整形] スキップしてコピー: {jp_clean_srt}")
        return

    if mode == "ollama":
        ensure_ollama()
        ensure_ollama_model(ollama_model)

    print(f"[字幕整形] {jp_clean_srt}")
    blocks = [parse_srt_block(lines) for lines in split_srt_blocks(source_text)]

    if mode == "ollama":
        positions: list[tuple[int, int]] = []
        source_lines: list[str] = []
        for block_index, block in enumerate(blocks):
            for line_index, line in enumerate(block.text_lines):
                if not line.strip():
                    continue
                positions.append((block_index, line_index))
                source_lines.append(line)

        cleaned_all_lines = clean_lines_with_ollama(source_lines, ollama_model)
        if len(cleaned_all_lines) != len(positions):
            raise RuntimeError(
                "Ollama の整形結果行数が入力行数と一致しません。"
            )
        for (block_index, line_index), cleaned in zip(positions, cleaned_all_lines):
            blocks[block_index].text_lines[line_index] = cleaned if cleaned else blocks[block_index].text_lines[line_index].strip()

        rendered_blocks = [render_srt_block(block) for block in blocks]
        jp_clean_srt.write_text("\n\n".join(rendered_blocks) + ("\n" if rendered_blocks else ""), encoding="utf-8")
        return

    rendered_blocks: list[str] = []
    for block in blocks:
        cleaned_lines: list[str] = []
        for line in block.text_lines:
            if not line.strip():
                cleaned_lines.append("")
                continue
            cleaned = clean_line_rule(line)
            cleaned_lines.append(cleaned if cleaned else line.strip())
        rendered_blocks.append(
            render_srt_block(
                SRTBlock(index=block.index, timecode=block.timecode, text_lines=cleaned_lines)
            )
        )

    jp_clean_srt.write_text("\n\n".join(rendered_blocks) + ("\n" if rendered_blocks else ""), encoding="utf-8")