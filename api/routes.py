"""
api/routes.py
-------------
FastAPI router for PharmaScanKE.

Endpoints:
  POST   /api/upload         – upload a study resource file
  GET    /api/notes          – list / filter resources
  GET    /api/notes/stats    – aggregate statistics
  DELETE /api/notes/{id}     – delete a resource
  POST   /api/analyze        – AI pharmacy analysis (text or file)
"""

import os
import re
import unicodedata
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse
from groq import AsyncGroq
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import get_db
from models.resource import Resource
from schemas.analysis import AnalysisRequest, AnalysisResponse, Pharmacy180Ref
from schemas.resource import (
    MessageResponse,
    ResourceListResponse,
    ResourceOut,
    ResourceStats,
    SemesterCount,
    SubjectCount,
)

router = APIRouter(prefix="/api", tags=["pharmascan"])

# ── Groq client (lazy) ────────────────────────────────────────────────────────
_groq: Optional[AsyncGroq] = None


def get_groq() -> AsyncGroq:
    global _groq
    if _groq is None:
        if not settings.GROQ_API_KEY:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="GROQ_API_KEY environment variable is not set.",
            )
        _groq = AsyncGroq(api_key=settings.GROQ_API_KEY)
    return _groq


# ── CDACC System Prompt ───────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are an expert CDACC-certified D.Pharm pharmacy analysis assistant for Kenyan pharmacy students.
You provide detailed, accurate, and educational pharmaceutical analysis aligned with the CDACC D.Pharm curriculum.

MANDATORY FORMATTING RULES:
1. When analyzing any pharmacy document, chemical entity, or drug concept, you MUST dynamically include a
   relevant structural image using this EXACT Markdown pattern for each specific chemical entity mentioned:
   ![Drug Name Structure](https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/DRUGNAME/PNG)
   where DRUGNAME is the drug name in lowercase, URL-safe (spaces as %20).
   Example: ![Amoxicillin Structure](https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/amoxicillin/PNG)
   Example: ![Ibuprofen Structure](https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/ibuprofen/PNG)

