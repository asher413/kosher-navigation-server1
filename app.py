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
# שים לב: הכנס כאן את המפתחות האמיתיים שלך
GEMINI_API_KEY = "AIzaSy..." 
GOOGLE_MAPS_KEY = "YOUR_GOOGLE_MAPS_KEY"
gmaps = googlemaps.Client(key=GOOGLE_MAPS_KEY)

FORBIDDEN_WORDS = ["מילה1", "מילה2", "תוכן_לא_הולם"] 
BLOCKED_USERS = ["0501234567"]

def is_safe(text):
    """פונקציה לבדיקת סינון תוכן"""
    if not text: return True
    return not any(word in text for word in FORBIDDEN_WORDS)

def get_yt_audio(query, count=1):
    """חיפוש ביוטיוב והחזרת קישור לשמיעה"""
    if not is_safe(query): return "blocked"
    ydl_opts = {'format': 'bestaudio/best', 'noplaylist': True, 'quiet': True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(f"ytsearch{count}:{query}", download=False)
            if count == 1:
                title = info['entries'][0]['title']
                if not is_safe(title): return "blocked"
                return info['entries'][0]['url']
            else:
                urls = [entry['url'] for entry in info['entries'] if is_safe(entry['title'])]
                return urls
        except: return None

def get_navigation(origin, destination, mode="driving"):
    """מחשב מסלול ומחזיר הוראות קוליות"""
    try:
        now = datetime.now()
        directions = gmaps.directions(origin, destination, mode=mode, departure_time=now, language='he')
        
        if not directions:
            return "לא נמצא מסלול מתאים."
            
        leg = directions[0]['legs'][0]
        steps = leg['steps']
        
        instructions = [f"המסלול מ{leg['start_address']} ל{leg['end_address']}. זמן משוער {leg['duration']['text']}."]
        for step in steps[:4]: 
            clean_instr = step['html_instructions'].replace('<b>', '').replace('</b>', '').replace('<div style="font-size:0.9em">', ' ').replace('</div>', '')
            instructions.append(f"בעוד {step['distance']['text']}, {clean_instr}")
            
        return ". ".join(instructions)
    except Exception as e:
        print(f"Error in nav: {e}")
        return "שגיאה בחישוב המסלול. וודא שהכתובות נכונות."

def ask_gemini(prompt):
    """פנייה לבינה המלאכותית Gemini"""
    if not is_safe(prompt): return "התוכן חסום."
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.json()['candidates'][0]['content']['parts'][0]['text']
    except: return "שגיאה בחיבור לבינה המלאכותית."

@app.get("/ivr", response_class=PlainTextResponse)
async def ivr_logic(request: Request):
    p = request.query_params
    phone = p.get("phone", "")
    path = p.get("path", "")
    speech = p.get("search", "") 
    digits = p.get("Digits", "")

    if phone in BLOCKED_USERS:
        return "id_list_message=t-הגישה חסומה&hangup"

    # --- שלוחה 1: וויז | שלוחה 2: מוביט ---
    if path in ["waze", "moovit"]:
        origin = p.get("origin_text", "")
        if not origin:
            return "read=t-נא לומר בקול את נקודת המוצא-origin_text,no,record,no"
        
        if not speech: 
            return "read=t-לאן תרצה להגיע?-search,no,record,no"
        
        mode = "driving" if path == "waze" else "transit"
        nav_res = get_navigation(origin, speech, mode=mode)
        return f"id_list_message=t-{nav_res}&goto=/0"
    
    # --- שלוחה 3: ספוטיפיי | שלוחה 4: יוטיוב ---
    if path in ["spotify", "youtube"]:
        if not digits and not speech:
            return "read=t-לשירים חדשים הקש 1. לחיפוש קולי הקש 2.=selection,no,1,1,1,Ok,no"
            
        if digits == "1":
            urls = get_yt_audio("שירים חדשים להיטים 2026", count=10)
            if not urls or urls == "blocked": return "id_list_message=t-לא נמצאו שירים או שהתוכן חסום"
            url = random.choice(urls)
            return f"play_url={url}&play_url_control=yes&play_url_digits=2"
        
        if digits == "2":
            return "read=t-נא לומר את שם השיר לחיפוש-search,no,record,no"

        if speech:
            url = get_yt_audio(speech)
            if url == "blocked": return "id_list_message=t-התוכן חסום"
            if not url: return "id_list_message=t-שיר לא נמצא"
            return f"play_url={url}&play_url_control=yes&play_url_digits=2"
            
    # --- שלוחה 5/6: בינה מלאכותית ---
    if path in ["chat_gizra", "chat_pargod"]:
        if not speech: return "read=t-מה השאלה שלך?-search,no,record,no"
        res = ask_gemini(speech)
        return f"id_list_message=t-{res[:400]}&goto=/0"

    return "id_list_message=t-ברוכים הבאים למערכת החכמה. נא לבחור שלוחה&goto=/0"

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
