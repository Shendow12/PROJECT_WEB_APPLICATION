import os
import re
from fastapi import FastAPI, HTTPException, Query, Body, status
from pydantic import BaseModel, field_validator
from typing import List, Optional
from dotenv import load_dotenv
from supabase import create_client, Client
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo # Necesită Python 3.9+

# --- 1. Configurare & Conexiune ---
load_dotenv()

app = FastAPI(
    title="QuickWash MVP",
    description="Backend Final: Fără Auth, Rezervare Smart, Validare Program."
)

SUPABASE_URL: str = os.environ.get("SUPABASE_URL")
SUPABASE_KEY: str = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL sau SUPABASE_KEY lipsesc din fișierul .env")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ==========================================
# 2. MODELE DE DATE (Pydantic Schemas)
# ==========================================

# --- Spălătorii ---
class SpalatorieCreate(BaseModel):
    nume: str
    adresa: Optional[str] = None
    
    # Validăm formatul programului
    program_functionare: str = "00:00 - 24:00"
    
    latitudine: float
    longitudine: float

    @field_validator('program_functionare')
    @classmethod
    def validate_program(cls, v: str) -> str:
        if "non" in v.lower() and "stop" in v.lower():
            return "00:00 - 24:00"
        
        pattern = r"^\d{2}:\d{2}\s*-\s*\d{2}:\d{2}$"
        if not re.match(pattern, v.strip()):
            raise ValueError('Format invalid! Folosește strict "HH:MM - HH:MM" (ex: 08:00 - 22:00)')
        return v

class SpalatorieResponse(BaseModel):
    id: str
    nume: str
    adresa: Optional[str]
    program_functionare: Optional[str]
    latitudine: float
    longitudine: float
    distanta_km: Optional[float] = None

# --- Boxe ---
class BoxaBase(BaseModel):
    nume_boxa: str
    pret_rezervare_lei: float = 15.0
    timp_rezervare_minute: int = 60
    is_available: bool = True

class BoxaCreate(BoxaBase):
    pass

class BoxaUpdate(BaseModel):
    nume_boxa: Optional[str] = None
    pret_rezervare_lei: Optional[float] = None
    timp_rezervare_minute: Optional[int] = None
    is_available: Optional[bool] = None

class BoxaResponse(BoxaBase):
    boxa_id: str
    spalatorie_id: str

# --- Rezervări (Fără User ID) ---
class RezervareCreate(BaseModel):
    boxa_id: str
    durata_minute: int 
    # OBLIGATORIU: Telefon sau Nr. Înmatriculare (Identitatea clientului)
    client_ref: str 

class RezervareResponse(BaseModel):
    rezervare_id: str
    boxa_id: str
    spalatorie_id: str
    ora_start: datetime
    ora_sfarsit: datetime
    status: str
    client_ref: str

# --- Disponibilitate ---
class IntervalLiber(BaseModel):
    start: datetime
    end: datetime
    minute_disponibile: int

class BoxaDisponibila(BaseModel):
    boxa_id: str
    nume_boxa: str
    pret_rezervare_lei: float
    intervale: List[IntervalLiber]

class SpalatorieDisponibilaResponse(BaseModel):
    spalatorie_id: str
    nume: str
    program_functionare: str
    distanta_km: Optional[float] = None
    boxe_libere: List[BoxaDisponibila]


# ==========================================
# 3. LOGICA DE BUSINESS (Helpers & Algoritmi)
# ==========================================

def parse_schedule(schedule_str: str):
    """ Extrage orele (int) din string. """
    if not schedule_str or "00:00 - 24:00" in schedule_str:
        return 0, 24
    try:
        parts = schedule_str.split('-')
        start = int(parts[0].strip().split(':')[0])
        end = int(parts[1].strip().split(':')[0])
        return start, end
    except:
        return 0, 24 

