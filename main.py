"""
Patient-Language Bridge — FastAPI Backend
==========================================
Endpoints:
  POST /api/analyze-report     → Analyze medical report with Gemini, return JSON + generate PDF
  POST /api/chat               → Streaming chatbot using latest report from DB
  POST /api/auth/signup        → User registration
  POST /api/auth/login         → User login → returns JWT token

Run:
  pip install fastapi uvicorn google-generativeai reportlab python-jose passlib[bcrypt] python-multipart supabase
  uvicorn main:app --reload --port 8000
"""

import os, json, uuid, hashlib, re
from datetime import datetime, timedelta
from typing import AsyncGenerator

import google.generativeai as genai
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from jose import jwt, JWTError
from passlib.context import CryptContext
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.enums import TA_CENTER, TA_LEFT

# ─── CONFIG ────────────────────────────────────────────────
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")
JWT_SECRET      = os.getenv("JWT_SECRET", "your-super-secret-jwt-key-change-this")
JWT_ALGORITHM   = "HS256"
JWT_EXPIRE_MINS = 60 * 24   # 24 hours
PDF_OUTPUT_DIR  = "generated_reports"
os.makedirs(PDF_OUTPUT_DIR, exist_ok=True)

genai.configure(api_key=GEMINI_API_KEY)

# ─── IN-MEMORY STORES (replace with Supabase/DB in production) ─
users_db: dict   = {}   # email → {id, email, hashed_password, name, age, gender, known_conditions, medications}
reports_db: dict = {}   # user_id → [list of report analysis dicts, latest first]

# ─── SECURITY ──────────────────────────────────────────────
pwd_ctx  = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer   = HTTPBearer()

def hash_password(password: str) -> str:
    return pwd_ctx.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_ctx.verify(plain, hashed)

