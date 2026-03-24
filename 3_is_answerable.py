import ast
import csv
import json
import os
import random
import re
import time
from datetime import datetime

import boto3
from botocore.exceptions import ClientError

import config

INPUT_FILEPATH = config.PIPELINE_CSV_RETRIEVER
OUTPUT_FILEPATH = config.PIPELINE_CSV_ANSWERABLE


def get_bedrock_client():
    session = boto3.Session(profile_name=config.AWS_PROFILE_LLM)
    return session.client(service_name="bedrock-runtime", region_name=config.AWS_REGION)


def ensure_parent_dir(path):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def backoff_sleep(attempt):
    base = config.BACKOFF_BASE_SECONDS * (2 ** attempt)
    sleep_for = min(base, config.BACKOFF_MAX_SECONDS)
    sleep_for += random.uniform(0, config.BACKOFF_JITTER_SECONDS)
    time.sleep(sleep_for)


def call_with_retry(fn, operation_name, error_log):
    last_error = None
    for attempt in range(config.MAX_RETRIES + 1):
        try:
            return fn()
        except ClientError as e:
            last_error = e
        except Exception as e:
            last_error = e

        if attempt < config.MAX_RETRIES:
            backoff_sleep(attempt)
        else:
            if last_error is not None:
                error_log.append({
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "operation": operation_name,
                    "error": str(last_error),
                })
            return None


def clean_llm_output(text):
    cleaned_text = re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=re.DOTALL | re.IGNORECASE)
    return cleaned_text.strip()


def extract_is_answerable_tag(text):
    cleaned = clean_llm_output(text)
    match = re.search(
        r"<is_answerable>\s*(yes|no)\s*</is_answerable>",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None, cleaned
    return match.group(1).strip().lower(), cleaned


def append_progress(progress_log_path, row_index, status):
    ensure_parent_dir(progress_log_path)
    with open(progress_log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "row_index": row_index,
            "status": status,
        }, ensure_ascii=False) + "\n")