def calculeaza_gaps(
    start_window_utc: datetime, 
    end_window_utc: datetime, 
    rezervari: list, 
    durata_minima_minute: int,
    program_str: str = "00:00 - 24:00"
):
    """ Algoritmul Smart care ține cont de fus orar RO și program. """
    gaps = []
    
    # 1. Fus Orar România
    try:
        tz_ro = ZoneInfo("Europe/Bucharest")
    except:
        tz_ro = timezone.utc

    now_ro = start_window_utc.astimezone(tz_ro)
    ora_deschidere, ora_inchidere = parse_schedule(program_str)
    
    # 2. Ajustăm fereastra de start (Clamping)
    if now_ro.hour < ora_deschidere:
        start_ro_adjusted = now_ro.replace(hour=ora_deschidere, minute=0, second=0)
        start_window_utc = start_ro_adjusted.astimezone(timezone.utc)
    elif now_ro.hour >= ora_inchidere and ora_inchidere != 24:
        return [] 

    if start_window_utc >= end_window_utc:
        return []

    current_time = start_window_utc
    
    rezervari_sorted = sorted(
        rezervari, 
        key=lambda x: datetime.fromisoformat(x['ora_start'])
    )

    for res in rezervari_sorted:
        res_start = datetime.fromisoformat(res['ora_start'])
        res_end = datetime.fromisoformat(res['ora_sfarsit'])

        if res_start > current_time:
            # Tăiem la ora închiderii
            gap_start_ro = current_time.astimezone(tz_ro)
            limit_end = res_start
            
            if ora_inchidere != 24:
                ora_inchidere_azi = gap_start_ro.replace(hour=ora_inchidere, minute=0, second=0).astimezone(timezone.utc)
                if limit_end > ora_inchidere_azi:
                    limit_end = ora_inchidere_azi
            
            if limit_end > current_time:
                gap_duration = (limit_end - current_time).total_seconds() / 60
                if gap_duration >= durata_minima_minute:
                    gaps.append({
                        "start": current_time,
                        "end": limit_end,
                        "minute_disponibile": int(gap_duration)
                    })

        if res_end > current_time:
            current_time = res_end

    # Gap final
    if current_time < end_window_utc:
        limit_end = end_window_utc
        
        gap_start_ro = current_time.astimezone(tz_ro)
        if ora_inchidere != 24:
            ora_inchidere_azi = gap_start_ro.replace(hour=ora_inchidere, minute=0, second=0).astimezone(timezone.utc)
            if limit_end > ora_inchidere_azi:
                limit_end = ora_inchidere_azi
        
        if limit_end > current_time:
            gap_duration = (limit_end - current_time).total_seconds() / 60
            if gap_duration >= durata_minima_minute:
                gaps.append({
                    "start": current_time,
                    "end": limit_end,
                    "minute_disponibile": int(gap_duration)
                })
            
    return gaps


# ==========================================
# 4. RUTE API (Endpoints)
# ==========================================

@app.get("/", summary="Health Check")
def read_root():
    return {"status": "QuickWash API este live!"}

# --- A. SPĂLĂTORII ---

@app.post("/spalatorii", status_code=status.HTTP_201_CREATED)
def add_spalatorie(spalatorie: SpalatorieCreate = Body(...)):
    try:
        data = spalatorie.model_dump()
        response = supabase.table('spalatorii').insert({
            "nume": data['nume'],
            "adresa": data['adresa'],
            "program_functionare": data['program_functionare'],
            "locatie": f"SRID=4326;POINT({data['longitudine']} {data['latitudine']})"
        }).execute()
        if response.data: return response.data[0]
        raise HTTPException(status_code=500, detail="Eroare salvare.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/spalatorii-apropiate/disponibilitate", response_model=List[SpalatorieDisponibilaResponse])
def get_spalatorii_apropiate_disponibile(
    lat: float, lon: float, raza_km: float = 5.0, durata_dorita_min: int = 30
):
    try:
        # 1. Căutare Geo (RPC)
        locatii = supabase.rpc('gaseste_apropiate', {'user_lat': lat, 'user_lon': lon, 'raza_km': raza_km}).execute()
        if not locatii.data: return []

        spalatorii_ids = [s['id'] for s in locatii.data]
        now = datetime.now(timezone.utc)
        end_window = now + timedelta(hours=2)

        # 2. Fetch Data
        boxe_all = supabase.table('boxe').select('*').in_('spalatorie_id', spalatorii_ids).eq('is_available', True).execute()
        rezervari_all = supabase.table('rezervari').select('*').in_('spalatorie_id', spalatorii_ids).eq('status', 'activa').gte('ora_sfarsit', now.isoformat()).lte('ora_start', end_window.isoformat()).execute()

        rezultat_final = []

        # 3. Procesare
        for loc in locatii.data:
            program = loc.get('program_functionare', "00:00 - 24:00") or "00:00 - 24:00"
            boxe_locatie = [b for b in boxe_all.data if b['spalatorie_id'] == loc['id']]
            boxe_cu_gaps = []

            for boxa in boxe_locatie:
                rez_boxa = [r for r in rezervari_all.data if r['boxa_id'] == boxa['boxa_id']]
                gaps = calculeaza_gaps(now, end_window, rez_boxa, durata_dorita_min, program_str=program)
                if gaps:
                    boxe_cu_gaps.append({
                        "boxa_id": boxa['boxa_id'],
                        "nume_boxa": boxa['nume_boxa'],
                        "pret_rezervare_lei": boxa['pret_rezervare_lei'],
                        "intervale": gaps
                    })
            
            if boxe_cu_gaps:
                rezultat_final.append({
                    "spalatorie_id": loc['id'],
                    "nume": loc['nume'],
                    "program_functionare": program,
                    "distanta_km": loc['distanta_km'],
                    "boxe_libere": boxe_cu_gaps
                })

        return rezultat_final
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- B. BOXE (CRUD) ---

@app.get("/spalatorii/{spalatorie_id}/boxe", response_model=List[BoxaResponse])
def get_boxe_spalatorie(spalatorie_id: str):
    try:
        return supabase.table('boxe').select('*').eq('spalatorie_id', spalatorie_id).execute().data
    except Exception as e: raise HTTPException(500, str(e))

