import os
import asyncio
import logging
import uuid
import time
from typing import List, Dict, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile, File, Request, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel
import httpx
import yt_dlp
import speech_recognition as sr
import googlemaps
from gtts import gTTS

# --------------------------------------------------
# Logging
# --------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

# --------------------------------------------------
# Lifespan
# --------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.async_client = httpx.AsyncClient(timeout=15.0)
    app.state.cache = {}
    app.state.rate_limit = {}
    logger.info("AsyncClient started")
    yield
    await app.state.async_client.aclose()
    logger.info("AsyncClient closed")

app = FastAPI(title="Advanced Audio Search API", lifespan=lifespan)

# --------------------------------------------------
# Config
# --------------------------------------------------

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"

gmaps = googlemaps.Client(key=MAPS_API_KEY) if MAPS_API_KEY else None

# --------------------------------------------------
# Models
# --------------------------------------------------

class SearchResponse(BaseModel):
    message: str
    results: Optional[List[Dict]] = None

class ChatRequest(BaseModel):
    text: str

# --------------------------------------------------
# Utilities
# --------------------------------------------------

def smart_trim(text: str, limit: int = 400) -> str:
    return text if len(text) <= limit else text[:limit] + "... (הטקסט קוצר)"

def is_safe(text: str) -> bool:
    forbidden = ["xxx", "badword"]
    lowered = text.lower()
    return not any(word in lowered for word in forbidden)

def rate_limit(ip: str, limit: int = 30, window: int = 60):
    now = time.time()
    records = app.state.rate_limit.setdefault(ip, [])
    records[:] = [t for t in records if now - t < window]
    if len(records) >= limit:
        raise HTTPException(status_code=429, detail="Too many requests")
    records.append(now)

def get_cache(key):
    item = app.state.cache.get(key)
    if not item:
        return None
    data, timestamp = item
    if time.time() - timestamp > 60:
        del app.state.cache[key]
        return None
    return data

def set_cache(key, value):
    app.state.cache[key] = (value, time.time())

async def retry_request(func, retries=3):
    for attempt in range(retries):
        try:
            return await func()
        except Exception:
            if attempt == retries - 1:
                raise
            await asyncio.sleep(2 ** attempt)

# --------------------------------------------------
# Middleware
# --------------------------------------------------

@app.middleware("http")
async def global_middleware(request: Request, call_next):
    logger.info(f"Incoming request: {request.url}")
    rate_limit(request.client.host if request.client else "unknown")
    try:
        response = await call_next(request)
        return response
    except Exception as e:
        logger.error(f"Unhandled error: {e}")
        return JSONResponse(status_code=500, content={"error": "Server error"})

# --------------------------------------------------
# IVR MENU (UPDATED WITH KEYPAD NAVIGATION)
# --------------------------------------------------

