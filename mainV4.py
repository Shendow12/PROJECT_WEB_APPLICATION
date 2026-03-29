import os
import re
from fastapi import FastAPI, HTTPException, Query, Body, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, field_validator
from typing import List, Optional
from dotenv import load_dotenv
from supabase import create_client, Client
from datetime import datetime, timedelta, timezone
from fastapi.middleware.cors import CORSMiddleware
import google.generativeai as genai
from pydantic import BaseModel

# --- 1. CONFIGURARE & CONEXIUNE ---
load_dotenv()

app = FastAPI(
    title="QuickWash MVP",
    description="Backend Final: Supabase Auth + Rezervare Smart + Night Owl Fix"
)

# CORS - Permitem Frontend-ul
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL: str = os.environ.get("SUPABASE_URL")
SUPABASE_KEY: str = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL sau SUPABASE_KEY lipsesc din fișierul .env")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- CONFIGURARE GEMINI ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY lipsește din .env")

genai.configure(api_key=GEMINI_API_KEY)

# --- SECURITATE (Portarul) ---
# Această componentă este nouă și critică pentru Auth
security = HTTPBearer()

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """ 
    Verifică token-ul JWT trimis de Frontend.
    Returnează obiectul User din Supabase sau dă eroare 401.
    """
    token = credentials.credentials
    try:
        user_response = supabase.auth.get_user(token)
        if not user_response.user:
            raise HTTPException(status_code=401, detail="Token invalid sau expirat.")
        return user_response.user
    except Exception:
        raise HTTPException(status_code=401, detail="Trebuie să fii logat.")


# ==========================================
# 2. MODELE DE DATE (Pydantic Schemas)
# ==========================================

# --- Spălătorii ---
class SpalatorieCreate(BaseModel):
    nume: str
    adresa: Optional[str] = None
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
            raise ValueError('Format invalid! Folosește "HH:MM - HH:MM"')
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

# --- Rezervări (Actualizat pentru Ora Specifică) ---
class RezervareCreate(BaseModel):
    boxa_id: str
    durata_minute: int
    # Adăugăm acest câmp opțional. Dacă lipsește, înseamnă "ACUM".
    ora_start: Optional[datetime] = None

class RezervareResponse(BaseModel):
    rezervare_id: str
    boxa_id: str
    spalatorie_id: str
    ora_start: datetime
    ora_sfarsit: datetime
    status: str
    # MODIFICARE CRITICĂ: client_ref e optional acum, user_id e nou
    user_id: Optional[str] = None
    client_ref: Optional[str] = None 

# --- Modele Disponibilitate ---
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
    latitudine: float
    longitudine: float
    boxe_libere: List[BoxaDisponibila]


# ==========================================
# 3. LOGICA DE BUSINESS (Algoritmul Night Owl)
# ==========================================

def parse_schedule(schedule_str: str):
    if not schedule_str or "00:00 - 24:00" in schedule_str:
        return 0, 24
    try:
        parts = schedule_str.split('-')
        return int(parts[0].strip().split(':')[0]), int(parts[1].strip().split(':')[0])
    except:
        return 0, 24 

def calculeaza_gaps(start_window_utc, end_window_utc, rezervari, durata_minima_minute, program_str="00:00 - 24:00"):
    # Aceasta este versiunea V5 (Night Owl Fix) pe care ai cerut-o
    gaps = []
    RO_OFFSET = timezone(timedelta(hours=2)) 
    now_ro = start_window_utc.astimezone(RO_OFFSET)
    
    ora_deschidere, ora_inchidere = parse_schedule(program_str)
    
    open_intervals = []
    if ora_deschidere < ora_inchidere:
        open_intervals.append((ora_deschidere, ora_inchidere))
    elif ora_deschidere > ora_inchidere:
        open_intervals.append((0, ora_inchidere))
        open_intervals.append((ora_deschidere, 24))
    else:
        open_intervals.append((0, 24))

    adjusted_start_utc = None
    current_hour = now_ro.hour + (now_ro.minute / 60)
    is_open_now = False
    next_open_hour = None
    current_closing_hour = 24

    for (start_h, end_h) in open_intervals:
        if start_h <= current_hour < end_h:
            is_open_now = True
            current_closing_hour = end_h
            break
        if start_h > current_hour:
            if next_open_hour is None or start_h < next_open_hour:
                next_open_hour = start_h
                current_closing_hour = end_h

    if is_open_now:
        adjusted_start_utc = start_window_utc
    elif next_open_hour is not None:
        target_h = int(next_open_hour)
        target_m = int((next_open_hour - target_h) * 60)
        start_ro_adjusted = now_ro.replace(hour=target_h, minute=target_m, second=0)
        adjusted_start_utc = start_ro_adjusted.astimezone(timezone.utc)
    else:
        return []

    if adjusted_start_utc >= end_window_utc:
        return []

    current_time = adjusted_start_utc
    rezervari_sorted = sorted(rezervari, key=lambda x: datetime.fromisoformat(x['ora_start']))

    for res in rezervari_sorted:
        res_start = datetime.fromisoformat(res['ora_start'])
        res_end = datetime.fromisoformat(res['ora_sfarsit'])

        if res_start > current_time:
            gap_start_ro = current_time.astimezone(RO_OFFSET)
            limit_end = res_start
            
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
                    gaps.append({"start": current_time, "end": limit_end, "minute_disponibile": int(gap_duration)})

        if res_end > current_time:
            current_time = res_end

    if current_time < end_window_utc:
        limit_end = end_window_utc
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
                gaps.append({"start": current_time, "end": limit_end, "minute_disponibile": int(gap_duration)})
            
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

