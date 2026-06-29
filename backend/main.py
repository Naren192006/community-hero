from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dotenv import load_dotenv
from pathlib import Path
from google import genai
from google.genai import types
from pydantic import BaseModel
import os, uuid, json
from PIL import Image
import io
import database

load_dotenv(Path(__file__).parent.parent / ".env")

app = FastAPI()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

app.mount("/static", StaticFiles(directory="../frontend"), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class SignupModel(BaseModel):
    name: str
    email: str
    password: str


class LoginModel(BaseModel):
    email: str
    password: str


@app.get("/")
def root():
    return {"status": "Community Hero API is running!"}


@app.get("/app")
def serve_frontend():
    return FileResponse("../frontend/index.html")


@app.get("/dashboard")
def serve_dashboard():
    return FileResponse("../frontend/dashboard.html")


@app.get("/map")
def serve_map():
    return FileResponse("../frontend/map.html")


@app.get("/profile")
def serve_profile():
    return FileResponse("../frontend/profile.html")


@app.post("/signup")
def signup(data: SignupModel):
    user, error = database.create_user(data.email, data.password, data.name)
    if error:
        raise HTTPException(status_code=400, detail=error)
    return {"message": "Account created!", "name": user["name"], "email": user["email"]}


@app.post("/login")
def login(data: LoginModel):
    user = database.get_user(data.email, data.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return {"message": "Login successful!", "name": user["name"], "email": user["email"]}


@app.post("/report")
async def create_report(
    photo: UploadFile = File(...),
    location: str = Form(...),
    category: str = Form("Unknown"),
    description: str = Form(""),
    user_email: str = Form(...)
):
    # Require login: reject if no email was sent, or if it doesn't
    # belong to a registered user. This stops anonymous/spoofed
    # submissions rather than just defaulting to "anonymous".
    user_email = (user_email or "").strip()
    if not user_email:
        raise HTTPException(status_code=401, detail="You must be logged in to submit a report.")
    if not database.user_exists(user_email):
        raise HTTPException(status_code=401, detail="Invalid session. Please log in again.")

    # Check for duplicate first
    duplicate = database.check_duplicate(location, category)
    if duplicate:
        return {
            "duplicate": True,
            "message": "Similar issue already reported at this location!",
            "existing_report": duplicate
        }

    # Read and save photo
    photo_bytes = await photo.read()
    image = Image.open(io.BytesIO(photo_bytes))
    report_id = "CH-" + str(uuid.uuid4())[:6].upper()

    reports_dir = Path(__file__).parent / "reports"
    reports_dir.mkdir(exist_ok=True)
    image.save(reports_dir / f"{report_id}.jpg")

    # Convert image for Gemini
    img_byte_arr = io.BytesIO()
    image.save(img_byte_arr, format='JPEG')
    img_byte_arr = img_byte_arr.getvalue()

    # Build prompt
    prompt = f"""
    You are an AI assistant for a civic issue reporting platform.
    Analyze this image and the details below, then respond ONLY in valid JSON.

    Location: {location}
    Category hint: {category}
    Description: {description}

    Respond with exactly this JSON format:
    {{
        "issue_type": "specific issue name",
        "severity": "High/Medium/Low",
        "estimated_resolution": "X-Y days",
        "summary": "2-3 sentence description of the issue and why it needs attention",
        "complaint": "A formal complaint letter to the municipal authority"
    }}
    """

    # Send to Gemini
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_bytes(data=img_byte_arr, mime_type="image/jpeg"),
                prompt
            ]
        )
        raw = (response.text or "").strip()
        raw = raw.replace("```json", "").replace("```", "").strip()

        if not raw:
            raise ValueError("Empty response from Gemini")

        result = json.loads(raw)

    except Exception as e:
        print(f"Gemini error: {e}")
        result = {
            "issue_type": category,
            "severity": "Medium",
            "estimated_resolution": "3-7 days",
            "summary": "AI analysis temporarily unavailable. Report has been saved successfully.",
            "complaint": f"To,\nThe Municipal Authority\n\nSub: Report of {category} at {location}\n\nRespected Sir/Madam,\n\nI wish to report a {category} issue at {location}. Kindly inspect and resolve at the earliest.\n\nSincerely,\nA Concerned Citizen"
        }

    result["report_id"] = report_id
    result["location"] = location
    result["duplicate"] = False

    database.save_report(result, user_email)

    return result


@app.get("/reports")
def get_reports():
    return database.get_all_reports()


@app.post("/upvote/{report_id}")
def upvote(report_id: str):
    report = database.upvote_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report