def create_jwt(user_id: str, email: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "exp": datetime.utcnow() + timedelta(minutes=JWT_EXPIRE_MINS)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

def get_current_user(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> dict:
    payload  = decode_jwt(creds.credentials)
    user_id  = payload.get("sub")
    email    = payload.get("email")
    if email not in users_db:
        raise HTTPException(status_code=401, detail="User not found")
    return users_db[email]

# ─── APP ───────────────────────────────────────────────────
app = FastAPI(
    title="Patient-Language Bridge API",
    description="AI-powered medical report simplification platform",
    version="1.0.0"
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── MASTER PROMPT ─────────────────────────────────────────
def build_analysis_prompt(
    report_text: str,
    patient_name: str,
    patient_age: int,
    patient_gender: str,
    known_conditions: str,
    current_medications: str
) -> str:
    return f"""
You are an expert medical AI assistant. A patient has uploaded their medical report.
Analyze it completely and return ONLY a single valid JSON object — no markdown, no backticks, no extra text.

PATIENT CONTEXT:
- Name: {patient_name}
- Age: {patient_age}
- Gender: {patient_gender}
- Known Conditions: {known_conditions}
- Current Medications: {current_medications}

MEDICAL REPORT TEXT:
{report_text}

Return this exact JSON structure (fill all fields based on the report above):

{{
  "report_meta": {{
    "report_type": "e.g. Blood Test / MRI / Discharge Summary",
    "report_date": "date found in report or null",
    "lab_name": "lab or hospital name or null",
    "doctor_name": "doctor name or null",
    "analyzed_at": "{datetime.utcnow().isoformat()}"
  }},
  "simple_summary": {{
    "headline": "One sentence a 10-year-old would understand",
    "plain_english": "3-5 sentences, zero jargon, 5th grade level explaining the whole report",
    "key_findings": ["Finding 1", "Finding 2", "Finding 3"],
    "good_news": "What is normal or positive, stated reassuringly",
    "concerns": "What needs attention, stated calmly"
  }},
  "health_analytics": {{
    "overall_health_score": {{
      "score": 75,
      "out_of": 100,
      "grade": "B",
      "interpretation": "Plain language explanation of this score"
    }},
    "risk_level": {{
      "level": "LOW / MODERATE / HIGH / CRITICAL",
      "color": "GREEN / YELLOW / ORANGE / RED",
      "reason": "Why this risk level"
    }},
    "biomarker_analysis": [
      {{
        "name": "Biomarker name",
        "value": "patient value",
        "unit": "unit",
        "normal_range": "min - max",
        "status": "LOW / NORMAL / HIGH / CRITICAL",
        "plain_meaning": "what this means in plain English",
        "trend": "IMPROVING / STABLE / WORSENING / UNKNOWN"
      }}
    ],
    "organ_health": {{
      "heart":       "GOOD / FAIR / NEEDS_ATTENTION / CRITICAL / NOT_TESTED",
      "kidneys":     "GOOD / FAIR / NEEDS_ATTENTION / CRITICAL / NOT_TESTED",
      "liver":       "GOOD / FAIR / NEEDS_ATTENTION / CRITICAL / NOT_TESTED",
      "lungs":       "GOOD / FAIR / NEEDS_ATTENTION / CRITICAL / NOT_TESTED",
      "blood":       "GOOD / FAIR / NEEDS_ATTENTION / CRITICAL / NOT_TESTED",
      "thyroid":     "GOOD / FAIR / NEEDS_ATTENTION / CRITICAL / NOT_TESTED",
      "blood_sugar": "GOOD / FAIR / NEEDS_ATTENTION / CRITICAL / NOT_TESTED"
    }},
    "critical_alerts": [
      {{
        "parameter": "name of critical value",
        "value": "dangerous value",
        "reason": "why dangerous",
        "urgency": "GO TO ER NOW / SEE DOCTOR TODAY / SEE DOCTOR THIS WEEK",
        "action": "exact action patient must take"
      }}
    ],
    "lifestyle_risk_factors": {{
      "diabetes_risk":       "LOW / MODERATE / HIGH",
      "heart_disease_risk":  "LOW / MODERATE / HIGH",
      "anemia_risk":         "LOW / MODERATE / HIGH",
      "kidney_disease_risk": "LOW / MODERATE / HIGH",
      "infection_risk":      "LOW / MODERATE / HIGH"
    }}
  }},
  "personalized_plan": {{
    "next_steps": [
      {{
        "priority": "URGENT / HIGH / MEDIUM / LOW",
        "action": "Exact action",
        "reason": "Why needed",
        "timeframe": "Within X days"
      }}
    ],
    "medications": [
      {{
        "name": "medication name",
        "dosage": "dosage",
        "frequency": "how often",
        "plain_instruction": "plain language instruction",
        "important_note": "important note"
      }}
    ],
    "follow_up_tests": [
      {{
        "test_name": "test name",
        "reason": "why needed",
        "when": "when to get it",
        "urgency": "URGENT / ROUTINE"
      }}
    ],
    "specialist_referrals": [
      {{
        "specialist": "specialist type",
        "reason": "why needed",
        "urgency": "URGENT / WITHIN_1_MONTH / ROUTINE"
      }}
    ],
    "diet_plan": {{
      "foods_to_eat":  ["Food 1 with reason", "Food 2 with reason"],
      "foods_to_avoid": ["Food 1 with reason", "Food 2 with reason"],
      "hydration": "water and fluid advice",
      "meal_timing": "meal timing advice"
    }},
    "lifestyle_recommendations": [
      {{
        "category": "Exercise / Sleep / Stress / Habits",
        "recommendation": "Specific advice",
        "reason": "Why this helps"
      }}
    ],
    "daily_health_checklist": [
      "Morning: action",
      "Afternoon: action",
      "Evening: action",
      "Night: action"
    ]
  }},
  "personalized_faqs": [
    {{
      "question": "Is this result normal for my age?",
      "answer": "Personalised answer referencing actual values"
    }},
    {{
      "question": "Should I be worried?",
      "answer": "Honest calm personalised answer"
    }},
    {{
      "question": "What does the most abnormal finding mean?",
      "answer": "Plain English explanation"
    }},
    {{
      "question": "Do I need to go to hospital right now?",
      "answer": "Clear yes or no with reasoning"
    }},
    {{
      "question": "What happens if I ignore this report?",
      "answer": "Honest consequences of inaction"
    }},
    {{
      "question": "How long will it take to get better?",
      "answer": "Realistic timeline"
    }},
    {{
      "question": "Can I continue daily activities?",
      "answer": "Specific guidance on work exercise travel"
    }},
    {{
      "question": "What should I tell my family?",
      "answer": "Simple talking points"
    }},
    {{
      "question": "Will I need surgery or hospitalization?",
      "answer": "Assessment from this report"
    }},
    {{
      "question": "What is the ONE most important thing to do today?",
      "answer": "Single most critical action"
    }}
  ],
  "doctor_consultation_brief": {{
    "summary_for_doctor": "2-3 sentence brief the patient can show their doctor",
    "questions_to_ask_doctor": [
      "Question 1",
      "Question 2",
      "Question 3"
    ]
  }},
  "report_confidence": {{
    "confidence_score": 85,
    "out_of": 100,
    "limitations": "What this AI cannot determine and why a real doctor is still needed"
  }}
}}

RULES:
- Return ONLY valid JSON. Nothing else.
- Base ALL answers on the actual report text provided.
- Use null for anything not found in the report.
- critical_alerts only for genuinely dangerous values.
- plain_english must have ZERO medical jargon.
- personalized_faqs must reference this patient's actual values.
"""

# ─── PDF GENERATOR ─────────────────────────────────────────
def generate_pdf(analysis: dict, patient_name: str, report_id: str) -> str:
    filename = f"{PDF_OUTPUT_DIR}/report_{report_id}.pdf"
    doc = SimpleDocTemplate(filename, pagesize=A4,
                            rightMargin=0.75*inch, leftMargin=0.75*inch,
                            topMargin=0.75*inch, bottomMargin=0.75*inch)
    styles = getSampleStyleSheet()
    story  = []

    # Custom styles
    h1 = ParagraphStyle("H1", parent=styles["Heading1"],
                         fontSize=20, textColor=colors.HexColor("#1A2B4A"),
                         spaceAfter=4, spaceBefore=0)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"],
                         fontSize=13, textColor=colors.HexColor("#0891B2"),
                         spaceAfter=4, spaceBefore=12,
                         borderPad=4, borderColor=colors.HexColor("#0891B2"),
                         borderWidth=0, leftIndent=0)
    body_style = ParagraphStyle("Body", parent=styles["Normal"],
                                fontSize=10, leading=15,
                                textColor=colors.HexColor("#1E293B"))
    small = ParagraphStyle("Small", parent=styles["Normal"],
                           fontSize=9, leading=13,
                           textColor=colors.HexColor("#475569"))

    def section(title):
        story.append(Spacer(1, 8))
        story.append(Paragraph(title, h2))
        story.append(HRFlowable(width="100%", thickness=0.5,
                                color=colors.HexColor("#CBD5E1")))
        story.append(Spacer(1, 4))

    def bullet(text):
        story.append(Paragraph(f"• {text}", body_style))

    # ── Header ──
    story.append(Paragraph("Patient-Language Bridge", h1))
    story.append(Paragraph("AI Medical Report Analysis", styles["Heading3"]))
    story.append(Paragraph(f"Patient: {patient_name}  |  Report ID: {report_id}  |  "
                           f"Generated: {datetime.utcnow().strftime('%d %b %Y %H:%M UTC')}",
                           small))
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=1.5,
                            color=colors.HexColor("#0891B2")))
    story.append(Spacer(1, 10))

    # ── Simple Summary ──
    ss = analysis.get("simple_summary", {})
    section("📋 Simple Summary")
    if ss.get("headline"):
        story.append(Paragraph(f"<b>{ss['headline']}</b>", body_style))
        story.append(Spacer(1, 4))
    if ss.get("plain_english"):
        story.append(Paragraph(ss["plain_english"], body_style))
        story.append(Spacer(1, 6))
    for f in ss.get("key_findings", []):
        bullet(f)
    if ss.get("good_news"):
        story.append(Spacer(1, 4))
        story.append(Paragraph(f"<b>✅ Good News:</b> {ss['good_news']}", body_style))
    if ss.get("concerns"):
        story.append(Paragraph(f"<b>⚠️ Concerns:</b> {ss['concerns']}", body_style))

    # ── Health Analytics ──
    ha = analysis.get("health_analytics", {})
    section("📊 Health Analytics")

    hs = ha.get("overall_health_score", {})
    rl = ha.get("risk_level", {})
    risk_color = {"GREEN": "#059669", "YELLOW": "#D97706",
                  "ORANGE": "#E85D3A", "RED": "#DC2626"}.get(rl.get("color",""), "#1A2B4A")

    analytics_data = [
        ["Health Score", f"{hs.get('score','?')}/100 — Grade {hs.get('grade','?')}"],
        ["Risk Level",   f"{rl.get('level','?')}  |  {rl.get('reason','')}"],
    ]
    tbl = Table(analytics_data, colWidths=[1.5*inch, 5*inch])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (0,-1), colors.HexColor("#EEF2F7")),
        ("FONTNAME",   (0,0), (0,-1), "Helvetica-Bold"),
        ("FONTSIZE",   (0,0), (-1,-1), 10),
        ("GRID",       (0,0), (-1,-1), 0.4, colors.HexColor("#CBD5E1")),
        ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING", (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 8))

    # Biomarkers table
    biomarkers = ha.get("biomarker_analysis", [])
    if biomarkers:
        story.append(Paragraph("<b>Biomarker Results:</b>", body_style))
        story.append(Spacer(1, 4))
        bm_data = [["Biomarker", "Your Value", "Normal Range", "Status", "Meaning"]]
        for bm in biomarkers:
            status_color = {"NORMAL":"#059669","LOW":"#D97706",
                           "HIGH":"#E85D3A","CRITICAL":"#DC2626"}.get(bm.get("status",""),"#1A2B4A")
            bm_data.append([
                bm.get("name",""),
                f"{bm.get('value','')} {bm.get('unit','')}",
                bm.get("normal_range",""),
                bm.get("status",""),
                bm.get("plain_meaning","")[:60]
            ])
        bm_tbl = Table(bm_data, colWidths=[1.1*inch, 0.9*inch, 1*inch, 0.8*inch, 2.85*inch])
        bm_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,0), colors.HexColor("#1A2B4A")),
            ("TEXTCOLOR",     (0,0), (-1,0), colors.white),
            ("FONTNAME",      (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE",      (0,0), (-1,-1), 8),
            ("GRID",          (0,0), (-1,-1), 0.3, colors.HexColor("#CBD5E1")),
            ("ROWBACKGROUNDS",(0,1), (-1,-1), [colors.white, colors.HexColor("#F7F9FC")]),
            ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
            ("TOPPADDING",    (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ]))
        story.append(bm_tbl)

    # Critical alerts
    alerts = ha.get("critical_alerts", [])
    if alerts:
        section("🚨 Critical Alerts")
        for alert in alerts:
            story.append(Paragraph(
                f"<b>{alert.get('parameter','')}:</b> {alert.get('value','')} — "
                f"<font color='#DC2626'><b>{alert.get('urgency','')}</b></font>",
                body_style))
            story.append(Paragraph(f"Action: {alert.get('action','')}", small))
            story.append(Spacer(1, 4))

    # ── Personalized Plan ──
    pp = analysis.get("personalized_plan", {})
    section("🗺️ Your Personalized Plan")

    next_steps = pp.get("next_steps", [])
    if next_steps:
        story.append(Paragraph("<b>Next Steps:</b>", body_style))
        for step in next_steps:
            story.append(Paragraph(
                f"[{step.get('priority','')}] {step.get('action','')} "
                f"— {step.get('timeframe','')}",
                body_style))
            story.append(Paragraph(f"  Why: {step.get('reason','')}", small))

    diet = pp.get("diet_plan", {})
    if diet:
        story.append(Spacer(1, 6))
        story.append(Paragraph("<b>Diet Plan:</b>", body_style))
        for food in diet.get("foods_to_eat", []):
            bullet(f"✅ {food}")
        for food in diet.get("foods_to_avoid", []):
            bullet(f"❌ {food}")
        if diet.get("hydration"):
            bullet(f"💧 {diet['hydration']}")

    checklist = pp.get("daily_health_checklist", [])
    if checklist:
        story.append(Spacer(1, 6))
        story.append(Paragraph("<b>Daily Checklist:</b>", body_style))
        for item in checklist:
            bullet(item)

    # ── FAQs ──
    section("❓ Your Personal FAQs")
    for faq in analysis.get("personalized_faqs", []):
        story.append(Paragraph(f"<b>Q: {faq.get('question','')}</b>", body_style))
        story.append(Paragraph(f"A: {faq.get('answer','')}", small))
        story.append(Spacer(1, 5))

    # ── Doctor Brief ──
    db = analysis.get("doctor_consultation_brief", {})
    section("🩺 For Your Doctor")
    if db.get("summary_for_doctor"):
        story.append(Paragraph(db["summary_for_doctor"], body_style))
    for q in db.get("questions_to_ask_doctor", []):
        bullet(q)

    # ── Disclaimer ──
    story.append(Spacer(1, 16))
    story.append(HRFlowable(width="100%", thickness=0.5,
                            color=colors.HexColor("#CBD5E1")))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        "⚠️ DISCLAIMER: This report is generated by AI for informational purposes only. "
        "It is NOT a medical diagnosis. Always consult a qualified healthcare professional "
        "before making any medical decisions. In case of emergency, call 112 immediately.",
        ParagraphStyle("Disclaimer", parent=styles["Normal"],
                       fontSize=8, textColor=colors.HexColor("#94A3B8"),
                       leading=12)))

    doc.build(story)
    return filename


