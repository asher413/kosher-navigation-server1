import asyncio
import logging
import uuid
import time
import os
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
# Utilities
# --------------------------------------------------

def smart_trim(text: str, limit: int = 400) -> str:
    if not text:
        return ""
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

@app.get("/")
async def health_check():
    return {"status": "ok"}

@app.head("/")
async def health_check_head():
    return {}

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
# Security & Rate Limit
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
# Cache
# --------------------------------------------------

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
            return PlainTextResponse(
                "id_list_message=t-עברת את מכסת הבקשות נסה שוב מאוחר יותר"
            )
        raise e

    try:
        response = await call_next(request)
        return response

    except Exception as e:

        logger.error(f"Unhandled error: {e}")

        if "/ivr" in str(request.url):
            return PlainTextResponse(
                "id_list_message=t-אירעה שגיאה במערכת אנא נסו שוב"
            )

        return JSONResponse(
            status_code=500,
            content={"error": "server error"}
        )

# --------------------------------------------------
# YouTube
# --------------------------------------------------

async def search_youtube(query: str):

    if not query:
        return None

    cache_key = f"yt_{query}"
    cached = get_cache(cache_key)

    if cached:
        return cached

    ydl_opts = {
        'quiet': True,
        'noplaylist': True,
        'extract_flat': True
    }

    try:

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:

            loop = asyncio.get_event_loop()

            info = await loop.run_in_executor(
                None,
                lambda: ydl.extract_info(
                    f"ytsearch1:{query}",
                    download=False
                )
            )

            if 'entries' in info and info['entries']:

                result = [{
                    'title': info['entries'][0]['title'],
                    'video_id': info['entries'][0]['id']
                }]

                set_cache(cache_key, result)

                return result

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
                'player_client': ['ios', 'android', 'mweb'],
                'skip': ['webpage', 'hls']
            }
        },
        'http_headers': {
            'User-Agent': 'com.google.android.youtube/19.29.37'
        }
    }

    try:

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:

            loop = asyncio.get_event_loop()

            url = f"https://www.youtube.com/watch?v={video_id}"

            info = await loop.run_in_executor(
                None,
                lambda: ydl.extract_info(url, download=False)
            )

            return {
                'url': info['url'],
                'title': info['title']
            }

    except Exception as e:

        logger.error(f"Audio extraction error: {e}")

        return {"error": str(e)}

