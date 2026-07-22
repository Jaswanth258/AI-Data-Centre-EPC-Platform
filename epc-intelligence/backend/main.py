import os
import shutil
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .doc_parser import build_clause_trees, rebuild_clause_trees, list_submittals, list_specs
from .rfi_agent import answer_rfi
from .compliance_agent import verify_submittal

# Define project directories relative to this file
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BACKEND_DIR)
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
FRONTEND_DIR = os.path.join(PROJECT_ROOT, "frontend")

# Ensure data directories exist
os.makedirs(os.path.join(DATA_DIR, "specs"), exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, "submittals"), exist_ok=True)

app = FastAPI(title="AegisEPC Project Intelligence Platform API")

# Enable CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Startup event: Build clause trees from spec files
@app.on_event("startup")
async def startup_event():
    print("=" * 52)
    print("  AegisEPC -- Clause-Tree Ingestion Starting...   ")
    print("=" * 52)
    build_clause_trees(DATA_DIR)
    print("AegisEPC ready. No vector DB, no embeddings -- pure clause-tree retrieval.\n")

# Request models
class ChatMessage(BaseModel):
    role: str
    content: str

class QuestionRequest(BaseModel):
    question: str
    history: list[ChatMessage] = []
    scope: str = "all"

class ComplianceRequest(BaseModel):
    submittal_file: str


# ---------------------------------------------------------------------------
# Helpers: extract text from uploaded files
# ---------------------------------------------------------------------------

ALLOWED_EXTENSIONS = {'.md', '.txt', '.pdf'}

def _extract_text_from_pdf(filepath: str) -> str:
    """Extract text from a PDF using pdfplumber. Returns plain text."""
    try:
        import pdfplumber
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="PDF support requires pdfplumber. Install it: pip install pdfplumber"
        )
    text_parts = []
    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    return "\n\n".join(text_parts)


def _save_upload(upload_file: UploadFile, dest_dir: str) -> dict:
    """
    Save an uploaded file to dest_dir.
    If PDF, extract text and save as .md alongside the original.
    Returns {filename, clauses_or_none, message}.
    """
    original_name = upload_file.filename or "uploaded_file"
    _, ext = os.path.splitext(original_name)
    ext = ext.lower()

    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext}. Accepted: {', '.join(ALLOWED_EXTENSIONS)}"
        )

    # Save the raw uploaded file
    dest_path = os.path.join(dest_dir, original_name)
    with open(dest_path, "wb") as f:
        shutil.copyfileobj(upload_file.file, f)

    saved_name = original_name

    # If PDF, extract text and save as .md for the parser
    if ext == '.pdf':
        extracted_text = _extract_text_from_pdf(dest_path)
        if not extracted_text.strip():
            raise HTTPException(
                status_code=400,
                detail="Could not extract any text from the PDF. It may be scanned/image-based."
            )
        md_name = original_name.rsplit('.', 1)[0] + '.md'
        md_path = os.path.join(dest_dir, md_name)
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(extracted_text)
        saved_name = md_name
        print(f"  PDF -> extracted text saved as {md_name}")

    return {"filename": saved_name, "original": original_name}


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/submittals")
def get_submittals():
    try:
        files = list_submittals(DATA_DIR)
        return {"submittals": files}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/specs")
def get_specs():
    try:
        files = list_specs(DATA_DIR)
        return {"specs": files}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/ask")
def ask_question(req: QuestionRequest):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty.")
    try:
        # Convert Pydantic message list to list of dicts for the agent
        history_list = [{"role": msg.role, "content": msg.content} for msg in req.history]
        result = answer_rfi(req.question, history_list, scope=req.scope)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/check-compliance")
def check_compliance(req: ComplianceRequest):
    if not req.submittal_file.strip():
        raise HTTPException(status_code=400, detail="Submittal file name cannot be empty.")
    try:
        report = verify_submittal(req.submittal_file, DATA_DIR)
        return {"report": report}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/upload/spec")
async def upload_spec(file: UploadFile = File(...)):
    """Upload a specification document (.md, .txt, or .pdf).
    Replaces existing spec files and rebuilds the clause tree."""
    specs_dir = os.path.join(DATA_DIR, "specs")
    
    # Delete existing spec files so new upload replaces the active ground-truth
    if os.path.exists(specs_dir):
        for existing_file in os.listdir(specs_dir):
            if existing_file.endswith(('.md', '.txt', '.pdf')):
                try:
                    os.remove(os.path.join(specs_dir, existing_file))
                    print(f"  Removed old spec file: {existing_file}")
                except Exception as ex:
                    print(f"  Could not remove {existing_file}: {ex}")

    result = _save_upload(file, specs_dir)

    # Re-index specs so the new file is immediately available
    rebuild_clause_trees(DATA_DIR)

    return {
        "message": f"Specification '{result['original']}' uploaded and indexed successfully.",
        "filename": result["filename"],
        "specs": list_specs(DATA_DIR),
    }

@app.post("/api/upload/submittal")
async def upload_submittal(file: UploadFile = File(...)):
    """Upload a vendor submittal document (.md, .txt, or .pdf)."""
    submittals_dir = os.path.join(DATA_DIR, "submittals")
    result = _save_upload(file, submittals_dir)

    return {
        "message": f"Submittal '{result['original']}' uploaded successfully.",
        "filename": result["filename"],
        "submittals": list_submittals(DATA_DIR),
    }

# Route to serve the frontend single-page application
@app.get("/")
def serve_index():
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=404, detail="index.html not found in frontend directory.")
    return FileResponse(index_path)