def load_processed_rows(progress_log_path):
    processed = set()
    if not os.path.exists(progress_log_path):
        return processed

    with open(progress_log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            row_index = obj.get("row_index")
            status = obj.get("status")
            if isinstance(row_index, int) and status in {"generated", "error"}:
                processed.add(row_index)
    return processed


def parse_retrieved_contexts(raw_value):
    if not raw_value:
        return []

    try:
        parsed = ast.literal_eval(raw_value)
    except (ValueError, SyntaxError):
        return [raw_value]

    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return [str(parsed)]


def format_retrieved_contexts(raw_value):
    contexts = parse_retrieved_contexts(raw_value)
    if not contexts:
        return "No retrieved contexts were provided."

    formatted_contexts = []
    for idx, context in enumerate(contexts, start=1):
        formatted_contexts.append(f"[Context {idx}]\n{context}")
    return "\n\n".join(formatted_contexts)


def get_response_text(response):
    response_body = json.loads(response.get("body").read().decode("utf-8"))
    if "choices" in response_body:
        return response_body["choices"][0]["message"]["content"]
    if "output" in response_body:
        return response_body["output"]["message"]["content"]
    return str(response_body)


def append_row_to_csv(path, row, fieldnames, dialect):
    ensure_parent_dir(path)
    file_exists = os.path.exists(path)
    has_content = file_exists and os.path.getsize(path) > 0

    with open(path, "a" if has_content else "w", encoding="utf-8", newline="") as outfile:
        writer = csv.DictWriter(
            outfile,
            fieldnames=fieldnames,
            delimiter=dialect.delimiter,
            quotechar='"',
            doublequote=True,
            quoting=csv.QUOTE_MINIMAL,
            lineterminator="\n",
        )
        if not has_content:
            writer.writeheader()
        writer.writerow(row)


def classify_answerability(user_input, retrieved_contexts, client, error_log, parse_fail_log_path):
    system_prompt = """
You are judging whether a RAG assistant could answer a user's question using ONLY the retrieved contexts provided.

Decision rule:
- Return '<is_answerable>yes</is_answerable>' only if the retrieved contexts are sufficient for an honest, correct, factual, and satisfactorily complete answer.
- Return '<is_answerable>no</is_answerable>' if the contexts are missing essential information, only partially cover the question, require outside knowledge, are too vague, or would force the agent to guess.
- Return '<is_answerable>no</is_answerable>' if the user input is so unclear or inappropriate that you cannot really determine what information is being asked for, even with the contexts. You do need some leeway, as our users are not always clear or well-formed, but if the input simply does not have a clear meaning, you should err on the side of "no".
- The assistant must rely ONLY on the retrieved contexts. IT CAN NEVER rely on its own world knowledge, nor can you to decide for a yes/no answer. REFERENCE CONTEXT is the ONLY SOURCE OF TRUTH.
- If a safe and honest answer would mainly be "I don't have enough information" or similar, return '<is_answerable>no</is_answerable>'.

Output format:
Return ONLY one of these two exact strings:
<is_answerable>yes</is_answerable>
<is_answerable>no</is_answerable>
""".strip()

    user_prompt = f"""
Evaluate the following case.

User input:
{user_input}

Retrieved contexts:
{format_retrieved_contexts(retrieved_contexts)}
""".strip()

    body = json.dumps({
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 400,
    })

    for attempt in range(config.MAX_RETRIES + 1):
        def _call():
            return client.invoke_model(
                modelId=config.MODEL_ID,
                body=body,
            )

        response = call_with_retry(_call, "invoke_model_is_answerable", error_log)
        if response is None:
            return None

        content = get_response_text(response)
        parsed_value, cleaned = extract_is_answerable_tag(content)
        if parsed_value:
            return parsed_value

        if attempt < config.MAX_RETRIES:
            backoff_sleep(attempt)
            continue

        ensure_parent_dir(parse_fail_log_path)
        with open(parse_fail_log_path, "a", encoding="utf-8") as log_file:
            log_file.write(json.dumps({
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "reason": "parse_failed",
                "user_input": user_input,
                "raw_response": cleaned,
            }, ensure_ascii=False) + "\n")
        return None


def detect_csv_dialect(path):
    with open(path, "r", encoding="utf-8", newline="") as csvfile:
        sample = csvfile.read(8192)

    if not sample:
        return csv.excel

    try:
        return csv.Sniffer().sniff(sample)
    except csv.Error:
        return csv.excel


def build_output_fieldnames(input_fieldnames):
    output_fieldnames = []
    inserted = False

    for fieldname in input_fieldnames:
        output_fieldnames.append(fieldname)
        if fieldname == "user_input":
            output_fieldnames.append("is_answerable")
            inserted = True

    if not inserted:
        raise ValueError("Missing required column 'user_input'.")

    return output_fieldnames


def build_output_row(row, input_fieldnames, answerability_value):
    output_row = {}
    for fieldname in input_fieldnames:
        output_row[fieldname] = row.get(fieldname, "")
        if fieldname == "user_input":
            output_row["is_answerable"] = answerability_value
    return output_row


def main():
    random.seed(config.SEED)
    print(f"Loading {INPUT_FILEPATH}...")

    if not os.path.exists(INPUT_FILEPATH):
        print("Input file not found. Run File 2 first.")
        return

    dialect = detect_csv_dialect(INPUT_FILEPATH)
    parse_fail_log_path = os.path.join(
        os.path.dirname(OUTPUT_FILEPATH),
        "is_answerable_parse_failures.jsonl"
    )
    progress_log_path = os.path.join(
        os.path.dirname(OUTPUT_FILEPATH),
        "is_answerable_progress.jsonl"
    )
    summary_path = os.path.join(
        os.path.dirname(OUTPUT_FILEPATH),
        "run_summary_3.json"
    )

    error_log = []
    parse_failures = 0
    processed_count = 0

    try:
        with open(INPUT_FILEPATH, "r", encoding="utf-8", newline="") as infile:
            reader = csv.DictReader(infile, dialect=dialect)
            input_fieldnames = reader.fieldnames or []

            if "user_input" not in input_fieldnames:
                print("Missing required column 'user_input'. Run File 1 first.")
                return
            if "retrieved_contexts" not in input_fieldnames:
                print("Missing required column 'retrieved_contexts'. Run File 2 first.")
                return

            output_fieldnames = build_output_fieldnames(input_fieldnames)
            ensure_parent_dir(OUTPUT_FILEPATH)
            processed_rows = load_processed_rows(progress_log_path)

            client = get_bedrock_client()

            print(f"Resuming with {len(processed_rows)} completed rows.")
            print("Starting answerability classification...")
            rows = list(reader)
            total_rows = len(rows)

            for row_index, row in enumerate(rows):
                if row_index in processed_rows:
                    continue

                display_index = row_index + 1
                user_input = row.get("user_input", "")
                retrieved_contexts = row.get("retrieved_contexts", "")
                print(f"[{display_index}/{total_rows}] Judging: {user_input[:50]}...")

                answerability_value = classify_answerability(
                    user_input=user_input,
                    retrieved_contexts=retrieved_contexts,
                    client=client,
                    error_log=error_log,
                    parse_fail_log_path=parse_fail_log_path,
                )

                if answerability_value is None:
                    parse_failures += 1
                    answerability_value = "failed"
                    progress_status = "error"
                else:
                    progress_status = "generated"

                output_row = build_output_row(row, input_fieldnames, answerability_value)
                append_row_to_csv(
                    path=OUTPUT_FILEPATH,
                    row=output_row,
                    fieldnames=output_fieldnames,
                    dialect=dialect,
                )
                append_progress(progress_log_path, row_index, progress_status)
                processed_count += 1
    finally:
        if error_log or parse_failures:
            ensure_parent_dir(summary_path)
            with open(summary_path, "w", encoding="utf-8") as summary_file:
                json.dump({
                    "processed": processed_count,
                    "parse_failures": parse_failures,
                    "errors": error_log,
                }, summary_file, ensure_ascii=False, indent=2)

    print(f"Answerability classification complete. Wrote {processed_count} new rows to {OUTPUT_FILEPATH}")


if __name__ == "__main__":
    main()
