from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
import requests
import yt_dlp
import uvicorn
import random
import googlemaps
from datetime import datetime

app = FastAPI()

# --- הגדרות אבטחה, סינון ומפתחות ---
GEMINI_API_KEY = "AIzaSyCG7bz2Ew0IpyQHzYX4ZqwSIXf9navfsNw"
GOOGLE_MAPS_KEY = "AIzaSyCG7bz2Ew0IpyQHzYX4ZqwSIXf9navfsNw"
gmaps = googlemaps.Client(key=GOOGLE_MAPS_KEY)

FORBIDDEN_WORDS = ["מילה1", "מילה2", "תוכן_לא_הולם"]
BLOCKED_USERS = ["0501234567"]

def is_safe(text):
    """פונקציה לבדיקת סינון תוכן"""
    if not text:
        return True
    return not any(word in text for word in FORBIDDEN_WORDS)

def get_yt_audio(query, count=1):
    """חיפוש ביוטיוב והחזרת קישור לשמיעה עם מעקף חסימה"""
    if not is_safe(query):
        return "blocked"
    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch",
        "nocheckcertificate": True,
        "ignoreerrors": True,
        "source_address": "0.0.0.0",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            search_query = f"ytsearch{count}:{query}"
            info = ydl.extract_info(search_query, download=False)
            if not info or "entries" not in info or not info["entries"]:
                return None
            if count == 1:
                entry = info["entries"][0]
                if not entry or not is_safe(entry.get("title")):
                    return "blocked"
                return entry.get("url")
            else:
                urls = [
                    e["url"]
                    for e in info["entries"]
                    if e and is_safe(e.get("title"))
                ]
                return urls
        except Exception as e:
            print(f"Youtube Error: {e}")
            return None

def get_free_navigation(origin, destination):
    """מערכת ניווט חינמית כגיבוי (OSM)"""
    try:
        base_geo = "https://nominatim.openstreetmap.org/search"
        headers = {"User-Agent": "MyIVRSystem/1.0"}
        orig_geo = requests.get(
            f"{base_geo}?q={origin}&format=json", headers=headers
        ).json()
        dest_geo = requests.get(
            f"{base_geo}?q={destination}&format=json", headers=headers
        ).json()
        if not orig_geo or not dest_geo:
            return "לא הצלחתי למצוא את הכתובת במערכת החינמית."
        osrm_url = (
            f"http://router.project-osrm.org/route/v1/driving/"
            f"{orig_geo[0]['lon']},{orig_geo[0]['lat']};"
            f"{dest_geo[0]['lon']},{dest_geo[0]['lat']}?overview=false&steps=true"
        )
        route_res = requests.get(osrm_url).json()
        steps = route_res["routes"][0]["legs"][0]["steps"]
        instructions = ["שימוש במערכת גיבוי חינמית."]
        for step in steps[:3]:
            instructions.append(
                f"בעוד {int(step['distance'])} מטרים, {step['maneuver']['instruction']}"
            )
        return ". ".join(instructions)
    except:
        return "שתי מערכות הניווט אינן זמינות כרגע."

def get_navigation(origin, destination, mode="driving"):
    """מנסה גוגל, ואם נכשל עובר לחינמי"""
    try:
        now = datetime.now()
        directions = gmaps.directions(
            origin, destination, mode=mode, departure_time=now, language="he"
        )
        if directions:
            leg = directions[0]["legs"][0]
            steps = leg["steps"]
            instructions = [
                f"מסלול גוגל מ{leg['start_address']} ל{leg['end_address']}."
            ]
            for step in steps[:4]:
                clean_instr = (
                    step["html_instructions"]
                    .replace("<b>", "")
                    .replace("</b>", "")
                    .replace('<div style="font-size:0.9em">', " ")
                    .replace("</div>", "")
                )
                instructions.append(
                    f"בעוד {step['distance']['text']}, {clean_instr}"
                )
            return ". ".join(instructions)
        else:
            return get_free_navigation(origin, destination)
    except Exception as e:
        print(f"Google API Error, switching to Free: {e}")
        return get_free_navigation(origin, destination)

def ask_gemini(prompt):
    """פנייה לבינה המלאכותית Gemini"""
    if not is_safe(prompt):
        return "התוכן חסום."
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.json()["candidates"][0]["content"]["parts"][0]["text"]
    except:
        return "שגיאה בחיבור לבינה המלאכותית."

@app.get("/ivr", response_class=PlainTextResponse)
async def ivr_logic(request: Request):
    p = request.query_params
    phone = p.get("phone", "")
    path = p.get("path", "")
    speech = p.get("search", "")
    digits = p.get("Digits", "")

    if phone in BLOCKED_USERS:
        return "id_list_message=t-הגישה חסומה&hangup"

    if path in ["waze", "moovit"]:
        origin = p.get("origin_text", "")
        if not origin:
            return "read=t-נא לומר בקול את נקודת המוצא-origin_text,no,speech,yes,he-IL,no"
        if not speech:
            return "read=t-לאן תרצה להגיע?-search,no,speech,yes,he-IL,no"
        mode = "driving" if path == "waze" else "transit"
        nav_res = get_navigation(origin, speech, mode=mode)
        return f"id_list_message=t-{nav_res}&goto=/0"

    if path in ["spotify", "youtube"]:
        if not digits and not speech:
            return "read=t-לשירים חדשים הקש 1. לחיפוש קולי הקש 2.=selection,no,1,1,1,Ok,no"
        if digits == "1":
            urls = get_yt_audio("שירים חדשים להיטים 2026", count=10)
            if not urls or urls == "blocked":
                return "id_list_message=t-לא נמצאו שירים או שהתוכן חסום"
            url = random.choice(urls)
            return f"play_url={url}&play_url_control=yes&play_url_digits=2"
        if digits == "2":
            return "read=t-נא לומר את שם השיר לחיפוש-search,no,speech,yes,he-IL,no"
        if speech:
            url = get_yt_audio(speech)
            if url == "blocked":
                return "id_list_message=t-התוכן חסום"
            if not url:
                return "id_list_message=t-שיר לא נמצא"
            return f"play_url={url}&play_url_control=yes&play_url_digits=2"

    if path in ["chat_gizra", "chat_pargod"]:
        if not speech:
            return "read=t-מה השאלה שלך?-search,no,speech,yes,he-IL,no"
        res = ask_gemini(speech)
        return f"id_list_message=t-{res[:400]}&goto=/0"

    return "id_list_message=t-ברוכים הבאים למערכת החכמה. נא לבחור שלוחה&goto=/0"

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
