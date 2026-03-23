import os
import json
import random
import glob
import re
import time
import unicodedata
from datetime import datetime
import pandas as pd
import boto3
from botocore.exceptions import ClientError

import config

NO_GENERATION_SENTINEL = "NO_APLICA"

QUERY_STYLES = [
    {
        "style_name": "Buscador de Palabras Clave",
        "description": (
            "El usuario no redacta una oración completa. Escribe 2-4 fragmentos sueltos "
            "separados por espacios o comas, como si buscara en Google. "
            "Ejemplo: 'requisitos pie subsidio', 'seguro desgravamen edad maxima', 'renta minima postulacion'. "
            "USA EL SENTINEL si el fragmento no contiene conceptos que un usuario expresaría como palabras clave "
            "(por ejemplo, texto puramente narrativo o legal sin términos accionables)."
        )
    },
    {
        "style_name": "Caso Hipotetico en Primera Persona",
        "description": (
            "El usuario plantea una situación personal con cifras o condiciones concretas para ver "
            "si el texto se aplica a él. Usa estructuras como 'Si yo gano X...', 'Qué pasa si tengo Y años...', "
            "'En caso de que mi contrato sea...'. "
            "Las cifras deben ser INVENTADAS por ti — verosímiles para un trabajador chileno medio, "
            "pero NO copiadas del fragmento. "
            "USA EL SENTINEL si el fragmento no contiene ninguna condición, umbral o requisito "
            "que un usuario pudiera contrastar con su situación personal."
        )
    },
    {
        "style_name": "Coloquial Chileno Natural",
        "description": (
            "El usuario escribe como si enviara varios mensajes cortos por WhatsApp en ráfaga, "
            "no una pregunta formal y completa. Tono cercano, directo, con modismos chilenos. "
            "Puede usar 'oye', 'po', 'cachai', 'me conviene', 'qué onda con'. "
            "La consulta puede ser incompleta o terminar con '?' sin sujeto explícito. "
            "Ejemplo: 'oye y el subsidio pa arrendatarios aplica si llevo menos de un año?'."
        )
    },
    {
        "style_name": "Principiante / Educativo",
        "description": (
            "El usuario admite no entender el tema y pide que le expliquen un concepto básico. "
            "Usa estructuras como '¿Qué significa...?', '¿Cómo funciona eso de...?', "
            "'No entiendo bien qué es...', 'Me puedes explicar...'. "
            "El concepto preguntado debe estar IMPLÍCITO en el fragmento, no nombrado literalmente. "
            "USA EL SENTINEL si el fragmento es puramente operativo o procedimental "
            "y no contiene ningún concepto que requiera definición."
        )
    },
    {
        "style_name": "Orientado a la Accion",
        "description": (
            "El usuario quiere saber qué tiene que HACER: pasos, documentos, lugares, plazos, personas. "
            "Pregunta en imperativo o con 'cómo': '¿Dónde llevo los papeles?', '¿Con quién hablo?', "
            "'¿Qué hago primero?', '¿Cuánto tiempo demora?'. "
            "USA EL SENTINEL si el fragmento es puramente descriptivo o conceptual "
            "y no contiene ninguna acción, paso o trámite."
        )
    },
    {
        "style_name": "Mal Escrito",
        "description": (
            "El usuario escribe mal, con faltas ortográficas, sin respetar reglas gramaticales, con errores de tipeo."
        )
    },
    {
        "style_name": "Errores Ortograficos",
        "description": (
            "El usuario escribe con errores ortográficos fonéticos en palabras clave del dominio hipotecario: "
            "escribe como suena, no como se escribe. Ejemplos: "
            "'ipoteca' (hipoteca), 'subsídeo' (subsidio), 'rrequisitos' (requisitos), "
            "'haorro' (ahorro), 'abaluo' (avalúo), 'desgramen' (desgravamen). "
            "USA EL SENTINEL si el fragmento no contiene ninguna palabra técnica del dominio "
            "que un usuario podría escribir mal fonéticamente."
        )
    },
]



def extract_bd_code(filename):
    if not filename:
        return ""
    return filename[:9]


def get_bedrock_client():
    session = boto3.Session(profile_name=config.AWS_PROFILE_LLM)
    return session.client(service_name="bedrock-runtime", region_name=config.AWS_REGION)


def ensure_parent_dir(path):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def append_row_to_csv(path, row):
    ensure_parent_dir(path)
    file_exists = os.path.exists(path)
    has_content = file_exists and os.path.getsize(path) > 0
    pd.DataFrame([row]).to_csv(
        path,
        mode="a" if has_content else "w",
        header=not has_content,
        index=False,
    )


