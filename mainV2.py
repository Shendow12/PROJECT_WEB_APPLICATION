import os
import re
from fastapi import FastAPI, HTTPException, Query, Body, status
from pydantic import BaseModel, field_validator
from typing import List, Optional
from dotenv import load_dotenv
from supabase import create_client, Client
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo # Necesită Python 3.9+
from fastapi.middleware.cors import CORSMiddleware
# --- 1. Configurare & Conexiune ---
load_dotenv()

app = FastAPI(
    title="QuickWash MVP",
    description="Backend Final: Fără Auth, Rezervare Smart, Validare Program."
)

# Permitem oricărui frontend (Bolt, Localhost) să acceseze API-ul
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # În producție pui doar domeniul tău, pt dev lăsăm "*"
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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

# --- Modele pentru disponibilitate ---
class IntervalLiber(BaseModel):
    start: datetime
    end: datetime
    minute_disponibile: int

# ACEASTA TREBUIE SĂ FIE PRIMA
class BoxaDisponibila(BaseModel):
    boxa_id: str
    nume_boxa: str
    pret_rezervare_lei: float
    intervale: List[IntervalLiber]

# ACEASTA O FOLOSEȘTE PE CEA DE MAI SUS
class SpalatorieDisponibilaResponse(BaseModel):
    spalatorie_id: str
    nume: str
    program_functionare: str
    distanta_km: Optional[float] = None
    latitudine: float
    longitudine: float
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
    gaps = []
    
    # 1. Ora României (Manual Offset pentru siguranță pe Render)
    RO_OFFSET = timezone(timedelta(hours=2)) 
    now_ro = start_window_utc.astimezone(RO_OFFSET)
    
    ora_deschidere, ora_inchidere = parse_schedule(program_str)
    
    # 2. Determinăm intervalele de funcționare pentru "AZI"
    # Un program poate fi continuu (8-20) sau spart de miezul nopții (10-02)
    open_intervals = [] # Lista de tupluri (start_hour, end_hour)
    
    if ora_deschidere < ora_inchidere:
        # Program normal (ex: 08:00 - 22:00)
        open_intervals.append((ora_deschidere, ora_inchidere))
    elif ora_deschidere > ora_inchidere:
        # Program peste noapte (ex: 10:00 - 02:00)
        # Interval 1: 00:00 - 02:00 (dimineața devreme)
        open_intervals.append((0, ora_inchidere))
        # Interval 2: 10:00 - 24:00 (ziua și seara)
        open_intervals.append((ora_deschidere, 24))
    else:
        # Non-stop sau 00-24 sau 08-08
        open_intervals.append((0, 24))

    # 3. Verificăm dacă suntem într-un interval deschis ACUM
    # Sau ajustăm startul la următorul interval deschis
    adjusted_start_utc = None
    current_hour = now_ro.hour + (now_ro.minute / 60)
    
    is_open_now = False
    next_open_hour = None
    
    # Căutăm unde ne încadrăm
    for (start_h, end_h) in open_intervals:
        # Suntem în interval?
        if start_h <= current_hour < end_h:
            is_open_now = True
            # Limita de închidere curentă
            current_closing_hour = end_h
            break
        
        # Dacă nu suntem, care e următorul start?
        if start_h > current_hour:
            if next_open_hour is None or start_h < next_open_hour:
                next_open_hour = start_h
                current_closing_hour = end_h

    if is_open_now:
        # Suntem deschiși, păstrăm ora curentă
        adjusted_start_utc = start_window_utc
    elif next_open_hour is not None:
        # Suntem închiși, dar deschidem mai târziu azi
        # Ajustăm startul la ora deschiderii
        target_h = int(next_open_hour)
        target_m = int((next_open_hour - target_h) * 60)
        start_ro_adjusted = now_ro.replace(hour=target_h, minute=target_m, second=0)
        adjusted_start_utc = start_ro_adjusted.astimezone(timezone.utc)
    else:
        # S-a închis pe ziua de azi (și nu mai deschide până la 24:00)
        return []

    # Verificăm dacă ajustarea a depășit fereastra de căutare
    if adjusted_start_utc >= end_window_utc:
        return []

    # Setăm cursorul
    current_time = adjusted_start_utc
    
    # 4. Calculul efectiv al găurilor (Iterare printre rezervări)
    rezervari_sorted = sorted(
        rezervari, 
        key=lambda x: datetime.fromisoformat(x['ora_start'])
    )

    for res in rezervari_sorted:
        res_start = datetime.fromisoformat(res['ora_start'])
        res_end = datetime.fromisoformat(res['ora_sfarsit'])

        if res_start > current_time:
            # Avem un potențial gap. Trebuie să îl tăiem la ora închiderii curente
            # 'current_closing_hour' e ora la care se termină tura curentă (ex: 22:00 sau 02:00 sau 24:00)
            
            gap_start_ro = current_time.astimezone(RO_OFFSET)
            limit_end = res_start
            
            # Calculăm timestamp-ul orei de închidere pentru AZI
            if current_closing_hour != 24:
                h_close = int(current_closing_hour)
                m_close = int((current_closing_hour - h_close) * 60)
                
                # Atenție: dacă ora de închidere e mâine (teoretic nu ajungem aici pt că am spart intervalele pe zile)
                # Dar pentru siguranță, folosim data curentă a gap-ului
                ora_inchidere_azi_ro = gap_start_ro.replace(hour=h_close, minute=m_close, second=0)
                ora_inchidere_azi_utc = ora_inchidere_azi_ro.astimezone(timezone.utc)
                
                if limit_end > ora_inchidere_azi_utc:
                    limit_end = ora_inchidere_azi_utc
            
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

    # 5. Gap Final
    if current_time < end_window_utc:
        limit_end = end_window_utc
        
        # Tăiere finală la închidere
        gap_start_ro = current_time.astimezone(RO_OFFSET)
        if current_closing_hour != 24:
            h_close = int(current_closing_hour)
            m_close = int((current_closing_hour - h_close) * 60)
            ora_inchidere_azi_ro = gap_start_ro.replace(hour=h_close, minute=m_close, second=0)
            ora_inchidere_azi_utc = ora_inchidere_azi_ro.astimezone(timezone.utc)
            
            if limit_end > ora_inchidere_azi_utc:
                limit_end = ora_inchidere_azi_utc
        
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
                    
                    # --- LINII NOI PENTRU MAPARE ---
                    "program_functionare": program,
                    "latitudine": loc['latitudine'],   # Luăm din RPC
                    "longitudine": loc['longitudine'], # Luăm din RPC
                    # -------------------------------
                    
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