2. Use clear Markdown headings (##, ###) to structure your response.
3. Use tables (Markdown format) for drug comparisons, mechanism summaries, ADME parameters, or property lists.
4. Use bullet points for properties, side effects, indications, and contraindications.
5. For drug classes (e.g., beta-lactams), show the structure for the most representative member.
6. For pharmacokinetics, always present ADME parameters in a structured table.
7. Keep explanations at D.Pharm level — precise, clinically relevant, accessible to diploma students.
8. Reference CDACC syllabus topics where applicable.
9. End every response with a ## Key Takeaways section with 3–5 bullet clinical points.

Do not use emojis in the main analysis body.
"""

# ── Pharmacy180.com Concept Map ───────────────────────────────────────────────
PHARMACY180_MAP: dict[str, str] = {
    "beta lactam": "Beta-lactam antibiotics inhibit bacterial cell wall synthesis by covalently binding to penicillin-binding proteins (PBPs), preventing peptidoglycan cross-linking and causing cell lysis. Includes penicillins, cephalosporins, carbapenems, and monobactams.",
    "beta-lactam": "Beta-lactam antibiotics inhibit bacterial cell wall synthesis by binding to PBPs, preventing peptidoglycan cross-linking. Classes: penicillins, cephalosporins, carbapenems, monobactams.",
    "nsaid": "NSAIDs (Non-Steroidal Anti-Inflammatory Drugs) inhibit COX-1 and COX-2 enzymes, reducing prostaglandin and thromboxane synthesis. Used for analgesia, antipyresis, and anti-inflammation. Examples: ibuprofen, naproxen, diclofenac, aspirin.",
    "nsaids": "NSAIDs inhibit cyclooxygenase (COX) enzymes, reducing prostaglandin synthesis. Used for pain, fever, and inflammation management.",
    "alkaloid": "Alkaloids are nitrogen-containing organic compounds derived primarily from plants, with diverse pharmacological activity. Examples: morphine (opioid analgesic), quinine (antimalarial), caffeine (CNS stimulant), atropine (anticholinergic).",
    "alkaloids": "Plant-derived nitrogen-containing compounds with broad pharmacological activity including analgesic, antimalarial, and CNS effects.",
    "pharmacokinetics": "Pharmacokinetics (PK) describes how the body handles drugs — Absorption, Distribution, Metabolism, and Excretion (ADME). Key PK parameters: bioavailability (F), volume of distribution (Vd), half-life (t½), and clearance (CL).",
    "pharmacodynamics": "Pharmacodynamics (PD) describes how drugs affect the body — mechanisms of action, receptor binding, dose-response relationships, and therapeutic/toxic effects.",
    "antibiotic": "Antibiotics are antimicrobial agents that inhibit or kill bacteria. Classified by mechanism: cell wall inhibitors (β-lactams, glycopeptides), protein synthesis inhibitors (aminoglycosides, macrolides, tetracyclines), DNA gyrase inhibitors (fluoroquinolones), and cell membrane disruptors (polymyxins).",
    "antibiotics": "Antimicrobial agents classified by mechanism of action: cell wall synthesis inhibition, protein synthesis inhibition, DNA/RNA synthesis inhibition, or cell membrane disruption.",
    "antihypertensive": "Antihypertensive agents lower systemic blood pressure. Major drug classes: ACE inhibitors (captopril), ARBs (losartan), calcium channel blockers (amlodipine), beta-blockers (metoprolol), and diuretics (hydrochlorothiazide).",
    "antihypertensives": "Blood pressure-lowering agents acting on RAAS, sympathetic nervous system, or vascular smooth muscle.",
    "opioid": "Opioids bind to μ (mu), κ (kappa), and δ (delta) opioid receptors in the CNS and periphery, producing analgesia, euphoria, and respiratory depression. Examples: morphine, codeine, pethidine, tramadol, fentanyl.",
    "opioids": "Opioid receptor agonists producing analgesia and CNS depression. Risk of tolerance, dependence, and respiratory depression.",
    "corticosteroid": "Corticosteroids act on glucocorticoid/mineralocorticoid receptors, modulating gene expression to reduce inflammation and suppress immune responses. Examples: prednisolone, dexamethasone, hydrocortisone.",
    "corticosteroids": "Adrenal steroid hormones or synthetic analogs with potent anti-inflammatory and immunosuppressive activity.",
    "diuretic": "Diuretics enhance renal excretion of water and electrolytes. Classes: loop diuretics (furosemide — inhibit Na-K-2Cl cotransporter), thiazides (hydrochlorothiazide — inhibit NCC), potassium-sparing (spironolactone — aldosterone antagonist).",
    "diuretics": "Agents increasing urinary output by acting on specific renal tubular transport mechanisms.",
    "antifungal": "Antifungal agents exploit the fungal cell membrane's reliance on ergosterol. Azoles (fluconazole) inhibit ergosterol synthesis; polyenes (amphotericin B) bind ergosterol; echinocandins (caspofungin) inhibit β-1,3-glucan synthase.",
    "antifungals": "Agents targeting fungal-specific structures: ergosterol biosynthesis (azoles), ergosterol binding (polyenes), or cell wall synthesis (echinocandins).",
    "antiviral": "Antivirals interfere with specific viral replication stages: nucleoside analogs (acyclovir — herpes), protease inhibitors (lopinavir — HIV), neuraminidase inhibitors (oseltamivir — influenza), reverse transcriptase inhibitors (tenofovir — HIV).",
    "antivirals": "Agents targeting specific viral replication enzymes or structural proteins.",
    "analgesic": "Analgesics relieve pain through different mechanisms. WHO analgesic ladder: Step 1 — non-opioids (paracetamol, NSAIDs); Step 2 — weak opioids (codeine); Step 3 — strong opioids (morphine).",
    "analgesics": "Pain-relieving agents classified as non-opioid (paracetamol, NSAIDs) or opioid (codeine, morphine).",
    "receptor": "Receptors are macromolecular drug targets (usually proteins). Types: ionotropic (ligand-gated ion channels), metabotropic (GPCRs), enzyme-linked receptors, and nuclear receptors.",
    "bioavailability": "Bioavailability (F) is the fraction of administered drug reaching systemic circulation unchanged. IV = 100%. Oral bioavailability affected by first-pass hepatic metabolism, gut wall metabolism, and formulation factors.",
    "pharmacology": "Pharmacology is the science of drug action — including pharmacokinetics, pharmacodynamics, toxicology, chemotherapy, and clinical pharmacology.",
    "toxicology": "Toxicology studies adverse effects of chemicals and drugs. Key concepts: LD50, therapeutic index (TI = TD50/ED50), dose-response relationship, and antidote management.",
    "steroid": "Steroids are lipophilic molecules with a characteristic 4-ring cyclopentanoperhydrophenanthrene nucleus. Include glucocorticoids, mineralocorticoids, sex hormones, and anabolic steroids.",
    "steroids": "Lipid-soluble 4-ring structures with diverse hormonal and pharmacological activity.",
    "dosage form": "Pharmaceutical dosage forms are drug delivery systems: tablets, capsules, injections, solutions, suspensions, patches, inhalers, and suppositories. Choice affects bioavailability, onset, and patient compliance.",
    "pharmaceutical": "Pharmaceutical sciences encompass drug design, formulation, quality control, pharmacokinetics, and clinical therapeutics within the D.Pharm curriculum.",
    "antimalarial": "Antimalarials target the Plasmodium parasite at different lifecycle stages. Classes: quinolines (chloroquine, quinine), antifolates (pyrimethamine), artemisinins (artemether), and atovaquone. Kenya primarily uses artemisinin-based combination therapy (ACT).",
    "antiparasitic": "Antiparasitic drugs act against protozoa, helminths, or ectoparasites. Examples: metronidazole (anaerobic protozoa), albendazole (helminths), ivermectin (ectoparasites).",
}


def identify_concept(text: str) -> Optional[str]:
    """Find the first matching Pharmacy180 concept in the analysis text."""
    lower = text.lower()
    # Sort by length desc so longer (more specific) concepts match first
    for key in sorted(PHARMACY180_MAP.keys(), key=len, reverse=True):
        if key in lower:
            return key
    return None


# ── Filename helpers ──────────────────────────────────────────────────────────
def _secure_filename(filename: str) -> str:
    filename = unicodedata.normalize("NFKD", filename)
    filename = filename.encode("ascii", "ignore").decode("ascii")
    filename = filename.replace("\x00", "").replace("/", "_").replace("\\", "_")
    stem, _, suffix = filename.rpartition(".")
    suffix = suffix.lower()
    stem = re.sub(r"[^\w\-]", "_", stem or "file")
    stem = re.sub(r"_+", "_", stem).strip("_") or "file"
    return f"{stem}.{suffix}"


async def _unique_disk_path(safe_name: str) -> Path:
    target = settings.UPLOAD_DIR / safe_name
    if not target.exists():
        return target
    stem = Path(safe_name).stem
    suffix = Path(safe_name).suffix
    counter = 1
    while True:
        candidate = settings.UPLOAD_DIR / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


# ── POST /api/upload ──────────────────────────────────────────────────────────
@router.post(
    "/upload",
    response_model=ResourceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a new study resource",
)
async def upload_resource(
    file: UploadFile,
    title: str = Form(..., min_length=2, max_length=512),
    subject: str = Form(..., min_length=1, max_length=256),
    semester: str = Form(...),
    db: AsyncSession = Depends(get_db),
) -> ResourceOut:
    if semester not in settings.VALID_SEMESTERS:
        raise HTTPException(400, f"Invalid semester '{semester}'.")

    original_name = file.filename or "upload"
    ext = Path(original_name).suffix.lower()
    if ext not in settings.ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"File type '{ext}' not permitted. Accepted: {', '.join(sorted(settings.ALLOWED_EXTENSIONS))}")

    # Enforce max upload size (read header Content-Length or stream check)
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    size_read = 0
    chunks: list[bytes] = []
    while True:
        chunk = await file.read(256 * 1024)
        if not chunk:
            break
        size_read += len(chunk)
        if size_read > max_bytes:
            raise HTTPException(413, f"File exceeds the {settings.MAX_UPLOAD_SIZE_MB} MB limit.")
        chunks.append(chunk)

    safe_name = _secure_filename(original_name)
    disk_path = await _unique_disk_path(safe_name)
    final_name = disk_path.name

    try:
        async with aiofiles.open(disk_path, "wb") as f:
            for chunk in chunks:
                await f.write(chunk)
    except OSError as exc:
        raise HTTPException(500, f"Failed to save file: {exc}") from exc

    resource = Resource(
        title=title.strip(),
        subject=subject.strip(),
        semester=semester,
        file_name=final_name,
        file_path=f"/uploaded_notes/{final_name}",
    )
    db.add(resource)
    await db.flush()
    await db.refresh(resource)
    return ResourceOut.model_validate(resource)


# ── GET /api/notes ────────────────────────────────────────────────────────────
@router.get("/notes", response_model=ResourceListResponse, summary="List resources")
async def list_notes(
    semester: Optional[str] = None,
    subject: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
) -> ResourceListResponse:
    stmt = select(Resource).order_by(Resource.upload_date.desc())
    if semester:
        if semester not in settings.VALID_SEMESTERS:
            raise HTTPException(400, f"Invalid semester '{semester}'.")
        stmt = stmt.where(Resource.semester == semester)
    if subject:
        stmt = stmt.where(Resource.subject.ilike(f"%{subject.strip()}%"))

    rows = (await db.execute(stmt)).scalars().all()
    return ResourceListResponse(total=len(rows), items=[ResourceOut.model_validate(r) for r in rows])


# ── GET /api/notes/stats ──────────────────────────────────────────────────────
@router.get("/notes/stats", response_model=ResourceStats, summary="Resource statistics")
async def get_stats(db: AsyncSession = Depends(get_db)) -> ResourceStats:
    total_result = await db.execute(select(func.count()).select_from(Resource))
    total = total_result.scalar() or 0

    sem_result = await db.execute(
        select(Resource.semester, func.count().label("count"))
        .group_by(Resource.semester)
        .order_by(Resource.semester)
    )
    by_semester = [SemesterCount(semester=r.semester, count=r.count) for r in sem_result]

    sub_result = await db.execute(
        select(Resource.subject, func.count().label("count"))
        .group_by(Resource.subject)
        .order_by(func.count().desc())
        .limit(10)
    )
    by_subject = [SubjectCount(subject=r.subject, count=r.count) for r in sub_result]

    recent_result = await db.execute(
        select(Resource).order_by(Resource.upload_date.desc()).limit(5)
    )
    recent = [ResourceOut.model_validate(r) for r in recent_result.scalars().all()]

    return ResourceStats(total=total, by_semester=by_semester, by_subject=by_subject, recent=recent)


# ── DELETE /api/notes/{id} ────────────────────────────────────────────────────
@router.delete("/notes/{note_id}", response_model=MessageResponse, summary="Delete a resource")
async def delete_note(note_id: int, db: AsyncSession = Depends(get_db)) -> MessageResponse:
    result = await db.execute(select(Resource).where(Resource.id == note_id))
    resource = result.scalar_one_or_none()
    if resource is None:
        raise HTTPException(404, f"Resource id={note_id} not found.")

    disk_path = settings.UPLOAD_DIR / resource.file_name
    if disk_path.exists():
        try:
            os.unlink(disk_path)
        except OSError as exc:
            raise HTTPException(500, f"Could not delete file: {exc}") from exc

    await db.delete(resource)
    return MessageResponse(message="Resource deleted.", detail=f"Removed '{resource.file_name}'.")


# ── POST /api/analyze ─────────────────────────────────────────────────────────
@router.post("/analyze", response_model=AnalysisResponse, summary="AI pharmacy analysis")
async def analyze_content(body: AnalysisRequest) -> AnalysisResponse:
    client = get_groq()

    # Build messages based on text-only vs file-assisted mode
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    if not body.text_only and body.file_data and body.file_name:
        # File-assisted pipeline: decode base64 and embed content excerpt in prompt
        import base64 as _b64
        ext = (body.file_name.rsplit(".", 1)[-1] or "").upper()
        try:
            raw_bytes = _b64.b64decode(body.file_data)
            # Try to extract plain text for PDF-like or text-based formats
            try:
                excerpt = raw_bytes.decode("utf-8", errors="replace")[:6000]
            except Exception:
                excerpt = f"[Binary file — {len(raw_bytes):,} bytes; analyse from filename and prompt context]"
        except Exception:
            excerpt = "[Could not decode file content]"

        file_ctx = (
            f"The user has uploaded a pharmacy study document for analysis.\n"
            f"Filename: {body.file_name} ({ext})\n"
            f"File size: ~{len(body.file_data) // 1365} KB\n\n"
            f"--- BEGIN DOCUMENT CONTENT ---\n{excerpt}\n--- END DOCUMENT CONTENT ---\n\n"
            f"User analysis prompt: {body.prompt}\n\n"
            "Based on the document content above and the user's prompt, provide a thorough "
            "D.Pharm-level pharmaceutical analysis. Include PubChem structural images for all "
            "drugs or chemical entities identified in the document."
        )
        messages.append({"role": "user", "content": file_ctx})
    else:
        # Pure text-only pipeline — bypass file handling, route straight to text completion
        messages.append({"role": "user", "content": body.prompt})

    try:
        completion = await client.chat.completions.create(
            model=settings.GROQ_MODEL,
            messages=messages,
            max_tokens=settings.GROQ_MAX_TOKENS,
            temperature=settings.GROQ_TEMPERATURE,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AI analysis service unavailable: {type(exc).__name__}: {exc}",
        ) from exc

    if not completion.choices:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Groq returned an empty choices list — no response generated.",
        )

    raw = completion.choices[0].message.content or "No response generated."

    # Strip <think>…</think> reasoning blocks from DeepSeek-R1
    import re as _re
    analysis = _re.sub(r"<think>[\s\S]*?</think>", "", raw).strip()

    # Identify Pharmacy180 concept and append reference block
    concept_key = identify_concept(analysis + " " + body.prompt)
    pharmacy180_ref: Optional[Pharmacy180Ref] = None

    if concept_key and concept_key in PHARMACY180_MAP:
        display_concept = concept_key.title()
        pharmacy180_ref = Pharmacy180Ref(
            concept=display_concept,
            summary=PHARMACY180_MAP[concept_key],
            url="https://www.pharmacy180.com/",
        )
        analysis += (
            f"\n\n---\n\n"
            f"### 🌐 Pharmacy180 Reference Integration\n"
            f"> **Concept:** {display_concept}\n>\n"
            f"> {PHARMACY180_MAP[concept_key]}\n>\n"
            f"> 🔗 [Read Full Notes Portfolio on Pharmacy180](https://www.pharmacy180.com/)"
        )

    return AnalysisResponse(
        analysis=analysis,
        concept=concept_key,
        pharmacy180_ref=pharmacy180_ref,
        model=completion.model,
        tokens_used=completion.usage.total_tokens if completion.usage else None,
    )