def append_progress(progress_log_path, file_path, style_name, status):
    ensure_parent_dir(progress_log_path)
    with open(progress_log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "file_path": file_path,
            "style_name": style_name,
            "status": status
        }, ensure_ascii=False) + "\n")


def load_processed_pairs(progress_log_path):
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
                file_path = obj.get("file_path")
                style_name = obj.get("style_name")
                status = obj.get("status")
                if file_path and style_name and status in {"generated", "skipped_no_generation"}:
                    processed.add((file_path, style_name))
            except json.JSONDecodeError:
                continue
    return processed


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
    cleaned_text = re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=re.DOTALL)
    return cleaned_text.strip()


def normalize_style_name(style_name):
    if not style_name:
        return ""
    normalized = unicodedata.normalize("NFKD", style_name)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = re.sub(r"\s+", " ", normalized).strip().lower()
    return normalized


def parse_llm_xml(content, allowed_styles):
    content_no_reasoning = clean_llm_output(content)

    style_match = re.search(r"<style_name>(.*?)</style_name>", content_no_reasoning, re.DOTALL | re.IGNORECASE)
    input_match = re.search(r"<user_input>(.*?)</user_input>", content_no_reasoning, re.DOTALL | re.IGNORECASE)

    if not (style_match and input_match):
        return None, None, content_no_reasoning

    style_found = style_match.group(1).strip()
    question_text = input_match.group(1).strip()
    question_text = question_text.replace('"', "").replace("\n", " ").strip()

    if not question_text:
        return None, None, content_no_reasoning

    if allowed_styles:
        style_found_norm = normalize_style_name(style_found)
        matched_allowed_style = None
        for allowed_style in allowed_styles:
            if normalize_style_name(allowed_style) == style_found_norm:
                matched_allowed_style = allowed_style
                break
        if not matched_allowed_style:
            return None, None, content_no_reasoning
        style_found = matched_allowed_style

    return question_text, style_found, content_no_reasoning


def repair_xml_response(raw_content, allowed_styles, client, error_log):
    allowed_str = ", ".join(allowed_styles) if allowed_styles else ""
    repair_prompt = f"""
Extrae la consulta del usuario y el estilo desde el siguiente texto y devuelve SOLO el formato XML requerido.

Texto original:
{raw_content}

Estilos permitidos: {allowed_str}

Formato requerido (sin markdown, sin texto adicional):
<style_name>NOMBRE_DEL_ESTILO</style_name>
<user_input>CONSULTA_DEL_USUARIO</user_input>
"""

    body = json.dumps({
        "messages": [{"role": "user", "content": repair_prompt}],
        "temperature": 0.0,
        "max_tokens": 500,
    })

    def _call():
        return client.invoke_model(
            modelId=config.MODEL_ID,
            body=body
        )

    response = call_with_retry(_call, "invoke_model_repair", error_log)
    if response is None:
        return None, None, None

    response_body = json.loads(response.get("body").read().decode("utf-8"))
    if "choices" in response_body:
        content = response_body["choices"][0]["message"]["content"]
    elif "output" in response_body:
        content = response_body["output"]["message"]["content"]
    else:
        content = str(response_body)

    return parse_llm_xml(content, allowed_styles)


