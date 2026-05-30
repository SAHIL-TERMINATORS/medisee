# Patient-Language Bridge — Backend API

## Setup

```bash
pip install -r requirements.txt
```

Create a `.env` file:
```
GEMINI_API_KEY=your_gemini_api_key_here
JWT_SECRET=your_random_secret_string_here
```

Run:
```bash
uvicorn main:app --reload --port 8000
```

Swagger docs at: http://localhost:8000/docs

---

## API Reference

---

### POST /api/auth/signup
Register a new patient.

**Request Body:**
```json
{
  "name": "Rahul Sharma",
  "email": "rahul@example.com",
  "password": "mypassword123",
  "age": 34,
  "gender": "Male",
  "known_conditions": "Hypertension",
  "current_medications": "Amlodipine 5mg"
}
```

**Response:**
```json
{
  "success": true,
  "message": "Account created successfully",
  "token": "eyJ...",
  "user": { "id": "...", "name": "Rahul Sharma", "email": "rahul@example.com" }
}
```

---

### POST /api/auth/login
Login and get JWT token.

**Request Body:**
```json
{
  "email": "rahul@example.com",
  "password": "mypassword123"
}
```

**Response:**
```json
{
  "success": true,
  "token": "eyJ...",
  "user": { "id": "...", "name": "Rahul Sharma", "email": "rahul@example.com" }
}
```

Use the token in all subsequent requests:
```
Authorization: Bearer eyJ...
```

---

### POST /api/analyze-report
Analyze a medical report. Returns full AI analysis + PDF + JSON.

**Headers:** `Authorization: Bearer <token>`

**Request Body:**
```json
{
  "report_text": "Patient: Rahul Sharma, Age: 34\nHemoglobin: 9.2 g/dL (Normal: 13.5-17.5)\nBlood Sugar Fasting: 142 mg/dL (Normal: 70-100)\nCreatinine: 1.8 mg/dL (Normal: 0.7-1.3)\n...",
  "patient_name": "Rahul Sharma",
  "patient_age": 34,
  "patient_gender": "Male",
  "known_conditions": "Hypertension",
  "current_medications": "Amlodipine 5mg"
}
```

**Response:**
```json
{
  "success": true,
  "report_id": "A1B2C3D4",
  "analysis": {
    "report_meta": { ... },
    "simple_summary": {
      "headline": "Your blood sugar is high and you may be slightly anemic",
      "plain_english": "...",
      "key_findings": ["...", "..."],
      "good_news": "...",
      "concerns": "..."
    },
    "health_analytics": {
      "overall_health_score": { "score": 62, "out_of": 100, "grade": "D", "interpretation": "..." },
      "risk_level": { "level": "MODERATE", "color": "YELLOW", "reason": "..." },
      "biomarker_analysis": [ { "name": "Hemoglobin", "value": "9.2", "unit": "g/dL", "status": "LOW", ... } ],
      "organ_health": { "kidneys": "NEEDS_ATTENTION", "blood": "NEEDS_ATTENTION", ... },
      "critical_alerts": [],
      "lifestyle_risk_factors": { "diabetes_risk": "HIGH", ... }
    },
    "personalized_plan": {
      "next_steps": [ { "priority": "HIGH", "action": "...", "timeframe": "Within 3 days" } ],
      "diet_plan": { "foods_to_eat": [...], "foods_to_avoid": [...] },
      "daily_health_checklist": ["Morning: ...", "Night: ..."]
    },
    "personalized_faqs": [ { "question": "...", "answer": "..." } ],
    "doctor_consultation_brief": { "summary_for_doctor": "...", "questions_to_ask_doctor": [...] },
    "report_confidence": { "confidence_score": 88, "out_of": 100, "limitations": "..." }
  },
  "pdf_url": "/api/download/pdf/A1B2C3D4",
  "json_url": "/api/download/json/A1B2C3D4"
}
```

---

### POST /api/chat
Streaming chatbot about the user's latest report.
Send "STOP" to end the session.

**Headers:** `Authorization: Bearer <token>`

**Request Body:**
```json
{ "message": "Should I be worried about my kidney values?" }
```

**Response:** Server-Sent Events stream
```
data: {"token": "Based", "done": false}
data: {"token": " on", "done": false}
data: {"token": " your", "done": false}
...
data: {"token": "", "done": true}
```

**To consume the stream (JavaScript):**
```javascript
const response = await fetch('/api/chat', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${token}`
  },
  body: JSON.stringify({ message: "Is my sugar level dangerous?" })
});

const reader = response.body.getReader();
const decoder = new TextDecoder();

while (true) {
  const { done, value } = await reader.read();
  if (done) break;
  const text = decoder.decode(value);
  const lines = text.split('\n').filter(l => l.startsWith('data:'));
  for (const line of lines) {
    const data = JSON.parse(line.replace('data: ', ''));
    process.stdout.write(data.token);
    if (data.done) break;
  }
}

// To stop:
await fetch('/api/chat', {
  method: 'POST',
  headers: { ... },
  body: JSON.stringify({ message: "STOP" })
});
```

---

### GET /api/auth/me
Get current user profile.

---

### GET /api/reports/history
Get all past report analyses for the logged-in user.

---

### GET /api/download/pdf/{report_id}
Download the PDF report.

### GET /api/download/json/{report_id}
Download the raw JSON analysis.
