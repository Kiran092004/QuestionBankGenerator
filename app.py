
import os
import re
import json
import time
import unicodedata
import difflib
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
import streamlit as st

# Optional libs
try:
    import pdfplumber
except Exception:
    pdfplumber = None

try:
    from docx import Document
except Exception:
    Document = None

try:
    import google.generativeai as genai
except Exception:
    genai = None

try:
    import mysql.connector
except Exception:
    mysql = None

# ------------------ Config & folders ------------------
load_dotenv()
BASE_DIR = Path(__file__).resolve().parent
DOCS_DIR = Path(os.getenv('DOCS_DIR') or BASE_DIR / 'docs')
OUTPUT_DIR = Path(os.getenv('OUTPUT_DIR') or BASE_DIR / 'output_jsons')
UPLOAD_DIR = Path(os.getenv('UPLOAD_DIR') or BASE_DIR / 'uploads')
LOG_FILE = BASE_DIR / 'streamlit_app.log'

for p in (DOCS_DIR, OUTPUT_DIR, UPLOAD_DIR):
    p.mkdir(parents=True, exist_ok=True)

# ------------------ UI styling ------------------
st.set_page_config(page_title='Assessment Question Generator', layout='wide', initial_sidebar_state='expanded')
DARK_CSS = """
<style>
body { background-color: #0f1720; color: #e6eef6; }
.header { color: #a7f3d0; }
.card { background:#0b1220; border-radius:12px; padding:18px; box-shadow: 0 8px 24px rgba(0,0,0,0.6); }
.small { font-size:0.9rem; color:#bcdff9 }
.logs { background:#021019; color:#cfeefe; padding:10px; border-radius:6px; font-family:monospace; }
.btn { background: linear-gradient(90deg,#0ea5a5,#06b6d4); color:#052; border: none; padding: 8px 14px; border-radius:8px; }
</style>
"""
st.markdown(DARK_CSS, unsafe_allow_html=True)

# ------------------ MASTER_PROMPT (preserved exactly as provided) ------------------
MASTER_PROMPT = """
You are an expert teacher, assessment designer, and Bloom’s Taxonomy specialist. Generate curriculum-aligned, competency-based questions for grades 6–12. Output must follow JSON schema for database mapping.

TASK: For given chapter content, grade, subjectType, and Bloom’s categories, generate questions for each Bloom category using the *exact* counts per question type given in "QuestionTypeDistribution".

RULES:
- Each Bloom level must follow the same question-type counts from QuestionTypeDistribution.
- Do NOT add or remove types randomly.
-Thier language should be same as subjectType (e.g., English questions for English subject).
-Their should be Question on Each Bloom Level No Bloom should be skipped.
-Their should be Question with every Difficulty Level from 1 to 3.
- Give Numerical Questions also when subjectType is "Maths" or "Physics".
- Use Latex when subjectType is "Maths" or "Physics.
- Do NOT label options (❌ no A/B/C/D or 1/2/3/4).
- Options must appear clean, e.g., ["Paris", "London", "Rome", "Berlin"].
- Explanations must be based only on the question (no “chapter”, “text”, or “passage” references).
- Hints must be conceptual (no “refer to text”).
- Output must be VALID JSON only — no markdown, comments, or extra text. Start with '{' and end with '}'.

INPUT:
<INPUT>

OUTPUT JSON (only JSON, no extra text):
{
 "chapterId": <id>,
 "grade": <grade>,
 "subjectType": "<type>",
 "learningObjective": "<text>",
 "questions": [
   {
     "id": <int>,
     "bloomCategory": "<Remember/Understand/...>",
     "difficultyLevel": <1–3>,
     "questionType": "<MCQ/FIB/Short/Desc>",
     "questionText": "<string>",
     "options": ["option1","option2","option3","option4"],
     "answer": "<string>",
     "explanation": "<why correct — question-based only>",
     "hint": "<conceptual approach>",
     "estimatedTimeSec": <int>,
     "mysqlRow": {...}
   }
 ]
}
VALIDATE: JSON syntax must be valid. Each question must include explanation, hint, marks/time, and proper Bloom verb alignment.
"""

