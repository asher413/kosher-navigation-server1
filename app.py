from fastapi import FastAPI
from pydantic import BaseModel
import requests

app = FastAPI()

# פונקציה למציאת קואורדינטות (קו רוחב וגובה) מכתובת
def get_coords(address):
    url = f"https://nominatim.openstreetmap.org/search?q={address}&format=json&limit=1"
    headers = {'User-Agent': 'KosherNavApp/1.0'}
    resp = requests.get(url, headers=headers).json()
    if resp:
        return f"{resp[0]['lon']},{resp[0]['lat']}"
    return None

class RouteRequest(BaseModel):
    start: str
    end: str

@app.post("/route")
def calculate_route(data: RouteRequest):
    start_coords = get_coords(data.start)
    end_coords = get_coords(data.end)

    if not start_coords or not end_coords:
        return {"error": "אחת הכתובות לא נמצאה"}

    # פנייה למנוע הניווט החינמי OSRM
    osrm_url = f"http://router.project-osrm.org/route/v1/driving/{start_coords};{end_coords}?overview=false"
    route_resp = requests.get(osrm_url).json()

    if "routes" not in route_resp:
        return {"error": "לא נמצא מסלול"}

    distance_km = round(route_resp['routes'][0]['distance'] / 1000, 2)
    duration_min = round(route_resp['routes'][0]['duration'] / 60)

    return {
        "start": data.start,
        "end": data.end,
        "distance": f"{distance_km} קילומטר",
        "duration": f"{duration_min} דקות נסיעה",
        "instructions": "כאן יבואו הנחיות הפנייה בשלב הבא"
    }
