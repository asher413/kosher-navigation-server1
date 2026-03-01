import os
import asyncio
import logging
from typing import List, Dict, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile, File
from pydantic import BaseModel
import httpx
import yt_dlp
import speech_recognition as sr

# --------------------------------------------------
# Logging
# --------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

# --------------------------------------------------
# Lifespan (Modern FastAPI)
# --------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.async_client = httpx.AsyncClient(timeout=15.0)
    logger.info("AsyncClient started")
    yield
    await app.state.async_client.aclose()
    logger.info("AsyncClient closed")

app = FastAPI(title="Advanced Audio Search API", lifespan=lifespan)

# --------------------------------------------------
# Config (Environment Variables)
# --------------------------------------------------

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"

if not YOUTUBE_API_KEY:
    logger.warning("YOUTUBE_API_KEY not set!")

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
    if len(text) <= limit:
        return text
    return text[:limit] + "... (הטקסט קוצר)"

def is_safe(text: str) -> bool:
    forbidden = ["xxx", "badword"]
    lowered = text.lower()
    return not any(word in lowered for word in forbidden)

# --------------------------------------------------
# YouTube Search
# --------------------------------------------------

async def search_youtube(query: str) -> List[Dict]:
    params = {
        "part": "snippet",
        "q": query,
        "key": YOUTUBE_API_KEY,
        "maxResults": 20,
        "type": "video"
    }

    try:
        response = await app.state.async_client.get(YOUTUBE_SEARCH_URL, params=params)
        response.raise_for_status()
        data = response.json()

        results = []
        for item in data.get("items", []):
            results.append({
                "title": item["snippet"]["title"],
                "video_id": item["id"]["videoId"]
            })

        return results

    except Exception as e:
        logger.error(f"YouTube search failed: {e}")
        return []

# --------------------------------------------------
# yt_dlp Extractor (Non-blocking)
# --------------------------------------------------

async def extract_audio_info(video_id: str):
    loop = asyncio.get_event_loop()
    url = f"https://www.youtube.com/watch?v={video_id}"

    def run():
        ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "nocheckcertificate": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        return await loop.run_in_executor(None, run)
    except Exception as e:
        logger.error(f"Extraction error: {e}")
        return {"error": str(e)}

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

    first = results[0]
    others = results[1:]

    message = f"נמצא: {first['title']}."
    if others:
        message += " להשמעת שאר התוצאות הקש 2."

    return SearchResponse(
        message=message,
        results=results
    )

@app.get("/search/more")
async def search_more(query: str):
    results = await search_youtube(query)
    return {"results": results[1:]}

@app.get("/play")
async def play(video_id: str):
    info = await extract_audio_info(video_id)

    if "error" in info:
        raise HTTPException(status_code=500, detail=info["error"])

    return {
        "title": info.get("title"),
        "audio_url": info.get("url")
    }

@app.post("/chat")
async def chat(request: ChatRequest):
    if not is_safe(request.text):
        raise HTTPException(status_code=400, detail="תוכן לא תקין")

    response_text = f"תגובה עבור: {request.text}"
    return {"response": smart_trim(response_text)}

# --------------------------------------------------
# Speech To Text (Cloud Compatible)
# --------------------------------------------------

recognizer = sr.Recognizer()

@app.post("/speech-to-text")
async def speech_to_text(file: UploadFile = File(...)):
    try:
        audio_bytes = await file.read()

        with open("temp.wav", "wb") as f:
            f.write(audio_bytes)

        with sr.AudioFile("temp.wav") as source:
            audio = recognizer.record(source)

        text = recognizer.recognize_google(audio, language="he-IL")
        return {"text": text}

    except Exception as e:
        logger.error(f"Speech error: {e}")
        return {"error": str(e)}