# ------------------ Logging ------------------
def log(msg: str):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}\n"
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception:
        pass

def read_logs(limit=4000):
    if not LOG_FILE.exists():
        return ''
    with open(LOG_FILE, 'r', encoding='utf-8') as f:
        data = f.read()
    return data[-limit:]

def normalize_filename(text):
    text = unicodedata.normalize('NFKD', text)
    text = re.sub(r'[^a-zA-Z0-9_\\-\\. ]', '', text).strip()
    return text

# ------------------ File extraction helpers ------------------
def extract_text_from_pdf(pdf_path):
    if pdfplumber is None:
        raise RuntimeError('pdfplumber not installed')
    text = ''
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text += page.extract_text() or ''
        return re.sub(r'\s+', ' ', text).strip()
    except Exception as e:
        log(f'PDF extract error: {e}')
        return ''

def extract_text_from_docx(docx_path):
    if Document is None:
        raise RuntimeError('python-docx not installed')
    try:
        doc = Document(docx_path)
        text = ' '.join([p.text for p in doc.paragraphs])
        return re.sub(r'\s+', ' ', text).strip()
    except Exception as e:
        log(f'DOCX extract error: {e}')
        return ''

# ------------------ API Key rotation & Gemini 2.5 setup ------------------
def get_api_keys_from_env():
    keys = [os.getenv(f'GOOGLE_API_KEY_{i}') for i in range(1, 51) if os.getenv(f'GOOGLE_API_KEY_{i}')]
    return [k for k in keys if k]

API_KEYS = get_api_keys_from_env()
_api_index = 0

def get_next_api_key():
    global _api_index
    if not API_KEYS:
        raise RuntimeError('No GOOGLE_API_KEY_* found in .env')
    key = API_KEYS[_api_index]
    _api_index = (_api_index + 1) % len(API_KEYS)
    return key

def setup_genai_model():
    """
    Returns a configured GenerativeModel instance for gemini-2.5-flash.
    """
    if genai is None:
        raise RuntimeError('google-generativeai package not installed')
    genai.configure(api_key=get_next_api_key())
    # Create a GenerativeModel instance
    model = genai.GenerativeModel(
        model_name="gemini-2.5-flash",
        generation_config={
            "temperature": 0.0,
            "top_p": 1.0,
            "top_k": 1,
            "max_output_tokens": 8192
        }
    )
    return model

def safe_generate_content(model, prompt, retries=3, backoff=2):
    """
    model: instance returned by setup_genai_model()
    Returns text output or None.
    """
    if model is None:
        return None
    for attempt in range(1, retries + 1):
        try:
            response = model.generate_content(prompt)
            # Gemini 2.5 Flash provides .text (preferred)
            if hasattr(response, "text") and response.text and response.text.strip():
                return response.text.strip()
            # Fallback: examine candidates -> parts
            out = ""
            if hasattr(response, "candidates"):
                for cand in response.candidates:
                    for part in getattr(cand.content, "parts", []):
                        if hasattr(part, "text"):
                            out += part.text
                if out.strip():
                    return out.strip()
        except Exception as e:
            log(f"Gemini error attempt {attempt}: {e}")
            # rotate key on quota/rate issues
            if "429" in str(e) or "quota" in str(e).lower():
                try:
                    genai.configure(api_key=get_next_api_key())
                    model = setup_genai_model()
                except Exception as ex:
                    log(f"Key rotation failed: {ex}")
            time.sleep(backoff)
    return None

