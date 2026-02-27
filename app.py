from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
import requests
import yt_dlp
import uvicorn
import random
import googlemaps
from datetime import datetime
import os

app = FastAPI()

# --- מפתחות מהסביבה ---
GEMINI_API_KEY = "AIzaSyCG7bz2Ew0IpyQHzYX4ZqwSIXf9navfsNw"
GOOGLE_MAPS_KEY = "AIzaSyCG7bz2Ew0IpyQHzYX4ZqwSIXf9navfsNw"

gmaps = googlemaps.Client(key=GOOGLE_MAPS_KEY) if GOOGLE_MAPS_KEY else None

FORBIDDEN_WORDS = ["מילה1", "מילה2", "תוכן_לא_הולם"]
BLOCKED_USERS = ["0501234567"]


def is_safe(text):
    if not text:
        return True
    return not any(word in text for word in FORBIDDEN_WORDS)


def get_yt_audio(query, count=1):
    if not is_safe(query):
        return "blocked"

    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch",
        "ignoreerrors": True,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 5,
        "socket_timeout": 15,
        "force_ipv4": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            search_query = f"ytsearch{count}:{query}"
            info = ydl.extract_info(search_query, download=False)

            if not info or "entries" not in info or not info["entries"]:
                return None

            valid_entries = [
                e for e in info["entries"]
                if e and is_safe(e.get("title", ""))
            ]

            if not valid_entries:
                return None

            if count == 1:
                return valid_entries[0].get("url")

            return [e.get("url") for e in valid_entries if e.get("url")]

    except Exception as e:
        print(f"YouTube Error: {e}")
        return None


def get_free_navigation(origin, destination):
    try:
        base_geo = "https://nominatim.openstreetmap.org/search"
        headers = {"User-Agent": "MyIVRSystem/1.0"}

        orig_geo = requests.get(
            f"{base_geo}?q={origin}&format=json",
            headers=headers,
            timeout=10
        ).json()

        dest_geo = requests.get(
            f"{base_geo}?q={destination}&format=json",
            headers=headers,
            timeout=10
        ).json()

        if not orig_geo or not dest_geo:
            return "לא הצלחתי למצוא את הכתובת במערכת החינמית."

        osrm_url = (
            f"http://router.project-osrm.org/route/v1/driving/"
            f"{orig_geo[0]['lon']},{orig_geo[0]['lat']};"
            f"{dest_geo[0]['lon']},{dest_geo[0]['lat']}?overview=false&steps=true"
        )

        route_res = requests.get(osrm_url, timeout=10).json()

        if "routes" not in route_res or not route_res["routes"]:
            return "לא נמצאה דרך זמינה."

        steps = route_res["routes"][0]["legs"][0]["steps"]
        instructions = ["שימוש במערכת גיבוי חינמית."]

        for step in steps[:3]:
            instructions.append(
                f"בעוד {int(step['distance'])} מטרים, "
                f"{step['maneuver'].get('instruction', '')}"
            )

        return ". ".join(instructions)

    except Exception:
        return "מערכת הניווט אינה זמינה כרגע."


def get_navigation(origin, destination, mode="driving"):
    if not gmaps:
        return get_free_navigation(origin, destination)

    try:
        now = datetime.now()

        directions = gmaps.directions(
            origin,
            destination,
            mode=mode,
            departure_time=now,
            language="he"
        )

        if not directions:
            return get_free_navigation(origin, destination)

        leg = directions[0]["legs"][0]
        steps = leg["steps"]

        instructions = [
            f"מסלול מ{leg['start_address']} ל{leg['end_address']}."
        ]

        for step in steps[:4]:
            clean_instr = (
                step["html_instructions"]
                .replace("<b>", "")
                .replace("</b>", "")
                .replace("</div>", "")
            )

            instructions.append(
                f"בעוד {step['distance']['text']}, {clean_instr}"
            )

        return ". ".join(instructions)

    except Exception:
        return get_free_navigation(origin, destination)


def ask_gemini(prompt):
    if not is_safe(prompt) or not GEMINI_API_KEY:
        return "התוכן חסום או שאין מפתח API."

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    )

    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    try:
        response = requests.post(url, json=payload, timeout=15)
        data = response.json()

        if "candidates" not in data:
            return "שגיאה בקבלת תשובה."

        return data["candidates"][0]["content"]["parts"][0]["text"]

    except Exception:
        return "שגיאה בחיבור לבינה המלאכותית."


@app.get("/ivr", response_class=PlainTextResponse)
async def ivr_logic(request: Request):
    p = request.query_params
    
    # התיקון הקריטי: לוקח רק את המילה שלפני סימן השאלה אם קיים
    raw_path = p.get("path", "")
    path = raw_path.split('?')[0].strip() 
    
    speech = p.get("search", "")

    # --- צ'אט בינה מלאכותית ---
    if path == "chat":
        if not speech:
            return "read=t-מה השאלה שלך?-search,no,speech,no,he-IL,no"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
        try:
            res = requests.post(url, json={"contents": [{"parts": [{"text": speech}]}]}, timeout=10).json()
            text = res["candidates"][0]["content"]["parts"][0]["text"]
            return f"id_list_message=t-{text[:400]}"
        except:
            return "id_list_message=t-שגיאה בחיבור לבינה"

    # --- יוטיוב ---
    if path == "youtube":
        if not speech:
            return "read=t-נא לומר שם של שיר-search,no,speech,no,he-IL,no"
        ydl_opts = {"format": "bestaudio/best", "noplaylist": True, "quiet": True, "default_search": "ytsearch1"}
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{speech}", download=False)
                audio_url = info['entries'][0]['url']
                return f"play_url={audio_url}&play_url_control=yes"
        except:
            return "id_list_message=t-לא נמצא שיר מתאים"

    # --- ניווט ---
    if path in ["waze", "moovit"]:
        origin = p.get("origin_text", "")
        if not origin:
            return "read=t-נא לומר את נקודת המוצא-origin_text,no,speech,no,he-IL,no"
        if not speech:
            return "read=t-לאן תרצה להגיע?-search,no,speech,no,he-IL,no"
        try:
            mode = "driving" if path == "waze" else "transit"
            directions = gmaps.directions(origin, speech, mode=mode, language="he")
            if not directions:
                return "id_list_message=t-לא נמצא מסלול זמין"
            leg = directions[0]["legs"][0]
            msg = f"מסלול מ{leg['start_address']} ל{leg['end_address']}. "
            for step in leg["steps"][:3]:
                instr = step["html_instructions"].replace("<b>","").replace("</b>","").replace("</div>","")
                msg += f"בעוד {step['distance']['text']}, {instr}. "
            return f"id_list_message=t-{msg[:400]}"
        except:
            return "id_list_message=t-שגיאה בשירות הניווט"

    # הודעת דיבוב למקרה שזה עדיין לא מזהה
    return f"id_list_message=t-המערכת זיהתה נתיב בשם {path}"
    
    # אם הגענו לכאן, סימן שאף if לא עבד (ה-path לא עבר נכון)
        current_path = path if path else "ריק"
        return f"id_list_message=t-שגיאה. השלוחה הוגדרה כ-{current_path}. נא לבדוק את הגדרות ה-API"

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