@app.get("/spalatorii-apropiate/disponibilitate")
def get_spalatorii_apropiate_disponibile(
    lat: float, 
    lon: float, 
    raza_km: float = 10, 
    durata_dorita_min: int = 30
):
    try:
        # 1. Cerem datele de la Supabase
        locatii = supabase.rpc(
            'get_spalatorii_apropiate', 
            {
                'p_lat': lat,       # <-- Am schimbat 'lat_user' în 'p_lat'
                'p_lon': lon,       # <-- Am schimbat 'lon_user' în 'p_lon'
                'p_raza_km': raza_km # <-- Am schimbat 'raza_km' în 'p_raza_km'
            }
        ).execute()
        
        if not locatii.data:
            return []

        # --- REPARAȚIA 1: Folosim 'spalatorie_id' ---
        spalatorii_ids = [s['spalatorie_id'] for s in locatii.data]
        
        now = datetime.now(timezone.utc)
        end_window = now + timedelta(hours=2)

        # 2. Luăm datele despre boxe și rezervări
        boxe_all = supabase.table('boxe').select('*').in_('spalatorie_id', spalatorii_ids).eq('is_available', True).execute()
        rezervari_all = supabase.table('rezervari').select('*').in_('spalatorie_id', spalatorii_ids).eq('status', 'activa').gte('ora_sfarsit', now.isoformat()).lte('ora_start', end_window.isoformat()).execute()

        rezultat_final = []
        for loc in locatii.data:
            program = loc.get('program_functionare', "00:00 - 24:00") or "00:00 - 24:00"
            
            # --- REPARAȚIA 2: Folosim 'spalatorie_id' ---
            boxe_locatie = [b for b in boxe_all.data if b['spalatorie_id'] == loc['spalatorie_id']]
            
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
                    # --- REPARAȚIA 3: Folosim 'spalatorie_id' ---
                    "spalatorie_id": loc['spalatorie_id'],
                    "nume": loc['nume'],
                    "program_functionare": program,
                    "latitudine": loc['latitudine'],
                    "longitudine": loc['longitudine'],
                    "distanta_km": loc['distanta_km'],
                    "boxe_libere": boxe_cu_gaps
                })

        return rezultat_final

    except Exception as e:
        print(f"Eroare CRITICA: {e}") # Asta ne va ajuta să vedem eroarea dacă mai apare
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

# --- C. REZERVĂRI (SECURIZE CU AUTH) ---

@app.post("/rezervari", status_code=status.HTTP_201_CREATED, response_model=RezervareResponse)
def creare_rezervare(
    rezervare: RezervareCreate, 
    user = Depends(get_current_user) # Necesită Login
):
    try:
        # 1. Aflăm locația (spalatorie_id) pe baza boxei
        boxa_info = supabase.table('boxe').select('spalatorie_id').eq('boxa_id', rezervare.boxa_id).execute()
        if not boxa_info.data:
            raise HTTPException(status_code=404, detail="Boxa nu există.")
        real_spalatorie_id = boxa_info.data[0]['spalatorie_id']

        # 2. Calculăm timpii (LOGICA NOUĂ)
        # Dacă frontend-ul a trimis o oră preferată, o folosim. Altfel, folosim "ACUM".
        if rezervare.ora_start:
            start = rezervare.ora_start
        else:
            start = datetime.now(timezone.utc)
            
        sfarsit = start + timedelta(minutes=rezervare.durata_minute)
        
        # 3. Inserăm (folosind user.id din token)
        data_insert = {
            "boxa_id": rezervare.boxa_id,
            "spalatorie_id": real_spalatorie_id,
            "ora_start": start.isoformat(),
            "ora_sfarsit": sfarsit.isoformat(),
            "user_id": user.id,       
            "status": "activa"
        }
        
        response = supabase.table('rezervari').insert(data_insert).execute()
        
        if response.data: 
            rezultat = response.data[0]
            # Adăugăm email-ul pentru frontend
            rezultat['client_ref'] = user.email 
            return rezultat
            
        raise HTTPException(status_code=500, detail="Eroare server.")

    except Exception as e:
        if "conflict" in str(e).lower() or "exclusion" in str(e).lower():
            raise HTTPException(status_code=409, detail="Intervalul este deja ocupat!")
        raise HTTPException(status_code=500, detail=str(e))