# ------------------ Robust JSON extraction ------------------
def extract_json_from_text(text):
    if not text or not isinstance(text, str):
        return None
    text_clean = re.sub(r'```(?:json)?', '', text, flags=re.IGNORECASE).strip()
    candidates = []
    stack = []
    start_idx = None
    for i, ch in enumerate(text_clean):
        if ch == '{':
            if start_idx is None:
                start_idx = i
            stack.append('{')
        elif ch == '}':
            if stack:
                stack.pop()
                if not stack and start_idx is not None:
                    candidates.append(text_clean[start_idx:i+1])
                    start_idx = None
    candidates_sorted = sorted(candidates, key=len, reverse=True)
    for c in candidates_sorted:
        try:
            return json.loads(c)
        except Exception:
            continue
    try:
        return json.loads(text_clean)
    except Exception as e:
        log(f'JSON parse error: {e}')
    return None

# ------------------ Answer inference & fill helpers ------------------
def infer_answer_from_explanation(options, explanation):
    if not options or not explanation:
        return None
    explanation_low = explanation.lower()
    for opt in options:
        if not isinstance(opt, str):
            continue
        if opt.strip() and opt.lower() in explanation_low:
            return opt
    choices = [o for o in options if isinstance(o, str) and o.strip()]
    if choices:
        best = None
        best_ratio = 0.0
        for opt in choices:
            ratio = difflib.SequenceMatcher(None, opt.lower(), explanation_low).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best = opt
        if best_ratio > 0.4:
            return best
    return None

def fill_missing_answers(parsed):
    if not parsed or 'questions' not in parsed:
        return parsed
    for q in parsed['questions']:
        if q.get('answer'):
            continue
        qtype = (q.get('questionType') or q.get('question_type') or '').lower()
        opts = q.get('options') or q.get('choices') or []
        expl = q.get('explanation') or ''
        if 'mcq' in qtype:
            inferred = infer_answer_from_explanation(opts, expl)
            if inferred:
                q['answer'] = inferred
            elif opts:
                q['answer'] = opts[0]
                prev = q.get('explanation', '')
                q['explanation'] = (prev + ' (answer auto-filled)').strip()
            else:
                q['answer'] = ''
        else:
            if expl:
                m = re.search(r'["\']([^"\']{2,300})["\']', expl)
                if m:
                    q['answer'] = m.group(1).strip()
                else:
                    first_sentence = expl.strip().split('. ')[0][:200].strip()
                    q['answer'] = first_sentence
            else:
                q['answer'] = ''
    return parsed

def count_missing_answers(parsed):
    if not parsed or 'questions' not in parsed:
        return 0
    cnt = 0
    for q in parsed.get('questions', []):
        if not q.get('answer'):
            cnt += 1
    return cnt

def ask_model_to_fill_answers(model, raw_json_text):
    if model is None:
        return None
    prompt = ("You produced valid JSON but some question 'answer' fields are empty. "
              "For every question fill the 'answer' field correctly: for MCQ use the exact option text; for FIB/Short/Desc provide a concise model answer string. Output only the corrected full JSON.\n\n"
              + raw_json_text)
    filled = safe_generate_content(model, prompt)
    if not filled:
        return None
    return extract_json_from_text(filled)

# ------------------ Generator flow ------------------
def get_bloom_levels_for_grade(grade):
    if grade <= 8:
        return ['Remember', 'Understand', 'Apply']
    elif grade <= 10:
        return ['Remember', 'Understand', 'Apply', 'Analyze']
    else:
        return ['Remember', 'Understand', 'Apply', 'Analyze', 'Evaluate', 'Create']