# ══════════════════════════════════════════════════════════
# API 1 — ANALYZE MEDICAL REPORT
# ══════════════════════════════════════════════════════════
class AnalyzeRequest(BaseModel):
    report_text: str
    patient_name: str   = "Patient"
    patient_age: int    = 30
    patient_gender: str = "Not specified"
    known_conditions: str = "None"
    current_medications: str = "None"

@app.post("/api/analyze-report", summary="Analyze medical report with Gemini AI")
async def analyze_report(
    request: AnalyzeRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    Analyzes a medical report using Gemini 2.5 Pro.
    Returns:
    - Full JSON analysis (summary, analytics, plan, FAQs)
    - Path to generated PDF report
    - Stores analysis in user's report history
    """
    try:
        # Call Gemini 2.5 Pro
        model  = genai.GenerativeModel("gemini-2.5-pro-preview-06-05")
        prompt = build_analysis_prompt(
            report_text          = request.report_text,
            patient_name         = request.patient_name or current_user.get("name", "Patient"),
            patient_age          = request.patient_age  or current_user.get("age", 30),
            patient_gender       = request.patient_gender or current_user.get("gender", "Not specified"),
            known_conditions     = request.known_conditions,
            current_medications  = request.current_medications,
        )
        response = model.generate_content(prompt)
        raw_text = response.text.strip()

        # Clean and parse JSON
        raw_text = re.sub(r"^```json\s*", "", raw_text)
        raw_text = re.sub(r"\s*```$",     "", raw_text)
        analysis = json.loads(raw_text)

        # Generate report ID and save
        report_id  = str(uuid.uuid4())[:8].upper()
        user_id    = current_user["id"]
        patient_nm = request.patient_name or current_user.get("name", "Patient")

        # Generate PDF
        pdf_path = generate_pdf(analysis, patient_nm, report_id)

        # Save JSON to file
        json_path = f"{PDF_OUTPUT_DIR}/report_{report_id}.json"
        with open(json_path, "w") as f:
            json.dump(analysis, f, indent=2)

        # Store in DB
        report_entry = {
            "report_id":   report_id,
            "analyzed_at": datetime.utcnow().isoformat(),
            "analysis":    analysis,
            "pdf_path":    pdf_path,
            "json_path":   json_path,
        }
        if user_id not in reports_db:
            reports_db[user_id] = []
        reports_db[user_id].insert(0, report_entry)   # latest first

        return {
            "success":   True,
            "report_id": report_id,
            "analysis":  analysis,
            "pdf_url":   f"/api/download/pdf/{report_id}",
            "json_url":  f"/api/download/json/{report_id}",
            "message":   "Report analyzed successfully"
        }

    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail=f"Gemini returned invalid JSON: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/download/pdf/{report_id}")
async def download_pdf(report_id: str):
    path = f"{PDF_OUTPUT_DIR}/report_{report_id}.pdf"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="PDF not found")
    return FileResponse(path, media_type="application/pdf",
                        filename=f"medical_report_{report_id}.pdf")

@app.get("/api/download/json/{report_id}")
async def download_json(report_id: str):
    path = f"{PDF_OUTPUT_DIR}/report_{report_id}.json"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="JSON not found")
    return FileResponse(path, media_type="application/json",
                        filename=f"medical_report_{report_id}.json")


# ══════════════════════════════════════════════════════════
# API 2 — STREAMING CHATBOT (send "STOP" to end)
# ══════════════════════════════════════════════════════════
class ChatRequest(BaseModel):
    message: str

@app.post("/api/chat", summary="Streaming AI chatbot about your latest report")
async def chat(
    request: ChatRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    Streaming chatbot endpoint.
    - Uses the user's latest report from DB as context.
    - Send message "STOP" to end the session.
    - Response streams token by token using Server-Sent Events.
    """
    user_id = current_user["id"]

    # Check for STOP signal
    if request.message.strip().upper() == "STOP":
        return {"message": "Chat session ended. Goodbye!"}

    # Get latest report context
    user_reports    = reports_db.get(user_id, [])
    latest_analysis = user_reports[0]["analysis"] if user_reports else None

    report_context = ""
    if latest_analysis:
        ss = latest_analysis.get("simple_summary", {})
        ha = latest_analysis.get("health_analytics", {})
        report_context = f"""
PATIENT'S LATEST MEDICAL REPORT CONTEXT:
Report Type: {latest_analysis.get('report_meta', {}).get('report_type', 'Unknown')}
Analyzed: {latest_analysis.get('report_meta', {}).get('analyzed_at', 'Unknown')}

Summary: {ss.get('plain_english', 'No summary available')}

Health Score: {ha.get('overall_health_score', {}).get('score', '?')}/100
Risk Level: {ha.get('risk_level', {}).get('level', 'Unknown')}

Key Findings:
{chr(10).join('- ' + f for f in ss.get('key_findings', []))}

Biomarkers:
{chr(10).join(f"- {bm['name']}: {bm['value']} {bm['unit']} ({bm['status']})" for bm in ha.get('biomarker_analysis', []))}
"""
    else:
        report_context = "No medical report has been uploaded yet for this patient."

    system_prompt = f"""You are a caring, knowledgeable medical AI assistant for the Patient-Language Bridge app.
You are helping a patient understand their medical report.

{report_context}

RULES:
- Always speak in simple, plain English — no medical jargon.
- Be empathetic, calm, and reassuring.
- Reference the patient's actual report values when answering.
- If asked something outside the report, say "I can only answer based on your uploaded report."
- Never diagnose. Always recommend seeing a doctor for serious concerns.
- If the patient types STOP, end the conversation kindly.
- Keep responses concise — under 150 words unless more detail is specifically asked for.

Patient's question: {request.message}
"""

    async def stream_response() -> AsyncGenerator[str, None]:
        try:
            model = genai.GenerativeModel("gemini-2.5-pro-preview-06-05")
            response = model.generate_content(
                system_prompt,
                generation_config=genai.GenerationConfig(
                    temperature=0.4,
                    max_output_tokens=512,
                )
            )
            # Stream word by word
            words = response.text.split(" ")
            for i, word in enumerate(words):
                chunk = word + (" " if i < len(words)-1 else "")
                yield f"data: {json.dumps({'token': chunk, 'done': False})}\n\n"
                import asyncio
                await asyncio.sleep(0.02)   # simulate streaming
            yield f"data: {json.dumps({'token': '', 'done': True})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e), 'done': True})}\n\n"

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


# ══════════════════════════════════════════════════════════
# API 3 — AUTH: SIGNUP + LOGIN
# ══════════════════════════════════════════════════════════
class SignupRequest(BaseModel):
    name: str
    email: EmailStr
    password: str
    age: int            = 30
    gender: str         = "Not specified"
    known_conditions: str  = "None"
    current_medications: str = "None"

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

@app.post("/api/auth/signup", status_code=status.HTTP_201_CREATED,
          summary="Register a new user")
async def signup(request: SignupRequest):
    """
    Register a new patient account.
    Returns JWT token on success.
    """
    if request.email in users_db:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists"
        )
    if len(request.password) < 8:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password must be at least 8 characters"
        )

    user_id = str(uuid.uuid4())
    users_db[request.email] = {
        "id":                  user_id,
        "name":                request.name,
        "email":               request.email,
        "hashed_password":     hash_password(request.password),
        "age":                 request.age,
        "gender":              request.gender,
        "known_conditions":    request.known_conditions,
        "current_medications": request.current_medications,
        "created_at":          datetime.utcnow().isoformat(),
    }

    token = create_jwt(user_id, request.email)
    return {
        "success": True,
        "message": "Account created successfully",
        "token":   token,
        "user": {
            "id":    user_id,
            "name":  request.name,
            "email": request.email,
        }
    }

