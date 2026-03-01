import os
import asyncio
import logging
import uuid
import time
from typing import List, Dict, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile, File, Request
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
        except Exception as e:
            if attempt == retries - 1:
                raise
            await asyncio.sleep(2 ** attempt)

# --------------------------------------------------
# Middleware (Debug + Rate Limit)
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
# YouTube Search
# --------------------------------------------------

async def search_youtube(query: str) -> List[Dict]:
    if not YOUTUBE_API_KEY:
        logger.warning("YouTube API key missing")
        return []

    cache_key = f"yt:{query}"
    cached = get_cache(cache_key)
    if cached:
        return cached

    async def call():
        params = {
            "part": "snippet",
            "q": query,
            "key": YOUTUBE_API_KEY,
            "maxResults": 20,
            "type": "video"
        }
        r = await app.state.async_client.get(YOUTUBE_SEARCH_URL, params=params)
        r.raise_for_status()
        data = r.json()
        return [
            {
                "title": item["snippet"]["title"],
                "video_id": item["id"]["videoId"]
            }
            for item in data.get("items", [])
        ]

    results = await retry_request(call)
    set_cache(cache_key, results)
    return results

# --------------------------------------------------
# yt_dlp Extractor
# --------------------------------------------------

async def extract_audio_info(video_id: str):
    loop = asyncio.get_event_loop()
    url = f"https://www.youtube.com/watch?v={video_id}"

    def run():
        with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        return await loop.run_in_executor(None, run)
    except Exception as e:
        logger.error(f"Extraction error: {e}")
        return {"error": str(e)}

# --------------------------------------------------
# IVR Endpoint (Fix 404)
# --------------------------------------------------

@app.get("/ivr", response_class=PlainTextResponse)
async def ivr_handler(request: Request):
    params = request.query_params
    logger.info(f"IVR params: {dict(params)}")

    path = params.get("path", "")

    if path == "waze":
        return "ניווט מופעל"
    elif path == "search":
        return "חיפוש מופעל"
    else:
        return "תפריט ראשי"

# --------------------------------------------------
# Endpoints
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
async def text_to_speech(text: str, lang: str = "he"):
    filename = f"/tmp/{uuid.uuid4()}.mp3"
    try:
        tts = gTTS(text=text, lang=lang)
        tts.save(filename)
        return FileResponse(filename, media_type="audio/mpeg")
    finally:
        if os.path.exists(filename):
            os.remove(filename)

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