def generate_questions_for_lo(model, lo, grade, subject_type, blooms, chapter_text, qstart_id=1):
    """
    Returns (questions_list, next_qid)
    """
    question_type_distribution = {'MCQ': 1, 'FIB': 1, 'Short': 1, 'Desc': 1}
    all_questions = []
    qid = qstart_id

    for bloom in blooms:
        for difficulty in [1, 2, 3]:
            input_block = f"""
ChapterId: {lo.get('chapter_id','TEMP')}
Grade: {grade}
SubjectType: {subject_type}
Learning Objective:
LO{lo['id']}: {lo.get('objective_name','')}

BloomCategory: {bloom}
DifficultyLevel: {difficulty}

Chapter Content (reference only):
{chapter_text[:3500]}

QuestionTypeDistribution (for THIS Bloom + Difficulty only):
{json.dumps(question_type_distribution)}
"""
            prompt = MASTER_PROMPT.replace('<INPUT>', input_block)
            raw = safe_generate_content(model, prompt, retries=3) if model else None

            parsed = None
            if raw:
                parsed = extract_json_from_text(raw)

            if not parsed or 'questions' not in parsed:
                # fallback placeholders
                placeholders = {'questions': []}
                for qtype, cnt in question_type_distribution.items():
                    for _ in range(cnt):
                        placeholders['questions'].append({
                            'questionType': qtype,
                            'questionText': f'{qtype} placeholder for LO {lo["id"]} ({bloom} D{difficulty})',
                            'options': ["Option 1", "Option 2", "Option 3", "Option 4"] if qtype == 'MCQ' else [],
                            'answer': ("Option 1" if qtype == 'MCQ' else "Model answer"),
                            'explanation': 'Auto-generated placeholder explanation.',
                            'hint': 'Auto-generated hint.',
                            'estimatedTimeSec': 60
                        })
                parsed = placeholders

            parsed = fill_missing_answers(parsed)
            if count_missing_answers(parsed) > 0 and model is not None:
                try:
                    raw_parsed_text = json.dumps(parsed, ensure_ascii=False)
                    filled = ask_model_to_fill_answers(model, raw_parsed_text)
                    if filled and 'questions' in filled:
                        parsed = fill_missing_answers(filled)
                except Exception as e:
                    log(f'ask_model_to_fill_answers error: {e}')

            missing_final = count_missing_answers(parsed)
            if missing_final:
                log(f'LO {lo["id"]} {bloom} D{difficulty} - missing answers after fill: {missing_final}')

            for q in parsed.get('questions', []):
                q_record = {
                    'id': qid,
                    'bloomCategory': bloom,
                    'difficultyLevel': difficulty,
                    'questionType': q.get('questionType', 'MCQ'),
                    'questionText': q.get('questionText', ''),
                    'options': q.get('options', []),
                    'answer': q.get('answer', ''),
                    'explanation': q.get('explanation', ''),
                    'hint': q.get('hint', ''),
                    'estimatedTimeSec': q.get('estimatedTimeSec', 45),
                    'mysqlRow': {
                        'chapter_id': int(lo.get('chapter_id') or 0),
                        'learning_objective_id': lo.get('id'),
                        'bloom_category': bloom,
                        'difficulty_level': difficulty,
                        'question_type': q.get('questionType', 'MCQ')
                    }
                }
                all_questions.append(q_record)
                qid += 1

    return all_questions, qid

# ------------------ DB helpers ------------------
def get_db_conn():
    cfg = {
        'host': os.getenv('DB_HOST'),
        'user': os.getenv('DB_USER'),
        'password': os.getenv('DB_PASSWORD'),
        'database': os.getenv('DB_NAME')
    }
    if not cfg['host'] or mysql is None:
        return None
    try:
        return mysql.connector.connect(**cfg)
    except Exception as e:
        log(f'DB connect failed: {e}')
        return None