@app.patch("/rezervari/{rezervare_id}/checkout")
def early_checkout(rezervare_id: str):
    try:
        now = datetime.now(timezone.utc).isoformat()
        r = supabase.table('rezervari').update({"ora_sfarsit": now, "status": "finalizata"}).eq('rezervare_id', rezervare_id).execute()
        if r.data: return r.data[0]
        raise HTTPException(404, "Nu există")
    except Exception as e: raise HTTPException(500, str(e))

@app.patch("/rezervari/{rezervare_id}/anulare")
def anuleaza_rezervare(rezervare_id: str, user = Depends(get_current_user)):
    try:
        # 1. Verificăm dacă rezervarea există și aparține utilizatorului
        rez_check = supabase.table('rezervari').select('*').eq('rezervare_id', rezervare_id).eq('user_id', user.id).execute()
        
        if not rez_check.data:
            raise HTTPException(status_code=404, detail="Rezervarea nu a fost găsită sau nu îți aparține.")
            
        rezervare_db = rez_check.data[0]
        
        # 2. Verificăm dacă e deja anulată sau finalizată
        if rezervare_db['status'] != 'activa':
            raise HTTPException(status_code=400, detail="Doar rezervările active pot fi anulate.")
            
        # (Aici în viitor poți adăuga logica de < 60 min pentru penalizare financiară)
        
        # 3. Actualizăm statusul în baza de date
        response = supabase.table('rezervari').update({"status": "anulata"}).eq('rezervare_id', rezervare_id).execute()
        
        if response.data:
            rezultat = response.data[0]
            rezultat['client_ref'] = user.email
            return rezultat
            
        raise HTTPException(status_code=500, detail="Eroare la anularea rezervării.")
        
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- D. ISTORIC (SECURIZE CU AUTH) ---

@app.get("/rezervari", response_model=List[RezervareResponse], summary="Istoricul Meu")
def get_rezervari_mele(user = Depends(get_current_user)):
    """
    Returnează doar rezervările utilizatorului logat curent.
    Se folosește token-ul pentru identificare, nu un parametru URL.
    """
    try:
        response = supabase.table('rezervari').select('*')\
            .eq('user_id', user.id)\
            .order('ora_start', desc=True)\
            .execute()
        
        # Facem compatibilitate cu frontend-ul dacă se așteaptă la client_ref
        data = response.data
        for item in data:
            if 'client_ref' not in item or item['client_ref'] is None:
                item['client_ref'] = user.email
                
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/spalatorii/{spalatorie_id}/rezervari", response_model=List[RezervareResponse], summary="Admin Spălătorie")
def get_rezervari_spalatorie(
    spalatorie_id: str,
    doar_active: bool = Query(False)
):
    # Această rută va fi securizată ulterior pentru OWNER
    try:
        query = supabase.table('rezervari').select('*').eq('spalatorie_id', spalatorie_id)
        if doar_active:
            now = datetime.now(timezone.utc).isoformat()
            query = query.eq('status', 'activa').gte('ora_sfarsit', now)
            
        response = query.order('ora_start', desc=True).execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
# Aceasta este funcția pe care LLM-ul o va "vedea" și folosi
def cauta_spalatorii_libere(lat: float, lon: float, raza_km: float = 5.0, durata_min: int = 30):
    """
    Caută spălătorii auto cu boxe libere în apropierea utilizatorului.
    Folosește această funcție când utilizatorul întreabă de spălătorii disponibile.
    """
    # Aici apelăm efectiv funcția ta deja existentă din mainV3!
    rezultate = get_spalatorii_apropiate_disponibile(lat, lon, raza_km, durata_min)
    
    # Formatăm rezultatul ca string/JSON simplificat pentru ca LLM-ul să-l înțeleagă ușor
    if not rezultate:
        return "Nu am găsit nicio spălătorie liberă în acea zonă."
    
    raspuns = "Am găsit următoarele spălătorii libere:\n"
    for sp in rezultate:
        raspuns += f"- {sp['nume']} la {sp['distanta_km']:.1f} km. Are {len(sp['boxe_libere'])} boxe libere.\n"
    return raspuns

