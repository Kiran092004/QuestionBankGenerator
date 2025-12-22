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

st.session_state.setdefault("is_generating", False)
st.session_state.setdefault("generation_done", False)
st.session_state.setdefault("generated_files", [])
st.session_state.setdefault("status_text", "Idle")
st.session_state.setdefault("progress_value", 0.0)



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
body {
    background: radial-gradient(circle at top, #0f172a, #020617);
    color: #e5e7eb;
}

.sidebar .sidebar-content {
    background: linear-gradient(180deg, #020617, #020617);
}

.card {
    background: linear-gradient(180deg, #020617, #020617);
    border: 1px solid rgba(148,163,184,0.15);
    border-radius: 18px;
    padding: 22px;
    box-shadow: 0 20px 40px rgba(0,0,0,0.55);
}

h1, h2, h3 {
    color: #e0f2fe;
}

label {
    color: #cbd5f5 !important;
    font-weight: 500;
}

.stButton button {
    background: linear-gradient(90deg,#2563eb,#38bdf8);
    border-radius: 14px;
    font-weight: 600;
    padding: 10px 22px;
    border: none;
    box-shadow: 0 8px 20px rgba(37,99,235,0.4);
}

.stSelectbox > div,
.stTextInput > div,
.stNumberInput > div {
    background: #020617;
    border-radius: 12px;
    border: 1px solid rgba(148,163,184,0.2);
}

.logs {
    background:#020617;
    border:1px solid rgba(148,163,184,0.15);
    border-radius:14px;
    padding:14px;
    font-family: monospace;
}
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

def extract_chapter_title(text):
    if not text:
        return None

    lines = [l.strip() for l in text.split("\n") if len(l.strip()) > 5]

    patterns = [
        r'chapter\s*\d+\s*[:\-]?\s*(.+)',
        r'^\d+\.\s*(.+)',
    ]

    for line in lines[:15]:
        for p in patterns:
            m = re.search(p, line, re.IGNORECASE)
            if m:
                return m.group(1).strip()

    return lines[0][:120] if lines else None


def fetch_subject_chapters(grade_subject_id):
    conn = get_db_conn()
    if not conn:
        return []

    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT id, chapter_name, chapter_no
        FROM subject_chapters
        WHERE grade_subject_id = %s
    """, (grade_subject_id,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


def detect_chapter_from_db(chapter_title, filename, grade_subject_id, threshold=0.65):
    chapters = fetch_subject_chapters(grade_subject_id)
    if not chapters:
        return None

    title = (chapter_title or "").lower()
    fname = filename.lower()
    best = None
    best_score = 0

    for ch in chapters:
        db_name = ch["chapter_name"].lower()

        if title and (title in db_name or db_name in title):
            return ch

        if db_name in fname:
            return ch

        score = difflib.SequenceMatcher(None, title, db_name).ratio()
        if score > best_score:
            best_score = score
            best = ch

    if best_score >= threshold:
        return best

    return None

def fetch_grades_with_names(learning_medium_id, board_id):
    conn = get_db_conn()
    if not conn:
        return []

    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT id, grade_name
        FROM grades
        WHERE id IN (
            SELECT DISTINCT grade_id
            FROM grade_subjects
            WHERE learning_medium_id = %s
              AND board_id = %s
        )
        ORDER BY id
    """, (learning_medium_id, board_id))

    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

# ------------------ API Key rotation & Gemini 2.5 setup ------------------
def get_api_keys_from_env():
    keys = [os.getenv(f'GOOGLE_API_KEY_{i}') for i in range(1, 51) if os.getenv(f'GOOGLE_API_KEY_{i}')]
    return [k for k in keys if k]

API_KEYS = get_api_keys_from_env()
_api_index = 0
CURRENT_API_KEY = None


def get_next_api_key():
    global _api_index
    if not API_KEYS:
        raise RuntimeError('No GOOGLE_API_KEY_* found in .env')
    key = API_KEYS[_api_index]
    _api_index = (_api_index + 1) % len(API_KEYS)
    return key

def setup_genai_model():
    global CURRENT_API_KEY

    if genai is None:
        raise RuntimeError('google-generativeai package not installed')

    if CURRENT_API_KEY is None:
        CURRENT_API_KEY = get_next_api_key()

    genai.configure(api_key=CURRENT_API_KEY)

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


def safe_generate_content(
    model,
    prompt,
    retries_per_key=3,
    max_key_rotations=2,
    base_delay=2
):
    global CURRENT_API_KEY

    if model is None:
        return None

    for rotation in range(max_key_rotations + 1):

        for attempt in range(1, retries_per_key + 1):
            try:
                response = model.generate_content(prompt)

                if hasattr(response, "text") and response.text:
                    return response.text.strip()

                out = ""
                for cand in getattr(response, "candidates", []):
                    for part in getattr(cand.content, "parts", []):
                        if hasattr(part, "text"):
                            out += part.text

                if out.strip():
                    return out.strip()

            except Exception as e:
                log(f"Gemini error (attempt {attempt}): {e}")
                time.sleep(base_delay * attempt)

        # 🔄 rotate key ONLY after retries
        try:
            CURRENT_API_KEY = get_next_api_key()
            genai.configure(api_key=CURRENT_API_KEY)
            model = setup_genai_model()
            log("🔄 API key rotated")
        except Exception as e:
            log(f"❌ Key rotation failed: {e}")
            break

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
    if grade <= 11:
        return ['Remember', 'Understand', 'Apply']
    elif grade <= 13:
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
            raw = safe_generate_content(
                model,
                prompt,
                retries_per_key=2,
                max_key_rotations=1
            )

            time.sleep(1.2)  # ⭐ cooldown

            parsed = None
            if raw:
                parsed = extract_json_from_text(raw)
            if not parsed or 'questions' not in parsed:
                log(f"❌ Skipping LO {lo['id']} | {bloom} | D{difficulty} (Gemini failed)")
                continue


            parsed = fill_missing_answers(parsed)

            if count_missing_answers(parsed) > 0:
                raw_fix = ask_model_to_fill_answers(model, json.dumps(parsed))
                if raw_fix and 'questions' in raw_fix:
                    parsed = fill_missing_answers(raw_fix)

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


def fetch_subject_chapters(grade_subject_id):
    conn = get_db_conn()
    if not conn:
        return []

    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT id, chapter_name, chapter_no
        FROM subject_chapters
        WHERE grade_subject_id = %s
    """, (grade_subject_id,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows

def extract_chapter_title(text):
    """
    Try to extract chapter title from first page text
    """
    if not text:
        return None

    lines = [l.strip() for l in text.split("\n") if len(l.strip()) > 5]

    patterns = [
        r'chapter\s*\d+\s*[:\-]?\s*(.+)',
        r'^\d+\.\s*(.+)',
    ]

    for line in lines[:15]:  # only first page lines
        low = line.lower()
        for p in patterns:
            m = re.search(p, low, re.IGNORECASE)
            if m:
                return m.group(1).strip().title()

    # fallback: first big heading
    return lines[0][:120] if lines else None

def detect_chapter_from_db(
    chapter_title,
    filename,
    grade_subject_id,
    threshold=0.65
):
    """
    Match chapter title with DB chapter_name
    """
    chapters = fetch_subject_chapters(grade_subject_id)

    if not chapters:
        return None

    candidates = []
    title = (chapter_title or "").lower()
    fname = filename.lower()

    for ch in chapters:
        db_name = ch["chapter_name"].lower()

        # 1️⃣ Direct substring match
        if title and (title in db_name or db_name in title):
            return ch

        # 2️⃣ Filename match
        if db_name in fname:
            return ch

        # 3️⃣ Fuzzy score
        score = difflib.SequenceMatcher(None, title, db_name).ratio()
        candidates.append((score, ch))

    # 4️⃣ Best fuzzy match
    candidates.sort(reverse=True, key=lambda x: x[0])
    if candidates and candidates[0][0] >= threshold:
        return candidates[0][1]

    return None

# ------------------ Dropdown Data Fetch ------------------

def fetch_learning_mediums():
    conn = get_db_conn()
    if not conn:
        return []
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT id, learning_medium_language FROM learning_medium")
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


def fetch_boards():
    conn = get_db_conn()
    if not conn:
        return []
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT id, board_name FROM education_boards")
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


def fetch_grades(learning_medium_id, board_id):
    conn = get_db_conn()
    if not conn:
        return []
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT grade_id 
        FROM grade_subjects
        WHERE learning_medium_id = %s AND board_id = %s
        ORDER BY grade_id
    """, (learning_medium_id, board_id))
    grades = [r[0] for r in cur.fetchall()]
    cur.close(); conn.close()
    return grades


def fetch_subjects(learning_medium_id, board_id, grade_id):
    conn = get_db_conn()
    if not conn:
        return []
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT id, subject_name
        FROM grade_subjects
        WHERE learning_medium_id = %s
          AND board_id = %s
          AND grade_id = %s
        ORDER BY subject_name
    """, (learning_medium_id, board_id, grade_id))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows

# ------------------ Streamlit UI ------------------
st.sidebar.title('Generator — Dark Dashboard')
mode = st.sidebar.radio('Mode', ['Generate', 'Insert to DB', 'View Outputs', 'Logs'])

if mode == 'Generate':
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.header('Generate Questions')
    col1, col2 = st.columns([2, 1])
    with col1:
        st.markdown("### 📘 Academic Configuration")

# Fetch base data
        learning_mediums = fetch_learning_mediums()
        boards = fetch_boards()

        lm_map = {lm['learning_medium_language']: lm['id'] for lm in learning_mediums}
        board_map = {b['board_name']: b['id'] for b in boards}

        colA, colB = st.columns(2)

        with colA:
            learning_medium = st.selectbox(
                "Learning Medium",
                options=list(lm_map.keys())
            )

            board = st.selectbox(
                "Education Board",
                options=list(board_map.keys())
            )

        with colB:
            grade_rows = fetch_grades_with_names(
                lm_map[learning_medium],
                board_map[board]
            )

        if not grade_rows:
            st.warning("No grades found for selected board and medium.")
            st.stop()

        grade_map = {g["grade_name"]: g["id"] for g in grade_rows}

        selected_grade_name = st.selectbox(
            "Grade",
            options=list(grade_map.keys()),
            index=0
            )

        grade = grade_map[selected_grade_name]  # ✅ SAFE NOW


        subjects = fetch_subjects(
                    lm_map[learning_medium],
                    board_map[board],
                    grade
                )

        subject_map = {s['subject_name']: s['id'] for s in subjects}

        subject = st.selectbox(
                "Subject",
                options=list(subject_map.keys())
            )

        st.markdown("---")


        uploaded_files = st.file_uploader(
    "Upload chapter files (PDF/DOCX)",
    type=["pdf", "docx"],
    accept_multiple_files=True
)

        existing_files = [f for f in os.listdir(DOCS_DIR) if f.lower().endswith(('.pdf', '.docx'))]
        gen_btn = st.button('Generate Questions', key='gen')
    with col2:
        st.write('Settings')
        st.write(f'API keys configured: {len(API_KEYS)}')
        st.write(f'Output folder: {OUTPUT_DIR}')
        st.write('')
        status_box = st.empty()
        progress_bar = st.progress(0)
        percent_box = st.empty()


    if gen_btn:
        st.session_state.is_generating = True
        st.session_state.generation_done = False
        st.session_state.generated_files = []


        if not uploaded_files:
            st.error("Please upload at least one chapter file")
            st.stop()

        model = setup_genai_model()

        total_files = len(uploaded_files)
        completed_files = 0

        st.session_state.status_text = "Starting generation..."
        st.session_state.progress_value = 0.0

        status_box.info(st.session_state.status_text)
        progress_bar.progress(0)
        percent_box.write("0% completed")

        for uploaded in uploaded_files:

            st.session_state.status_text = f"Processing file: {uploaded.name}"
            status_box.info(st.session_state.status_text)


            name = normalize_filename(uploaded.name)
            dest = UPLOAD_DIR / name

            with open(dest, "wb") as f:
                f.write(uploaded.getbuffer())

            # Extract text
            if dest.suffix.lower() == ".pdf":
                chapter_text = extract_text_from_pdf(dest)
            else:
                chapter_text = extract_text_from_docx(dest)
        
            # --------- CHAPTER DETECTION (CORRECT) ---------

# Extract chapter title from text
            chapter_title = extract_chapter_title(chapter_text)

            # Detect chapter using DB (THIS IS THE ONLY FUNCTION CALL)
            matched_chapter = detect_chapter_from_db(
                chapter_title=chapter_title,
                filename=dest.name,
                grade_subject_id=subject_map[subject]
            )

            if not matched_chapter:
                st.warning(f"⚠ Could not detect chapter for {dest.name}, skipping")
                log(f"Chapter detection failed for {dest.name}")
                continue

            # ✅ FINAL chapter ID (JUST ASSIGN)
            chapter_id = matched_chapter["id"]

            st.success(
                f"📘 {dest.name} → {matched_chapter['chapter_name']} (ID {chapter_id})"
            )

            # --------- FETCH LEARNING OBJECTIVES ---------

            los = []
            conn = get_db_conn()
            if conn:
                cur = conn.cursor(dictionary=True)
                cur.execute(
                    "SELECT id, objective_name, chapter_id FROM learning_objectives WHERE chapter_id=%s",
                    (chapter_id,)
                )
                los = cur.fetchall()
                cur.close()
                conn.close()

            # Fallback LO if none found
            if not los:
                los = [{
                    "id": 1,
                    "objective_name": f"Understand {matched_chapter['chapter_name']}",
                    "chapter_id": chapter_id
                }]

            # --------- QUESTION GENERATION ---------

            blooms = get_bloom_levels_for_grade(int(grade))
            qid_start = 1
            all_lo_outputs = []
            total_los = len(los)
            completed_los = 0


            for lo in los:
                questions, qid_start = generate_questions_for_lo(
                    model,
                    lo,
                    int(grade),
                    subject,
                    blooms,
                    chapter_text,
                    qstart_id=qid_start
                )

                all_lo_outputs.append({
                    "loId": lo["id"],
                    "objective": lo["objective_name"],
                    "questions": questions
                })

                completed_los += 1

                file_progress = completed_los / total_los
                overall_progress = (
                    (completed_files + file_progress) / total_files
                )

                st.session_state.progress_value = overall_progress
                progress_bar.progress(min(overall_progress, 1.0))
                percent_box.write(f"{int(overall_progress * 100)}% completed")

                st.session_state.status_text = (
                    f"Generating questions → "
                    f"File {completed_files + 1}/{total_files}, "
                    f"LO {completed_los}/{total_los}"
                )
                status_box.info(st.session_state.status_text)


            # --------- SAVE OUTPUT ---------

            result = {
                "chapterId": chapter_id,
                "grade": int(grade),
                "subjectType": subject,
                "chapterName": matched_chapter["chapter_name"],
                "learningObjectives": all_lo_outputs
            }

            out_file = OUTPUT_DIR / f"chapter_{chapter_id}_{int(time.time())}.json"

            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)

            st.success(f"✅ Generated: {out_file.name}")
            completed_files += 1
            st.session_state.generated_files.append(str(out_file))
            log(f"Generated {out_file}")

        st.session_state.is_generating = False
        st.session_state.generation_done = True

        st.session_state.status_text = "✅ Generation completed successfully"
        st.session_state.progress_value = 1.0

        progress_bar.progress(1.0)
        percent_box.write("100% completed")
        status_box.success(st.session_state.status_text)
    


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

    files = (
        st.session_state.generated_files
        if st.session_state.generated_files
        else [
            str(OUTPUT_DIR / f)
            for f in os.listdir(OUTPUT_DIR)
            if f.endswith(".json")
        ]
    )

    if not files:
        st.info("No generated JSON files found.")
    else:
        for path in sorted(files, reverse=True):
            fname = os.path.basename(path)
            st.write(fname)

            with open(path, "rb") as fh:
                st.download_button(
                    label=f"Download {fname}",
                    data=fh,
                    file_name=fname,
                    key=fname
                )


elif mode == 'Logs':
    st.header('Logs')
    logs = read_logs()
    safe_logs = logs.replace("\n", "<br>")
    st.markdown(f'<div class="logs">{safe_logs}</div>', unsafe_allow_html=True)
    if st.button('Clear logs'):
        open(LOG_FILE, 'w').close()
        st.experimental_rerun()