# ---------------------------
# D. ADMIN & ISTORIC (Rute Noi)
# ---------------------------

@app.get("/rezervari", response_model=List[RezervareResponse], summary="Toate Rezervările (Admin)")
def get_toate_rezervarile(
    client_ref: Optional[str] = Query(None, description="Filtrează după nr. telefon/auto")
):
    """
    Returnează lista tuturor rezervărilor din sistem.
    Opțional: poți filtra după un client specific.
    """
    try:
        query = supabase.table('rezervari').select('*')
        
        # Dacă am primit un client_ref, filtrăm (Istoric Client)
        if client_ref:
            query = query.eq('client_ref', client_ref)
            
        # Ordonăm descrescător (cele mai noi primele)
        response = query.order('ora_start', desc=True).execute()
        
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/spalatorii/{spalatorie_id}/rezervari", response_model=List[RezervareResponse], summary="Rezervări per Spălătorie")
def get_rezervari_spalatorie(
    spalatorie_id: str,
    doar_active: bool = Query(False, description="Dacă true, arată doar ce urmează")
):
    """
    Returnează toate rezervările pentru o anumită spălătorie.
    Util pentru dashboard-ul proprietarului.
    """
    try:
        query = supabase.table('rezervari').select('*').eq('spalatorie_id', spalatorie_id)
        
        if doar_active:
            # Arată doar ce nu a expirat încă
            now = datetime.now(timezone.utc).isoformat()
            query = query.eq('status', 'activa').gte('ora_sfarsit', now)
            
        response = query.order('ora_start', desc=True).execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)