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

def smart_trim(text, limit=100):
    if not text: return ""
    return text[:limit] + "..." if len(text) > limit else text

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
    app.state.async_client = httpx.AsyncClient(timeout=30.0)
    app.state.cache = {}
    app.state.rate_limit = {}
    logger.info("AsyncClient started")
    yield
    await app.state.async_client.aclose()
    logger.info("AsyncClient closed")

app = FastAPI(title="Advanced Audio Search API", lifespan=lifespan)

@app.get("/")
async def health_check():
    return {"status": "ok"}

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
# Utilities (נשמרו בדיוק כפי שהיו)
# --------------------------------------------------

def smart_trim_v2(text: str, limit: int = 400) -> str:
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
    client_ip = request.client.host if request.client else "unknown"
    
    try:
        rate_limit(client_ip)
    except HTTPException as e:
        if "/ivr" in str(request.url):
            return PlainTextResponse("id_list_message=t-עברת את מכסת הבקשות")
        raise e

    try:
        response = await call_next(request)
        return response
    except Exception as e:
        logger.error(f"Unhandled error: {e}")
        if "/ivr" in str(request.url):
            return PlainTextResponse("id_list_message=t-חלה שגיאה במערכת")
        return JSONResponse(status_code=500, content={"error": "Server error"})

# --------------------------------------------------
# YouTube & Audio - פתרון חסינות (Anti-Block)
# --------------------------------------------------

async def search_youtube(query: str):
    ydl_opts = {
        'quiet': True, 
        'noplaylist': True, 
        'extract_flat': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'mweb'],
                'skip': ['webpage', 'hls']
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
                'player_client': ['android', 'ios', 'mweb'],
                'skip': ['webpage', 'hls']
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
# IVR MENU - התפריט החדש (חיפוש קולי וניווט)
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
    dtmf_input = mode or ApiExtension or DTMF or params.get("data")

    # ניקוי ערכים ריקים מימות המשיח
    if not dtmf_input or dtmf_input == "%val%": dtmf_input = None
    if not search_query or search_query == "%val%": search_query = None

    if hangup == "yes": return ""

    # --- תפריט ראשי ---
    if dtmf_input is None and search_query is None:
        content = (
            "read=t-ברוכים הבאים. "
            "לניווט בהליכה הקש 1. "
            "למוביט וקווי אוטובוס הקש 2. "
            "ליוטיוב הקש 3. "
            "לספוטיפיי הקש 4. "
            "לבינה מלאכותית הקש 5.=data,yes,1,1,1,Digits,no"
        )
        return PlainTextResponse(content=content)

    # --- טיפול בשלוחות (חיפוש קולי) ---
    
    # שלוחה 1: ניווט רגלי
    if dtmf_input == "1" and search_query is None:
        return PlainTextResponse("read=t-נא אמרו יעד להליכה לאחר הצליל. record=/speech-to-text?mode=walk,5,0,beep")

    # שלוחה 2: מוביט
    if dtmf_input == "2" and search_query is None:
        return PlainTextResponse("read=t-נא אמרו קו אוטובוס או יעד לאחר הצליל. record=/speech-to-text?mode=moovit,5,0,beep")

    # שלוחה 3: יוטיוב (תת תפריט)
    if dtmf_input == "3" and search_query is None:
        return PlainTextResponse("read=t-ליוטיוב: לשירים חדשים הקש 1. לחיפוש קולי הקש 2.=yt_sub,yes,1,1,1,Digits,no")

    if dtmf_input == "yt_sub":
        sub = params.get("data")
        if sub == "1": # שירים חדשים
            results = await search_youtube("שירים חדשים 2026")
            if results:
                info = await extract_audio_info(results[0]['video_id'])
                return PlainTextResponse(f"playfile={info['url']}")
            return PlainTextResponse("id_list_message=t-לא נמצאו שירים חדשים.")
        elif sub == "2": # חיפוש קולי
            return PlainTextResponse("read=t-נא אמרו שם שיר לאחר הצליל. record=/speech-to-text?mode=yt_voice,5,0,beep")

    # שלוחה 4 ו-5: ספוטיפיי ובינה מלאכותית
    if dtmf_input in ["4", "5"] and search_query is None:
        p = "נא אמרו שם שיר לספוטיפיי" if dtmf_input == "4" else "נא אמרו שאלה לבינה המלאכותית"
        return PlainTextResponse(f"read=t-{p}. record=/speech-to-text?mode={dtmf_input},5,0,beep")

    # --- עיבוד תוצאות חיפוש קולי ---
    if search_query:
        if dtmf_input == "yt_voice":
            res = await search_youtube(search_query)
            if res:
                info = await extract_audio_info(res[0]['video_id'])
                return PlainTextResponse(f"playfile={info['url']}")
            return PlainTextResponse("id_list_message=t-לא נמצאו תוצאות.")

        elif dtmf_input == "walk":
            if gmaps:
                # כאן אפשר להרחיב לניווט אמת
                return PlainTextResponse(f"id_list_message=t-מחשב מסלול הליכה אל {search_query}.")
            return PlainTextResponse("id_list_message=t-שירות המפות לא פעיל.")

        elif dtmf_input == "moovit":
            return PlainTextResponse(f"id_list_message=t-בודק אוטובוסים אל {search_query} במוביט.")

        elif dtmf_input == "5":
            ai_text = smart_trim(f"תשובה עבור {search_query}: השירות בבדיקה.")
            return PlainTextResponse(f"id_list_message=t-{ai_text}")

    return PlainTextResponse("id_list_message=t-חזרה לתפריט הראשי.")

# --------------------------------------------------
# Standard Endpoints (נשארו כפי שהיו)
# --------------------------------------------------

@app.get("/health")
async def health(): return {"status": "ok"}

@app.get("/search", response_model=SearchResponse)
async def search(query: str):
    if not is_safe(query): raise HTTPException(status_code=400, detail="תוכן לא תקין")
    results = await search_youtube(query)
    if not results: return SearchResponse(message="לא נמצאו תוצאות")
    return SearchResponse(message=f"נמצא: {results[0]['title']}", results=results)

@app.get("/play")
async def play(video_id: str):
    info = await extract_audio_info(video_id)
    if "error" in info: raise HTTPException(status_code=500, detail=info["error"])
    return {"title": info.get("title"), "audio_url": info.get("url")}

@app.post("/chat")
async def chat(request: ChatRequest):
    if not is_safe(request.text): raise HTTPException(status_code=400, detail="תוכן לא תקין")
    return {"response": smart_trim(f"תגובה עבור: {request.text}")}

@app.get("/tts")
async def text_to_speech(text: str, background_tasks: BackgroundTasks, lang: str = "he"):
    filename = f"/tmp/{uuid.uuid4()}.mp3"
    tts = gTTS(text=text, lang=lang)
    tts.save(filename)
    background_tasks.add_task(os.remove, filename)
    return FileResponse(filename, media_type="audio/mpeg", background=background_tasks)

# --------------------------------------------------
# Speech To Text (STT)
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
        # חזרה ל-IVR עם הטקסט
        return PlainTextResponse(f"go_to=/ivr?mode={mode}&search_query={text}")
    except Exception as e:
        logger.error(f"STT Error: {e}")
        return PlainTextResponse("id_list_message=t-הדיבור לא הובן, נסו שוב.")
    finally:
        if os.path.exists(unique_filename): os.remove(unique_filename)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
