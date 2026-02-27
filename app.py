from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
import requests
import yt_dlp
import uvicorn

app = FastAPI()

# --- הגדרות אבטחה וסינון ---
GEMINI_API_KEY = "AIzaSy..." # כאן המפתח שצירפת (מחקתי אותו לצורכי אבטחה בקוד הציבורי, שים אותו כאן)
FORBIDDEN_WORDS = ["מילה1", "מילה2", "תוכן_לא_הולם"] # רשימת מילים אסורות לסינון
BLOCKED_USERS = ["0501234567"] # רשימת מספרי טלפון חסומים

def is_safe(text):
    """פונקציה לבדיקת סינון תוכן"""
    if not text: return True
    return not any(word in text for word in FORBIDDEN_WORDS)

def get_yt_audio(query):
    """חיפוש ביוטיוב והחזרת קישור לשמיעה"""
    if not is_safe(query): return "blocked"
    ydl_opts = {'format': 'bestaudio/best', 'noplaylist': True, 'quiet': True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
            title = info['entries'][0]['title']
            if not is_safe(title): return "blocked"
            return info['entries'][0]['url']
        except: return None

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
    speech = p.get("search", "") # הטקסט מההקלטה (ASR)
    digits = p.get("Digits", "")

    # בדיקת חסימה
    if phone in BLOCKED_USERS:
        return "id_list_message=t-הגישה חסומה&hangup"

    # --- ניתוב שלוחות ---
    
    # שלוחה 1: וויז | שלוחה 2: מוביט
    if path in ["waze", "moovit"]:
        if not speech: return "read=t-לאן תרצה להגיע?-search,no,record,no"
        return f"id_list_message=t-מחשב מסלול אל {speech}. המתן...&goto=/0"

# --- שלוחה 3: ספוטיפיי | שלוחה 4: יוטיוב ---
    if path in ["spotify", "youtube"]:
        # אם המשתמש רק נכנס לשלוחה ולא הקיש כלום
        if not digits and not speech:
            return "read=t-לשירים חדשים הקש 1. לחיפוש קולי הקש 2.=selection,no,1,1,1,Ok,no"

        # אפשרות 1: שירים חדשים (מבצע חיפוש אוטומטי של להיטים חדשים)
        if digits == "1":
            search_query = "שירים חדשים 2024 להיטים"
            url = get_yt_audio(search_query)
            if url == "blocked": return "id_list_message=t-התוכן חסום"
            return f"play_url={url}"

        # אפשרות 2: מעבר לחיפוש קולי
        if digits == "2":
            return "read=t-נא לומר את שם השיר או האמן לחיפוש-search,no,record,no"

        # עיבוד תוצאת החיפוש הקולי (אם המערכת כבר הקליטה)
        if speech:
            url = get_yt_audio(speech)
            if url == "blocked": return "id_list_message=t-התוכן חסום לשימוש"
            if not url: return "id_list_message=t-שיר לא נמצא"
            return f"play_url={url}"

    # שלוחה 5: צ'אט הגיזרה | שלוחה 6: צ'אט הפרגוד (בינה מלאכותית)
    if path in ["chat_gizra", "chat_pargod"]:
        if not speech: return "read=t-שלום, אני הבינה המלאכותית. מה השאלה?-search,no,record,no"
        res = ask_gemini(speech)
        return f"id_list_message=t-{res[:400]}&goto=/0"

    # החלפת מספר מערכת (דוגמה ללוגיקה)
    if path == "change_num":
        return "id_list_message=t-המספר יוחלף בתוך 24 שעות"

    return "id_list_message=t-ברוכים הבאים למערכת החכמה&goto=/0"

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
