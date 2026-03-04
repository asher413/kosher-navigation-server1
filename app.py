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
# Logging & Utilities
# --------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

def smart_trim(text: str, limit: int = 400) -> str:
    if not text: return ""
    return text if len(text) <= limit else text[:limit] + "... (הטקסט קוצר)"

# --------------------------------------------------
# Lifespan
# --------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.async_client = httpx.AsyncClient(timeout=30.0)
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
# Helpers (Bypass logic added here)
# --------------------------------------------------

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

# --------------------------------------------------
# YouTube Search & Extraction (Fixed for blocks)
# --------------------------------------------------

async def search_youtube(query: str):
    # שימוש בלקוחות מובייל כדי למנוע חסימות 403
    ydl_opts = {
        'quiet': True, 
        'noplaylist': True, 
        'extract_flat': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['android_vr', 'ios', 'mweb'],
                'player_skip': ['webpage', 'hls']
            }
        }
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(f"ytsearch1:{query}", download=False))
            if 'entries' in info and info['entries']:
                return [{'title': info['entries'][0]['title'], 'video_id': info['entries'][0]['id']}]
        except Exception as e:
            logger.error(f"Youtube search error: {e}")
    return None

async def extract_audio_info(video_id: str):
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['android_vr', 'ios', 'mweb'],
                'player_skip': ['webpage', 'hls']
            }
        },
        'http_headers': {
            'User-Agent': 'com.google.android.youtube/19.29.37 (Linux; U; Android 11) gzip',
        }
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            loop = asyncio.get_event_loop()
            url = f"https://www.youtube.com/watch?v={video_id}"
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
            return {'url': info['url'], 'title': info['title']}
        except Exception as e:
            logger.error(f"Audio extraction error: {e}")
            return {"error": str(e)}

# --------------------------------------------------
# IVR Menu (Updated with Voice and Navigation)
# --------------------------------------------------

@app.get("/ivr", response_class=PlainTextResponse)
async def ivr(
    request: Request,
    ApiCallId: str = "",
    ApiPhone: str = "",
    ApiExtension: str = "",
    DTMF: str = None,
    search_query: str = None,
    mode: str = None,
    hangup: str = ""
):
    params = request.query_params
    # זיהוי המצב הנוכחי מה-URL או מהקשות המשתמש
    dtmf_input = mode or ApiExtension or DTMF or params.get("data")

    if not dtmf_input or dtmf_input == "%val%": dtmf_input = None
    if not search_query or search_query == "%val%": search_query = None

    if hangup == "yes": return ""

    # --- שלב 1: תפריט ראשי ---
    if dtmf_input is None and search_query is None:
        content = (
            "read=t-שלום. "
            "לניווט בהליכה הקש 1. "
            "למוביט ותחבורה ציבורית הקש 2. "
            "ליוטיוב הקש 3. "
            "לבינה מלאכותית הקש 5.=data,yes,1,1,1,Digits,no"
        )
        return PlainTextResponse(content=content)

    # --- שלוחה 1: ניווט רגלי (חיפוש קולי) ---
    if dtmf_input == "1" and search_query is None:
        return PlainTextResponse("read=t-נא אמרו יעד לניווט רגלי לאחר הצליל. record=/speech-to-text?mode=walk,5,0,beep")

    # --- שלוחה 2: מוביט/אוטובוסים (חיפוש קולי) ---
    if dtmf_input == "2" and search_query is None:
        return PlainTextResponse("read=t-נא אמרו יעד או קו אוטובוס לאחר הצליל. record=/speech-to-text?mode=bus,5,0,beep")

    # --- שלוחה 3: יוטיוב (תת תפריט) ---
    if dtmf_input == "3" and search_query is None:
        return PlainTextResponse(
            "read=t-ליוטיוב: להשמעת שירים חדשים הקש 1. לחיפוש קולי הקש 2.=mode_yt,yes,1,1,1,Digits,no"
        )

    # טיפול בתת-תפריט יוטיוב
    if dtmf_input == "mode_yt":
        sub_choice = params.get("data")
        if sub_choice == "1":
            results = await search_youtube("שירים חדשים 2025")
            if results:
                info = await extract_audio_info(results[0]['video_id'])
                return PlainTextResponse(f"playfile={info['url']}")
            return PlainTextResponse("id_list_message=t-לא נמצאו שירים חדשים.")
        elif sub_choice == "2":
            return PlainTextResponse("read=t-נא אמרו את שם השיר לחיפוש. record=/speech-to-text?mode=ytvoice,5,0,beep")

    # --- שלב 3: ביצוע פעולות על בסיס חיפוש קולי ---
    if search_query:
        if dtmf_input == "walk":
            # לוגיקת גוגל מפות לניווט רגלי
            if gmaps:
                # כאן ניתן להוסיף שליחת הוראות SMS או הקראה קולית של המסלול
                return PlainTextResponse(f"id_list_message=t-מחשב מסלול רגלי אל {search_query}.")
            return PlainTextResponse("id_list_message=t-שירות הניווט אינו זמין.")

        elif dtmf_input == "bus":
            # לוגיקת תחבורה ציבורית
            return PlainTextResponse(f"id_list_message=t-בודק קווי אוטובוס אל {search_query}.")

        elif dtmf_input == "ytvoice":
            results = await search_youtube(search_query)
            if results:
                info = await extract_audio_info(results[0]['video_id'])
                if "url" in info:
                    return PlainTextResponse(f"playfile={info['url']}")
            return PlainTextResponse("id_list_message=t-לא נמצאו תוצאות ביוטיוב.")

    return PlainTextResponse("id_list_message=t-חזרה לתפריט הראשי.")

# --------------------------------------------------
# Speech To Text & Standard Endpoints
# --------------------------------------------------

recognizer = sr.Recognizer()

@app.post("/speech-to-text")
async def speech_to_text(request: Request, file: UploadFile = File(...)):
    unique_filename = f"/tmp/{uuid.uuid4()}.wav"
    try:
        audio_bytes = await file.read()
        with open(unique_filename, "wb") as f:
            f.write(audio_bytes)

        with sr.AudioFile(unique_filename) as source:
            audio = recognizer.record(source)

        text = recognizer.recognize_google(audio, language="he-IL")
        mode = request.query_params.get("mode")
        # העברה חזרה ל-IVR עם הטקסט שפוענח
        return PlainTextResponse(f"go_to=/ivr?mode={mode}&search_query={text}")
    except Exception:
        return PlainTextResponse("id_list_message=t-הדיבור לא הובן, נסה שוב.")
    finally:
        if os.path.exists(unique_filename):
            os.remove(unique_filename)

# (שאר הפונקציות הקיימות נשארות ללא שינוי)
@app.middleware("http")
async def global_middleware(request: Request, call_next):
    client_ip = request.client.host if request.client else "unknown"
    try:
        rate_limit(client_ip)
    except HTTPException as e:
        if "/ivr" in str(request.url):
            return PlainTextResponse("id_list_message=t-עברת את מכסת הבקשות")
        raise e
    try:
        return await call_next(request)
    except Exception as e:
        if "/ivr" in str(request.url):
            return PlainTextResponse("id_list_message=t-חלה שגיאה במערכת")
        return JSONResponse(status_code=500, content={"error": str(e)})

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