@app.post("/api/auth/login", summary="Login and receive JWT token")
async def login(request: LoginRequest):
    """
    Login with email + password.
    Returns JWT token valid for 24 hours.
    """
    user = users_db.get(request.email)

    if not user or not verify_password(request.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password"
        )

    token = create_jwt(user["id"], request.email)
    return {
        "success": True,
        "message": "Login successful",
        "token":   token,
        "user": {
            "id":     user["id"],
            "name":   user["name"],
            "email":  user["email"],
            "age":    user["age"],
            "gender": user["gender"],
        }
    }

@app.get("/api/auth/me", summary="Get current logged-in user profile")
async def get_me(current_user: dict = Depends(get_current_user)):
    return {
        "id":                  current_user["id"],
        "name":                current_user["name"],
        "email":               current_user["email"],
        "age":                 current_user["age"],
        "gender":              current_user["gender"],
        "known_conditions":    current_user["known_conditions"],
        "current_medications": current_user["current_medications"],
        "created_at":          current_user["created_at"],
        "total_reports":       len(reports_db.get(current_user["id"], []))
    }

@app.get("/api/reports/history", summary="Get all past report analyses for logged-in user")
async def get_report_history(current_user: dict = Depends(get_current_user)):
    user_reports = reports_db.get(current_user["id"], [])
    return {
        "total":   len(user_reports),
        "reports": [
            {
                "report_id":    r["report_id"],
                "analyzed_at":  r["analyzed_at"],
                "report_type":  r["analysis"].get("report_meta", {}).get("report_type", "Unknown"),
                "health_score": r["analysis"].get("health_analytics", {})
                                             .get("overall_health_score", {}).get("score"),
                "risk_level":   r["analysis"].get("health_analytics", {})
                                             .get("risk_level", {}).get("level"),
                "pdf_url":      f"/api/download/pdf/{r['report_id']}",
                "json_url":     f"/api/download/json/{r['report_id']}",
            }
            for r in user_reports
        ]
    }

@app.get("/", summary="Health check")
async def root():
    return {"status": "ok", "app": "Patient-Language Bridge API", "version": "1.0.0"}