def generate_question_for_style(chunk_text, query_style, client, error_log, parse_fail_log_path):
    style_name = query_style["style_name"]
    style_description = query_style["description"]
    allowed_styles = [style_name]

    system_prompt = f"""
<rol>
You are a specialist in synthetic data generation for evaluating RAG (Retrieval-Augmented Generation) systems.
Your task is to build the Test Set for the AI assistant "Vivi" of the Casaverso portal of Banco Estado, Chile.
</rol>

<contexto_del_sistema>
Vivi is a conversational assistant of Banco Estado that helps people understand and apply for a crédito hipotecario.
The real users of the system are mostly people from middle and working-class sectors in Chile — dependent or independent workers, many with a secondary education level, who are looking to buy their first home using subsidios habitacionales (DS49, DS19, etc.) or créditos hipotecarios from Banco Estado.
Many use their phones to make queries. Their vocabulary is everyday, not technical.
</contexto_del_sistema>

<tarea>
You will be given a text fragment extracted from the Casaverso knowledge base (the "chunk").
Your only task is to write the user query that, in a real scenario, would lead the RAG system to retrieve that fragment as a relevant response.
You must not summarize, explain, or comment on the fragment. Only generate the query.
</tarea>

<perfil_del_usuario_simulado>
The person writing the query:
- Has NOT read the fragment. They will never see it.
- Does not know the technical terms, decree names, exact percentages, or legal articles in the text.
- Has a concrete need (buying a house, understanding a requirement, knowing how much they can borrow, etc.).
- Writes the way they talk: with everyday Chilean vocabulary, possible punctuation or typing errors, short or incomplete sentences.
</perfil_del_usuario_simulado>

<reglas_criticas>
RULE 1 — INFORMATION ASYMMETRY (the most important)
The query must arise from a need, NOT from the content of the text.
The user expresses what they WANT TO KNOW, not what the text SAYS.

✗ INCORRECT (contaminated with the text):
    Text: "El subsidio DS49 permite un ahorro mínimo de 50 UF para postular."
    Generated query: "¿Cuánto ahorro mínimo pide el DS49?"
    → The user used the exact name of the decree and the concept exactly as it appears in the text.

✓ CORRECT (arises from the need):
    Generated query: "cuánta plata tengo que tener ahorrada para poder postular a una casa?"
    → The user expresses their real doubt without knowing what the subsidio is called or that a minimum in UF exists.

RULE 2 — COPYING PHRASES FROM THE TEXT IS FORBIDDEN
** MANDATORY ** AND VERY IMPORTANT. NEVER skip this rule:
You must avoid copying phrases, technical terms, or proper names from the fragment into the generated query.
If the text says "Tasa de Interés Preferencial", the query can say "cómo están las tasas" or "me conviene más tasa fija o variable", never "Tasa de Interés Preferencial".

RULE 3 — ABSTRACTION
The fragment is the answer to a concrete human doubt. Think about what concrete, everyday doubt that fragment would answer.

RULE 4 — AUTHENTIC CHILEAN VOCABULARY
Use the real linguistic register of the target users. Examples of appropriate vocabulary:
- "la casa propia", "el crédito", "postular", "el subsidio", "los papeles", "la cuota", "cuánto me sale"
- Valid colloquialisms depending on style: "oye", "po", "cachai", "me conviene", "me alcanza pa", "qué tan difícil es"
- Avoid banking technicalities, legalisms, and marketing phrases.

RULE 5 — ONE SINGLE QUERY, ONE SINGLE INTENT
The query must reflect ONE concrete need. Do not combine multiple questions into a single query.
</reglas_criticas>

<guia_por_estilo>
Apply these additional restrictions according to the assigned style:

- Buscador de Palabras Clave: maximum 5 words total, no conjugated verbs, no question marks.
- Caso Hipotético en Primera Persona: invent the user's figures — do NOT copy them from the fragment. They must be plausible for our end users.
- Coloquial Chileno Natural: maximum 2 sentences. At least one Chilean colloquialism present.
- Principiante / Educativo: must include a phrase admitting ignorance ("no entiendo", "no sé qué es", "me puedes explicar").
- Orientado a la Acción: must include at least one operational action verb (llevar, hacer, ir, mandar, pedir, hablar).
- Mal Escrito: there must be at least one spelling or grammar error.
- Errores Ortográficos: at least one technical domain word must be spelled phonetically incorrectly.
</guia_por_estilo>

<instruccion_de_estilo>
You must write the query using EXCLUSIVELY the following style:

Name: {style_name}
Description: {style_description}

If the delivered fragment does not allow generating a realistic and coherent query under that style (for example, the content is too technical, administrative, or outside the scope of that user profile), then respond inside the <user_input> tag with exactly this text, without modifying it:
{NO_GENERATION_SENTINEL}
Do not use any other phrase, explanation, or alternative.
</instruccion_de_estilo>

<output_restriction>
ABSOLUTE RESTRICTION — OUTPUT LANGUAGE:
You must ALWAYS write the generated query in Spanish, using Chilean everyday vocabulary, regardless of the language of any instruction, fragment, or context provided to you. Generating the query in any language other than Spanish is strictly forbidden.
</output_restriction>

<formato_de_salida>
Respond ONLY with the following XML. No markdown. No preceding or following text. No quotes around values. No line breaks inside tags.

<style_name>{style_name}</style_name>
<user_input>LA_CONSULTA_GENERADA_AQUÍ</user_input>

The value inside <style_name> must be EXACTLY: {style_name}
The value inside <user_input> must be only the query, or the sentinel {NO_GENERATION_SENTINEL} if it does not apply.
</formato_de_salida>
"""

    # User / human turn — keep lean, just the dynamic content
    prompt = f"""
<chunk>
{chunk_text}
</chunk>
"""
    
    
    body = json.dumps({
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
        "temperature": config.TEMPERATURE,
        "max_tokens": 4000
    })

    def _call():
        return client.invoke_model(
            modelId=config.MODEL_ID,
            body=body
        )

    response = call_with_retry(_call, "invoke_model", error_log)
    if response is None:
        return None, None

    response_body = json.loads(response.get("body").read().decode("utf-8"))

    if "choices" in response_body:
        content = response_body["choices"][0]["message"]["content"]
    elif "output" in response_body:
        content = response_body["output"]["message"]["content"]
    else:
        content = str(response_body)

    question_text, style_found, cleaned = parse_llm_xml(content, allowed_styles)
    if question_text and style_found:
        return question_text, style_found

    repair_q, repair_style, _ = repair_xml_response(content, allowed_styles, client, error_log)
    if repair_q and repair_style:
        return repair_q, repair_style

    ensure_parent_dir(parse_fail_log_path)
    with open(parse_fail_log_path, "a", encoding="utf-8") as log_file:
        log_file.write(json.dumps({
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "reason": "parse_failed",
            "allowed_styles": allowed_styles,
            "raw_response": cleaned,
        }, ensure_ascii=False) + "\n")

    print(f"Warning: Could not parse XML from LLM response: {cleaned[:100]}...")
    return None, None


