"""
Generate a JSONL dataset of question-answer pairs using a Bedrock model via boto3.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

import boto3
import config

AWS_PROFILE = "default"
AWS_REGION = "us-east-1"
MODEL_ID = config.MODEL_ID
OUTPUT_FILE = "qa_dataset.jsonl"

NUM_SAMPLES = 2
TEMPERATURE = 0.7
TOP_P = 0.9
MAX_TOKENS = 768
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 1.5
USE_CONVERSE = True

CREATION_PROMPT = "Generate a question and answer pair in JSON format with the keys 'question' and 'answer'. The question should be about banking and finance, and the answer should be concise and accurate, and always roleplayed as a pirate. Format the output as a JSON object, for example: {\"question\": \"\", \"answer\": \"\"}"


def invoke_model(client: Any, prompt: str, attempt: int = 1) -> str:
    """
    Calls the model and returns the generated text.
    """
    if USE_CONVERSE:
        response = client.converse(
            modelId=MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={
                "temperature": TEMPERATURE,
                "topP": TOP_P,
                "maxTokens": MAX_TOKENS,
            },
        )

        output = response.get("output", {}).get("message", {})
        return "".join(
            chunk.get("text", "")
            for chunk in output.get("content", [])
            if chunk.get("text")
        )

    # Fallback path for older/legacy inference APIs.
    response = client.invoke_model(
        modelId=MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(
            {
                "prompt": prompt,
                "max_tokens": MAX_TOKENS,
                "temperature": TEMPERATURE,
                "top_p": TOP_P,
                "sample_count": 1,
            }
        ),
    )
    body = response.get("body").read().decode("utf-8")
    parsed = json.loads(body)

    # Best-effort extraction for multiple common schemas.
    if isinstance(parsed, dict):
        if "text" in parsed and isinstance(parsed["text"], str):
            return parsed["text"]
        completions = parsed.get("completions") or []
        if completions:
            first = completions[0]
            if isinstance(first, dict) and isinstance(first.get("text"), str):
                return first["text"]
        output = parsed.get("output") or {}
        if isinstance(output, dict) and isinstance(output.get("text"), str):
            return output["text"]
        if isinstance(parsed.get("generation"), str):
            return parsed["generation"]
    raise RuntimeError(f"Unable to extract text from model response on attempt {attempt}: {body}")


def clean_model_text(text: str) -> str:
    return text.strip()


def parse_qa(text: str) -> Dict[str, str]:
    """
    Parse JSON {"question": ..., "answer": ...} from model output.
    """
    candidate = clean_model_text(text)

    code_block_match = re.search(r"```(?:json)?(.*?)```", candidate, flags=re.DOTALL | re.IGNORECASE)
    if code_block_match:
        candidate = code_block_match.group(1).strip()

    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            question = parsed.get("question")
            answer = parsed.get("answer")
            if isinstance(question, str) and isinstance(answer, str):
                return {"question": question.strip(), "answer": answer.strip()}
    except json.JSONDecodeError:
        pass

    # Fallback heuristic parse for text responses like:
    # Question: ...
    # Answer: ...
    q_match = re.search(r"question\s*:\s*(.+)", candidate, flags=re.IGNORECASE)
    a_match = re.search(r"answer\s*:\s*(.+)", candidate, flags=re.IGNORECASE)
    if q_match and a_match:
        return {
            "question": q_match.group(1).strip(),
            "answer": a_match.group(1).strip(),
        }

    raise ValueError("Model output is not in the expected format. Expected JSON with question/answer.")


def build_prompt() -> str:
    if not CREATION_PROMPT.strip():
        raise ValueError(
            "CREATION_PROMPT is empty. Fill it in with your dataset generation instructions."
        )
    return CREATION_PROMPT.strip()


def generate_dataset(count: int, output_path: Path) -> None:
    session = boto3.session.Session(profile_name=AWS_PROFILE)
    client = session.client("bedrock-runtime", region_name=AWS_REGION)

    generated = []
    base_prompt = build_prompt()

    for i in range(count):
        attempt = 0
        while attempt < MAX_RETRIES:
            attempt += 1
            try:
                text = invoke_model(client, base_prompt, attempt=attempt)
                qa = parse_qa(text)
                generated.append(qa)
                break
            except Exception as exc:
                if attempt >= MAX_RETRIES:
                    raise RuntimeError(f"Failed to generate a valid QA pair on item {i + 1}: {exc}") from exc
                time.sleep(RETRY_DELAY_SECONDS * attempt)

    with output_path.open("w", encoding="utf-8") as f:
        for row in generated:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate QA dataset from LLM output into JSONL.")
    parser.add_argument(
        "--count",
        type=int,
        default=NUM_SAMPLES,
        help="Number of Q&A pairs to generate.",
    )
    parser.add_argument(
        "--output",
        default=OUTPUT_FILE,
        help="Output JSONL file path.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_path = Path(args.output)
    generate_dataset(args.count, output_path)


if __name__ == "__main__":
    main()