# --------------------------------------------------
# IVR MENU
# --------------------------------------------------
@app.get("/ivr", response_class=PlainTextResponse)
async def ivr(
    request: Request,
    ApiCallId: str = "",
    ApiPhone: str = "",
    ApiExtension: str = "",
    DTMF: str = None,
    search_query: str = None,
    hangup: str = ""
):
    params = request.query_params
    mode = params.get("mode")
    dtmf_input = mode or ApiExtension or DTMF or params.get("data")

    if not dtmf_input or dtmf_input == "%val%":
        dtmf_input = None

    if not search_query or search_query == "%val%":
        search_query = None

    if hangup == "yes":
        logger.info(f"שיחה הסתיימה {ApiPhone}")
        return ""

    logger.info(f"טלפון={ApiPhone} מקש={dtmf_input} חיפוש={search_query}")

    # --------------------------------------------------
    # תפריט ראשי
    # --------------------------------------------------
    if dtmf_input is None and search_query is None:
        content = (
            "read=t-שלום "
            "לניווט הקש 1 "
            "למוביט הקש 2 "
            "לחיפוש ביוטיוב הקש 3 "
            "לספוטיפיי הקש 4 "
            "לבינה מלאכותית הקש 5"
            "=data,yes,1,1,1,Digits,no"
        )
        return PlainTextResponse(content)

    logger.info(f"Mode={mode}, DTMF={dtmf_input}")

    # --------------------------------------------------
    # תת תפריט יוטיוב
    # --------------------------------------------------
    if dtmf_input == "3" and search_query is None:
        return PlainTextResponse(
            "read=t-להשמעת שירים חדשים הקש 1 "
            "לחיפוש קולי הקש 2"
            "=mode,yes,1,1,1,Digits,no"
        )

    logger.info(f"Mode={mode}, DTMF={dtmf_input}")

    # --------------------------------------------------
    # שירים חדשים
    # --------------------------------------------------
    results = []  # משתנה results מוכן לשימוש
    if mode == "3" and dtmf_input == "1":
        results = await search_youtube("שירים חדשים 2025")
        if not results or not results[0]:
            logger.warning("No YouTube results found for 'שירים חדשים 2025'")
            return PlainTextResponse("id_list_message=t-לא נמצאו שירים חדשים.")

        info = await extract_audio_info(results[0]['video_id'])
        if info and "url" in info:
            return PlainTextResponse(f"playfile={info['url']}")

        return PlainTextResponse("id_list_message=t-לא נמצאו שירים חדשים")

    logger.info(f"Mode={mode}, DTMF={dtmf_input}")

    # --------------------------------------------------
    # חיפוש קולי יוטיוב
    # --------------------------------------------------
    if mode == "3" and dtmf_input == "2":
        return PlainTextResponse(
            "read=t-נא אמרו את שם השיר לאחר הצליל "
            "record=/speech-to-text?mode=ytvoice,5,0,beep"
        )

    logger.info(f"Mode={mode}, DTMF={dtmf_input}")

    # --------------------------------------------------
    # בקשת הקלטה
    # --------------------------------------------------
    if dtmf_input and search_query is None:
        prompts = {
            "2": "נא אמרו יעד לנסיעה",
            "3": "נא אמרו שם שיר ליוטיוב",
            "4": "נא אמרו שם שיר לספוטיפיי",
            "5": "נא אמרו שאלה לבינה המלאכותית"
        }
        prompt_text = prompts.get(dtmf_input)
        if prompt_text:
            return PlainTextResponse(
                f"read=t-{prompt_text}."
                f"record=/speech-to-text?mode={dtmf_input},5,0,beep"
            )

    logger.info(f"Mode={mode}, DTMF={dtmf_input}")

    # --------------------------------------------------
    # תוצאה מחיפוש קולי
    # --------------------------------------------------
    if search_query:
        if mode == "ytvoice":
            results = await search_youtube(search_query)
            if results:
                info = await extract_audio_info(results[0]['video_id'])
                if info and "url" in info:
                    return PlainTextResponse(f"playfile={info['url']}")
            return PlainTextResponse("id_list_message=t-לא נמצאה תוצאה")

        elif dtmf_input == "2":
            if not gmaps:
                return PlainTextResponse("id_list_message=t-שירות המיקום לא מוגדר")

            loop = asyncio.get_event_loop()
            places = await loop.run_in_executor(
                None,
                lambda: gmaps.places(query=search_query)
            )
            results = places.get("results", [])
            if results:
                name = results[0].get("name")
                address = results[0].get("formatted_address")
                return PlainTextResponse(f"id_list_message=t-מצאתי את {name} בכתובת {address}")

            return PlainTextResponse("id_list_message=t-לא מצאתי את המקום")

        elif dtmf_input == "5":
            ai_response = smart_trim(f"תשובת המערכת עבור {search_query}")
            return PlainTextResponse(f"id_list_message=t-{ai_response}")

    return PlainTextResponse("id_list_message=t-חזרה לתפריט הראשי")
# --------------------------------------------------
# Speech To Text
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

        return PlainTextResponse(
            f"go_to=/ivr?mode={mode}&search_query={text}"
        )

    finally:

        if os.path.exists(unique_filename):
            os.remove(unique_filename)

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

if __name__ == "__main__":

    import uvicorn

    port = int(os.environ.get("PORT", 10000))

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port
    )