def insert_jsons_to_db(folder):
    conn = get_db_conn()
    if not conn:
        raise RuntimeError('DB not configured or mysql-connector not installed')
    cur = conn.cursor()
    questions_table = os.getenv('QUESTIONS_TABLE', 'assessment_questions')
    lo_table = os.getenv('LOS_TABLE', 'learning_objectives')

    for file in os.listdir(folder):
        if not file.endswith('.json'):
            continue
        path = os.path.join(folder, file)
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        chapterId = data.get('chapterId')
        learning_objectives = data.get('learningObjectives', [])
        cur.execute(f"SELECT MAX(id) FROM {lo_table}")
        max_lo = cur.fetchone()[0] or 0
        next_lo = max_lo + 1
        for lo in learning_objectives:
            cur.execute(f"INSERT INTO {lo_table} (id, objective_name, grade_subject_id, chapter_id) VALUES (%s,%s,%s,%s)",
                        (next_lo, lo.get('objective'), None, chapterId))
            lo['loId'] = next_lo
            next_lo += 1
        cur.execute(f"SELECT MAX(id) FROM {questions_table}")
        max_q = cur.fetchone()[0] or 0
        next_q = max_q + 1
        for lo in learning_objectives:
            for q in lo.get('questions', []):
                opts = q.get('options', [])
                opt1 = opts[0] if len(opts) > 0 else None
                opt2 = opts[1] if len(opts) > 1 else None
                opt3 = opts[2] if len(opts) > 2 else None
                opt4 = opts[3] if len(opts) > 3 else None
                answer = q.get('answer', '')
                answer_no = opts.index(answer) + 1 if answer in opts else None
                time_raw = q.get('estimatedTimeSec', 30)
                try:
                    sec = int(time_raw)
                except:
                    sec = 30
                cur.execute(f"INSERT INTO {questions_table} (id, assessment_parameter_id, chapter_learning_objective_id, difficulty_level_id, question_type_id, question_text, option1, option2, option3, option4, answer_text, answer_no, explanation, marks, required_time_to_give_answer, hint, chapter_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (next_q, 1, lo.get('loId'), q.get('difficultyLevel', 1), 1, q.get('questionText', ''), opt1, opt2, opt3, opt4, answer, answer_no, q.get('explanation', ''), 1, sec, q.get('hint', ''), chapterId))
                next_q += 1
    conn.commit()
    cur.close()
    conn.close()
    return True

# ------------------ Streamlit UI ------------------
st.sidebar.title('Generator — Dark Dashboard')
mode = st.sidebar.radio('Mode', ['Generate', 'Insert to DB', 'View Outputs', 'Logs'])

if mode == 'Generate':
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.header('Generate Questions')
    col1, col2 = st.columns([2, 1])
    with col1:
        chapter_id = st.text_input('Chapter ID', value='30')
        grade = st.number_input('Grade', min_value=6, max_value=12, value=9)
        subject = st.text_input('Subject Type', value='English')
        uploaded = st.file_uploader('Upload chapter (pdf/docx)', type=['pdf', 'docx'])
        existing_files = [f for f in os.listdir(DOCS_DIR) if f.lower().endswith(('.pdf', '.docx'))]
        gen_btn = st.button('Generate Questions', key='gen')
    with col2:
        st.write('Settings')
        st.write(f'API keys configured: {len(API_KEYS)}')
        st.write(f'Output folder: {OUTPUT_DIR}')
        st.write('')

    if gen_btn:
        selected_path = None
        if uploaded is not None:
            name = normalize_filename(uploaded.name)
            dest = UPLOAD_DIR / name
            with open(dest, 'wb') as out:
                out.write(uploaded.getbuffer())
            selected_path = dest
            st.success(f'Uploaded to {dest}')
            log(f'Uploaded file {dest}')

        try:
            model = None
            if API_KEYS and genai is not None:
                try:
                    model = setup_genai_model()
                except Exception as e:
                    log(f'GenAI setup failed: {e}')
                    model = None
        except Exception as e:
            st.error(f'GenAI init failed: {e}')
            log(f'GenAI init failed: {e}')
            model = None

        try:
            # extract chapter text
            if selected_path:
                if selected_path.suffix.lower() == '.pdf':
                    chapter_text = extract_text_from_pdf(selected_path)
                else:
                    chapter_text = extract_text_from_docx(selected_path)
            else:
                detected = None
                for f in existing_files:
                    if str(chapter_id) in f:
                        detected = DOCS_DIR / f
                        break
                if detected:
                    if detected.suffix.lower() == '.pdf':
                        chapter_text = extract_text_from_pdf(detected)
                    else:
                        chapter_text = extract_text_from_docx(detected)
                    selected_path = detected
                else:
                    st.error('No document found. Upload a file or put it in the docs folder.')
                    st.stop()

            # fetch LOs from DB if available, else create placeholder
            los = []
            try:
                conn = get_db_conn()
                if conn:
                    cur = conn.cursor(dictionary=True)
                    cur.execute('SELECT id, objective_name, chapter_id FROM learning_objectives WHERE chapter_id = %s', (chapter_id,))
                    rows = cur.fetchall()
                    cur.close(); conn.close()
                    if rows:
                        los = rows
            except Exception as e:
                log(f'LO fetch failed: {e}')

            if not los:
                los = [{'id': 190, 'objective_name': f'LO1: Understand {selected_path.stem}', 'chapter_id': int(chapter_id)}]

            blooms = get_bloom_levels_for_grade(int(grade))

            all_lo_outputs = []
            progress_bar = st.progress(0)
            total_steps = len(los) * len(blooms) * 3
            step = 0
            qid_start = 1

            for lo in los:
                if 'chapter_id' not in lo:
                    lo['chapter_id'] = int(chapter_id)
                questions, qid_start = generate_questions_for_lo(model, lo, int(grade), subject, blooms, chapter_text, qstart_id=qid_start)
                lo_out = {
                    'loId': lo['id'],
                    'objective': lo.get('objective_name', ''),
                    'bloomLevels': blooms,
                    'totalQuestions': len(questions),
                    'questions': questions
                }
                all_lo_outputs.append(lo_out)
                step += len(blooms) * 3
                progress_bar.progress(min(1.0, step / max(1, total_steps)))

            result = {
                'chapterId': int(chapter_id),
                'grade': int(grade),
                'subjectType': subject,
                'chapterName': selected_path.stem if selected_path else '',
                'learningObjectives': all_lo_outputs
            }

            out_file = OUTPUT_DIR / f'chapter_{chapter_id}_streamlit_{int(time.time())}.json'
            with open(out_file, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)

            st.success(f'Generated: {out_file}')
            st.write('Preview (first LO):')
            if all_lo_outputs:
                st.json(all_lo_outputs[0])
            log(f'Generated file {out_file}')
        except Exception as e:
            st.error(f'Generation failed: {e}')
            log(f'Generation failed: {e}')

elif mode == 'Insert to DB':
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.header('Insert JSON files into MySQL')
    folder = st.text_input('JSON folder', value=str(OUTPUT_DIR))
    insert_btn = st.button('Insert JSONs')
    if insert_btn:
        try:
            inserted = insert_jsons_to_db(folder)
            st.success('Inserted into DB')
            log(f'Inserted JSONs from {folder} into DB')
        except Exception as e:
            st.error(f'Insert failed: {e}')
            log(f'Insert failed: {e}')

elif mode == 'View Outputs':
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.header('Generated Outputs')
    files = [f for f in os.listdir(OUTPUT_DIR) if f.endswith('.json')]
    for f in sorted(files, reverse=True):
        st.write(f)
        if st.button(f'Download {f}', key=f):
            with open(OUTPUT_DIR / f, 'rb') as fh:
                st.download_button(label=f'Download {f}', data=fh, file_name=f)

elif mode == 'Logs':
    st.header('Logs')
    logs = read_logs()
    safe_logs = logs.replace("\n", "<br>")
    st.markdown(f'<div class="logs">{safe_logs}</div>', unsafe_allow_html=True)
    if st.button('Clear logs'):
        open(LOG_FILE, 'w').close()
        st.experimental_rerun()