# --- E. CHAT CU AI (GEMINI) ---
class ChatRequest(BaseModel):
    mesaj: str
    lat: float
    lon: float

@app.post("/api/chat")
def chat_cu_ai(request: ChatRequest, user = Depends(get_current_user)):
    # 1. Definim uneltele INCLUSE în rută, ca să aibă acces la obiectul `user`
    
    def cauta_spalatorii(raza_km: float = 5.0, durata_min: int = 30) -> str:
        """Caută spălătorii și returnează ID-urile boxelor libere. Apelează asta prima dată când userul vrea să rezerve."""
        rezultate = get_spalatorii_apropiate_disponibile(request.lat, request.lon, raza_km, durata_min)
        if not rezultate: return "Nu am găsit nicio spălătorie liberă."
        
        raspuns = "Spălătorii găsite:\n"
        for sp in rezultate:
            raspuns += f"- {sp['nume']}\n"
            for b in sp['boxe_libere']:
                raspuns += f"  * Boxa: {b['nume_boxa']} (ID Boxa: {b['boxa_id']}) - Preț: {b['pret_rezervare_lei']} RON\n"
        return raspuns

    def rezerva_boxa(boxa_id: str, durata_minute: int) -> str:
        """Rezervă o boxă pentru utilizator. Necesită boxa_id (obținut din căutare) și durata în minute."""
        try:
            # Aflăm spălătoria de care aparține boxa
            boxa_info = supabase.table('boxe').select('spalatorie_id').eq('boxa_id', boxa_id).execute()
            if not boxa_info.data: return "Eroare: Boxa nu există."
            
            real_spalatorie_id = boxa_info.data[0]['spalatorie_id']
            start = datetime.now(timezone.utc)
            sfarsit = start + timedelta(minutes=durata_minute)
            
            data_insert = {
                "boxa_id": boxa_id,
                "spalatorie_id": real_spalatorie_id,
                "ora_start": start.isoformat(),
                "ora_sfarsit": sfarsit.isoformat(),
                "user_id": user.id, # <-- Aici e magia! Folosim ID-ul userului logat
                "status": "activa"
            }
            res = supabase.table('rezervari').insert(data_insert).execute()
            if res.data:
                return f"Rezervare creată cu succes! ID-ul rezervării este: {res.data[0]['rezervare_id']}"
            return "Eroare la crearea rezervării în baza de date."
        except Exception as e:
            return f"Eroare: {str(e)}"

    def anuleaza_rezervare_chat(rezervare_id: str) -> str:
        """Anulează o rezervare activă a utilizatorului. Cere-i utilizatorului ID-ul rezervării dacă nu îl știi."""
        try:
            # Securitate: Verificăm dacă rezervarea chiar îi aparține acestui user
            rez_check = supabase.table('rezervari').select('*').eq('rezervare_id', rezervare_id).eq('user_id', user.id).execute()
            if not rez_check.data: return "Eroare: Rezervarea nu a fost găsită sau nu îți aparține."
            if rez_check.data[0]['status'] != 'activa': return "Eroare: Rezervarea nu mai este activă."
            
            res = supabase.table('rezervari').update({"status": "anulata"}).eq('rezervare_id', rezervare_id).execute()
            if res.data: return "Rezervarea a fost anulată cu succes."
            return "Eroare la anulare."
        except Exception as e:
            return f"Eroare: {str(e)}"

    # 2. Configurăm Gemini cu aceste 3 unelte super-puternice
    try:
        model = genai.GenerativeModel(
            model_name='gemini-2.5-pro',
            tools=[cauta_spalatorii, rezerva_boxa, anuleaza_rezervare_chat],
            system_instruction="Ești asistentul virtual QuickWash. Ajută utilizatorul să găsească spălătorii, să rezerve boxe și să anuleze rezervări. Dacă utilizatorul vrea să rezerve, folosește întâi `cauta_spalatorii` ca să afli `boxa_id`, apoi întreabă-l cât timp dorește, și la final apelează `rezerva_boxa`. Fii scurt, politicos și direct."
        )

        chat = model.start_chat(enable_automatic_function_calling=True)
        context_mesaj = f"(User ID: {user.id}, Locație curentă lat: {request.lat}, lon: {request.lon}). Mesaj user: {request.mesaj}"
        
        response = chat.send_message(context_mesaj)

        return {"raspuns_ai": response.text}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)