def main():
    random.seed(config.SEED)
    print(f"Using seed: {config.SEED}")
    print(f"Scanning for files in {config.KB_FOLDER}...")

    if not os.path.exists(config.KB_FOLDER):
        print(f"Error: Directory {config.KB_FOLDER} does not exist.")
        return

    search_pattern = os.path.join(config.KB_FOLDER, "**", "*.md")
    files = sorted(glob.glob(search_pattern, recursive=True))

    if not files:
        print(f"No .md files found in {config.KB_FOLDER} or its subdirectories.")
        return

    print(f"Found {len(files)} Markdown files.")

    client = get_bedrock_client()
    error_log = []
    parse_failures = 0
    generated_count = 0
    skipped_by_style_count = 0
    parse_fail_log_path = os.path.join(
        os.path.dirname(config.PIPELINE_CSV),
        "parse_failures.jsonl"
    )
    progress_log_path = os.path.join(
        os.path.dirname(config.PIPELINE_CSV),
        "generation_progress.jsonl"
    )

    processed_pairs = load_processed_pairs(progress_log_path)
    print(f"Resuming with {len(processed_pairs)} completed file/style pairs.")
    print("Generating synthetic questions...")

    for i, file_path in enumerate(files):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                chunk_text = f.read()

            if len(chunk_text) < 50:
                continue

            print(f"[{i + 1}/{len(files)}] Processing {os.path.basename(file_path)}")
            for style_idx, style in enumerate(QUERY_STYLES, start=1):
                style_name = style["style_name"]
                pair_key = (file_path, style_name)
                if pair_key in processed_pairs:
                    continue

                print(f"  - Style [{style_idx}/{len(QUERY_STYLES)}]: {style_name}")
                generated_question, style_used = generate_question_for_style(
                    chunk_text,
                    style,
                    client,
                    error_log,
                    parse_fail_log_path
                )

                if not (generated_question and style_used):
                    parse_failures += 1
                    continue

                if generated_question == NO_GENERATION_SENTINEL:
                    skipped_by_style_count += 1
                    append_progress(progress_log_path, file_path, style_name, "skipped_no_generation")
                    processed_pairs.add(pair_key)
                    continue

                row = {
                    "user_input": generated_question,
                    "reference_contexts": [chunk_text],
                    "query_style": style_used,
                    "source_file": extract_bd_code(os.path.basename(file_path)),
                    "filepath": os.path.relpath(file_path, config.KB_FOLDER)
                }
                append_row_to_csv(config.PIPELINE_CSV, row)
                generated_count += 1
                append_progress(progress_log_path, file_path, style_name, "generated")
                processed_pairs.add(pair_key)
        except Exception as e:
            print(f"Error reading file {file_path}: {e}")

    if generated_count > 0:
        print(f"Successfully generated {generated_count} test cases. Saved incrementally to {config.PIPELINE_CSV}")
    else:
        print("No data generated.")

    if error_log or parse_failures or skipped_by_style_count:
        print(
            "Non-fatal errors: "
            f"{len(error_log)} | Parse failures: {parse_failures} | "
            f"Skipped by style mismatch: {skipped_by_style_count}"
        )
        summary_path = os.path.join(
            os.path.dirname(config.PIPELINE_CSV),
            "run_summary.json"
        )
        ensure_parent_dir(summary_path)
        with open(summary_path, "w", encoding="utf-8") as summary_file:
            json.dump({
                "generated": generated_count,
                "parse_failures": parse_failures,
                "skipped_by_style_mismatch": skipped_by_style_count,
                "errors": error_log,
            }, summary_file, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