@app.get("/ivr", response_class=PlainTextResponse)
async def ivr(
    ApiCallId: str = "",
    ApiPhone: str = "",
    ApiExtension: str = "",
    path: str = "",
    DTMF: str = "",
    search_query: str = None,  # מקבל את הטקסט מהקלטת הקול
    hangup: str = ""
):
    logger.info(f"IVR call | phone={ApiPhone} | path={path} | dtmf={DTMF} | query={search_query}")

    if hangup == "yes":
        return ""

    # שלב 1: אם המשתמש עוד לא הקיש כלום ולא חיפש כלום - נשמיע תפריט
    if not DTMF and not search_query:
        return (
            "read=t-ברוכים הבאים למערכת. "
            "לניווט הקש 1. "
            "למוביט הקש 2. "
            "ליוטיוב הקש 3. "
            "לספוטיפיי הקש 4. "
            "לבינה מלאכותית הקש 5.=DTMF,yes,1,1,1,Digits,no"
        )

    # שלב 2: טיפול בבחירות התפריט (לפני חיפוש קולי)
    if DTMF and not search_query:
        if DTMF == "1":
            return "id_list_message=t-מערכת ניווט הופעלה."
        
        elif DTMF == "2":
            return "read=t-נא אמרו יעד למוביט=search_query,no,he,1,5,7"
        
        elif DTMF == "3":
            return "read=t-נא אמרו את שם השיר לחיפוש ביוטיוב=search_query,no,he,1,5,7"
        
        elif DTMF == "4":
            return "read=t-נא אמרו את שם השיר לחיפוש בספוטיפיי=search_query,no,he,1,5,7"
        
        elif DTMF == "5":
            return "id_list_message=t-מערכת בינה מלאכותית הופעלה."
        
        else:
            return "id_list_message=t-בחירה לא תקינה. להתראות."

    # שלב 3: טיפול בתוצאות החיפוש הקולי (אחרי שהמשתמש דיבר)
    if search_query:
        if DTMF == "3":  # חיפוש ביוטיוב
            results = await search_youtube(search_query)
            if results:
                first_title = results[0]['title']
                return f"id_list_message=t-מצאתי ביוטיוב את: {first_title}. מיד נשמיע."
            return "id_list_message=t-לא נמצאו תוצאות ביוטיוב."

        elif DTMF == "4":  # חיפוש בספוטיפיי (כאן תבוא הלוגיקה של ספוטיפיי בעתיד)
            return f"id_list_message=t-מחפש בספוטיפיי את {search_query}. השירות בבנייה."

        elif DTMF == "2":  # חיפוש במוביט/מיקום
            return f"id_list_message=t-מחפש דרכי הגעה אל {search_query}."

    return "id_list_message=t-תקלה במערכת."

# --------------------------------------------------
# Standard Endpoints
# --------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/search", response_model=SearchResponse)
async def search(query: str):
    if not is_safe(query):
        raise HTTPException(status_code=400, detail="תוכן לא תקין")

    results = await search_youtube(query)

    if not results:
        return SearchResponse(message="לא נמצאו תוצאות")

    message = f"נמצא: {results[0]['title']}."
    if len(results) > 1:
        message += " להשמעת שאר התוצאות הקש 2."

    return SearchResponse(message=message, results=results)

@app.get("/search/more")
async def search_more(query: str):
    return {"results": (await search_youtube(query))[1:]}

@app.get("/play")
async def play(video_id: str):
    info = await extract_audio_info(video_id)
    if "error" in info:
        raise HTTPException(status_code=500, detail=info["error"])
    return {"title": info.get("title"), "audio_url": info.get("url")}

@app.post("/chat")
async def chat(request: ChatRequest):
    if not is_safe(request.text):
        raise HTTPException(status_code=400, detail="תוכן לא תקין")
    return {"response": smart_trim(f"תגובה עבור: {request.text}")}

# --------------------------------------------------
# TTS
# --------------------------------------------------

@app.get("/tts")
async def text_to_speech(text: str, background_tasks: BackgroundTasks, lang: str = "he"):
    filename = f"/tmp/{uuid.uuid4()}.mp3"
    tts = gTTS(text=text, lang=lang)
    tts.save(filename)

    background_tasks.add_task(os.remove, filename)

    return FileResponse(
        filename,
        media_type="audio/mpeg",
        background=background_tasks
    )

# --------------------------------------------------
# Maps
# --------------------------------------------------

@app.get("/location/search")
async def find_place(query: str):
    if not gmaps:
        raise HTTPException(status_code=500, detail="Google Maps API key not set")

    loop = asyncio.get_event_loop()
    places = await loop.run_in_executor(None, lambda: gmaps.places(query=query))
    return {"results": places.get("results", [])}

# --------------------------------------------------
# Speech To Text
# --------------------------------------------------

recognizer = sr.Recognizer()

@app.post("/speech-to-text")
async def speech_to_text(file: UploadFile = File(...)):
    unique_filename = f"/tmp/{uuid.uuid4()}.wav"
    try:
        audio_bytes = await file.read()
        with open(unique_filename, "wb") as f:
            f.write(audio_bytes)

        with sr.AudioFile(unique_filename) as source:
            audio = recognizer.record(source)

        text = recognizer.recognize_google(audio, language="he-IL")
        return {"text": text}
    finally:
        if os.path.exists(unique_filename):
            os.remove(unique_filename)