@app.post("/spalatorii/{spalatorie_id}/boxe", status_code=201)
def adauga_boxa(spalatorie_id: str, boxa: BoxaCreate = Body(...)):
    try:
        d = boxa.model_dump(); d['spalatorie_id'] = spalatorie_id
        return supabase.table('boxe').insert(d).execute().data[0]
    except Exception as e: raise HTTPException(500, str(e))

@app.patch("/spalatorii/{spalatorie_id}/boxe/{boxa_id}")
def update_boxa(spalatorie_id: str, boxa_id: str, u: BoxaUpdate):
    try:
        return supabase.table('boxe').update(u.model_dump(exclude_unset=True)).eq('boxa_id', boxa_id).execute().data[0]
    except Exception as e: raise HTTPException(500, str(e))

@app.delete("/spalatorii/{spalatorie_id}/boxe/{boxa_id}", status_code=204)
def sterge_boxa(spalatorie_id: str, boxa_id: str):
    try:
        supabase.table('boxe').delete().eq('boxa_id', boxa_id).execute()
    except Exception as e: raise HTTPException(500, str(e))

# --- C. REZERVĂRI (NO AUTH) ---

@app.post("/rezervari", status_code=status.HTTP_201_CREATED, response_model=RezervareResponse)
def creare_rezervare(rezervare: RezervareCreate):
    try:
        # 1. Aflăm locația
        boxa_info = supabase.table('boxe').select('spalatorie_id').eq('boxa_id', rezervare.boxa_id).execute()
        if not boxa_info.data:
            raise HTTPException(status_code=404, detail="Boxa nu există.")
        real_spalatorie_id = boxa_info.data[0]['spalatorie_id']

        # 2. Calculăm timpii
        start = datetime.now(timezone.utc)
        sfarsit = start + timedelta(minutes=rezervare.durata_minute)
        
        # 3. Inserăm (Fără user_id, doar client_ref)
        data_insert = {
            "boxa_id": rezervare.boxa_id,
            "spalatorie_id": real_spalatorie_id,
            "ora_start": start.isoformat(),
            "ora_sfarsit": sfarsit.isoformat(),
            "client_ref": rezervare.client_ref,
            "status": "activa"
        }
        response = supabase.table('rezervari').insert(data_insert).execute()
        if response.data: return response.data[0]
        raise HTTPException(status_code=500, detail="Eroare server.")

    except Exception as e:
        if "conflict" in str(e).lower() or "exclusion" in str(e).lower():
            raise HTTPException(status_code=409, detail="Boxa este deja ocupată!")
        raise HTTPException(status_code=500, detail=str(e))

@app.patch("/rezervari/{rezervare_id}/checkout")
def early_checkout(rezervare_id: str):
    try:
        now = datetime.now(timezone.utc).isoformat()
        r = supabase.table('rezervari').update({"ora_sfarsit": now, "status": "finalizata"}).eq('rezervare_id', rezervare_id).execute()
        if r.data: return r.data[0]
        raise HTTPException(404, "Nu există")
    except Exception as e: raise HTTPException(500, str(e))

# --- D. Disponibilitate Detaliată ---
@app.get("/spalatorii/{spalatorie_id}/disponibilitate", response_model=List[BoxaDisponibila])
def get_disponibilitate_spalatorie(
    spalatorie_id: str,
    durata_dorita_min: int = 30,
    fereastra_ore: int = 2
):
    try:
        now = datetime.now(timezone.utc)
        end_window = now + timedelta(hours=fereastra_ore)
        
        spalatorie = supabase.table('spalatorii').select('program_functionare').eq('id', spalatorie_id).execute()
        program = "00:00 - 24:00"
        if spalatorie.data:
            program = spalatorie.data[0].get('program_functionare', "00:00 - 24:00") or "00:00 - 24:00"

        boxe = supabase.table('boxe').select('*').eq('spalatorie_id', spalatorie_id).eq('is_available', True).execute()
        if not boxe.data: return []

        rezervari = supabase.table('rezervari').select('*').eq('spalatorie_id', spalatorie_id).eq('status', 'activa').gte('ora_sfarsit', now.isoformat()).lte('ora_start', end_window.isoformat()).execute()
        
        rezultat = []
        for boxa in boxe.data:
            rez_boxa = [r for r in rezervari.data if r['boxa_id'] == boxa['boxa_id']]
            gaps = calculeaza_gaps(now, end_window, rez_boxa, durata_dorita_min, program_str=program)
            if gaps:
                rezultat.append({
                    "boxa_id": boxa['boxa_id'],
                    "nume_boxa": boxa['nume_boxa'],
                    "pret_rezervare_lei": boxa['pret_rezervare_lei'],
                    "intervale": gaps
                })
        return rezultat
    except Exception as e: raise HTTPException(500, str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)