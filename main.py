import os
from fastapi import FastAPI, HTTPException, Query, Body, status
from pydantic import BaseModel
from typing import List, Optional
from dotenv import load_dotenv
from supabase import create_client, Client
from datetime import datetime, timedelta, timezone
from itertools import groupby

# --- 1. Configurare & Conexiune ---
load_dotenv()

app = FastAPI(
    title="QuickWash API V3",
    description="Backend complet: Spălătorii, Boxe (CRUD Nested) și Rezervări Smart."
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
    program_functionare: Optional[str] = "00:00 - 24:00"
    latitudine: float
    longitudine: float

class SpalatorieResponse(BaseModel):
    id: str
    nume: str
    adresa: Optional[str]
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
    # Luăm spalatorie_id din URL, nu din body
    pass

class BoxaUpdate(BaseModel):
    nume_boxa: Optional[str] = None
    pret_rezervare_lei: Optional[float] = None
    timp_rezervare_minute: Optional[int] = None
    is_available: Optional[bool] = None

class BoxaResponse(BoxaBase):
    boxa_id: str
    spalatorie_id: str

# --- Rezervări ---
class RezervareCreate(BaseModel):
    boxa_id: str
    durata_minute: int 
    client_ref: Optional[str] = None

class RezervareResponse(BaseModel):
    rezervare_id: str
    boxa_id: str
    spalatorie_id: str # Important pentru istoric
    ora_start: datetime
    ora_sfarsit: datetime
    status: str
    client_ref: Optional[str]

# Modele pentru disponibilitate
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
    distanta_km: Optional[float] = None
    boxe_libere: List[BoxaDisponibila]

# --- HELPER: Algoritmul de calculare a "găurilor" ---
def calculeaza_gaps(start_window: datetime, end_window: datetime, rezervari: list, durata_minima_minute: int):
    gaps = []
    current_time = start_window

    # Sortăm rezervările cronologic ca să putem calcula spațiile dintre ele
    # Rezervările vin din DB, ne asigurăm că sunt datetime objects
    rezervari_sorted = sorted(
        rezervari, 
        key=lambda x: datetime.fromisoformat(x['ora_start'])
    )

    for res in rezervari_sorted:
        res_start = datetime.fromisoformat(res['ora_start'])
        res_end = datetime.fromisoformat(res['ora_sfarsit'])

        # Dacă rezervarea începe DUPĂ momentul curent, avem o gaură!
        if res_start > current_time:
            gap_duration = (res_start - current_time).total_seconds() / 60
            if gap_duration >= durata_minima_minute:
                gaps.append({
                    "start": current_time,
                    "end": res_start,
                    "minute_disponibile": int(gap_duration)
                })
        
        # Mutăm cursorul de timp după rezervarea curentă
        # (Dar ne asigurăm că nu mergem înapoi în timp, în caz de suprapuneri ciudate)
        if res_end > current_time:
            current_time = res_end

    # Verificăm spațiul de după ultima rezervare până la finalul ferestrei
    if current_time < end_window:
        gap_duration = (end_window - current_time).total_seconds() / 60
        if gap_duration >= durata_minima_minute:
            gaps.append({
                "start": current_time,
                "end": end_window,
                "minute_disponibile": int(gap_duration)
            })
            
    return gaps


# ==========================================
# 3. RUTE API (Endpoints)
# ==========================================

@app.get("/", summary="Health Check")
def read_root():
    return {"status": "QuickWash API este live!"}

# ---------------------------
# A. SPĂLĂTORII
# ---------------------------

@app.post("/spalatorii", status_code=status.HTTP_201_CREATED, summary="Adaugă Spălătorie")
def add_spalatorie(spalatorie: SpalatorieCreate = Body(...)):
    try:
        data = spalatorie.model_dump()
        response = supabase.table('spalatorii').insert({
            "nume": data['nume'],
            "adresa": data['adresa'],
            "program_functionare": data['program_functionare'],
            "locatie": f"SRID=4326;POINT({data['longitudine']} {data['latitudine']})"
        }).execute()

        if response.data:
            return response.data[0]
        raise HTTPException(status_code=500, detail="Eroare la salvare.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/spalatorii-apropiate", response_model=List[SpalatorieResponse], summary="Căutare Geo")
def get_spalatorii_apropiate(
    lat: float = Query(..., description="Lat user"),
    lon: float = Query(..., description="Lon user"),
    raza_km: float = Query(5.0, description="Raza în km")
):
    try:
        response = supabase.rpc(
            'gaseste_apropiate',
            {'user_lat': lat, 'user_lon': lon, 'raza_km': raza_km}
        ).execute()
        
        if response.data:
            return [SpalatorieResponse(**item) for item in response.data]
        return []
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Eroare server: {str(e)}")


# ---------------------------
# B. BOXE (Nested Routes)
# ---------------------------

@app.get("/spalatorii/{spalatorie_id}/boxe", response_model=List[BoxaResponse])
def get_boxe_spalatorie(spalatorie_id: str):
    try:
        response = supabase.table('boxe').select('*').eq('spalatorie_id', spalatorie_id).execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/spalatorii/{spalatorie_id}/boxe/{boxa_id}", response_model=BoxaResponse)
def get_single_boxa(spalatorie_id: str, boxa_id: str):
    try:
        response = supabase.table('boxe').select('*')\
            .eq('boxa_id', boxa_id)\
            .eq('spalatorie_id', spalatorie_id)\
            .execute()
        
        if response.data:
            return response.data[0]
        raise HTTPException(status_code=404, detail="Boxa nu a fost găsită.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/spalatorii/{spalatorie_id}/boxe", status_code=status.HTTP_201_CREATED, response_model=BoxaResponse)
def adauga_boxa(spalatorie_id: str, boxa: BoxaCreate = Body(...)):
    try:
        insert_data = boxa.model_dump()
        insert_data['spalatorie_id'] = spalatorie_id
        
        response = supabase.table('boxe').insert(insert_data).execute()
        
        if response.data:
            return response.data[0]
        raise HTTPException(status_code=500, detail="Eroare la creare.")
    except Exception as e:
        if "foreign key" in str(e):
            raise HTTPException(status_code=404, detail="Spălătoria nu există.")
        raise HTTPException(status_code=500, detail=str(e))

@app.patch("/spalatorii/{spalatorie_id}/boxe/{boxa_id}", response_model=BoxaResponse)
def update_boxa(spalatorie_id: str, boxa_id: str, boxa_update: BoxaUpdate):
    try:
        update_data = boxa_update.model_dump(exclude_unset=True)
        if not update_data:
            raise HTTPException(status_code=400, detail="Fără date de update.")

        response = supabase.table('boxe').update(update_data)\
            .eq('boxa_id', boxa_id)\
            .eq('spalatorie_id', spalatorie_id)\
            .execute()
        
        if response.data:
            return response.data[0]
        raise HTTPException(status_code=404, detail="Boxa nu există.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/spalatorii/{spalatorie_id}/boxe/{boxa_id}", status_code=status.HTTP_204_NO_CONTENT)
def sterge_boxa(spalatorie_id: str, boxa_id: str):
    try:
        response = supabase.table('boxe').delete()\
            .eq('boxa_id', boxa_id)\
            .eq('spalatorie_id', spalatorie_id)\
            .execute()
        if not response.data:
             raise HTTPException(status_code=404, detail="Boxa nu a fost găsită.")
        return None
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------
# C. REZERVĂRI (Smart Logic)
# ---------------------------

@app.post("/rezervari", status_code=status.HTTP_201_CREATED, response_model=RezervareResponse)
def creare_rezervare(rezervare: RezervareCreate):
    """
    Creează o rezervare. 
    Completează automat ID-ul spălătoriei pentru istoric.
    """
    try:
        # 1. Căutăm ID-ul spălătoriei (Părintele boxei)
        boxa_info = supabase.table('boxe').select('spalatorie_id').eq('boxa_id', rezervare.boxa_id).execute()
        
        if not boxa_info.data:
            raise HTTPException(status_code=404, detail="Boxa specificată nu există.")
            
        real_spalatorie_id = boxa_info.data[0]['spalatorie_id']

        # 2. Calculăm timpii
        start = datetime.now(timezone.utc)
        sfarsit = start + timedelta(minutes=rezervare.durata_minute)
        
        # 3. Inserăm cu TOATE datele necesare
        data_insert = {
            "boxa_id": rezervare.boxa_id,
            "spalatorie_id": real_spalatorie_id, # Completat automat
            "ora_start": start.isoformat(),
            "ora_sfarsit": sfarsit.isoformat(),
            "client_ref": rezervare.client_ref,
            "status": "activa"
        }

        response = supabase.table('rezervari').insert(data_insert).execute()
        
        if response.data:
            return response.data[0]
        raise HTTPException(status_code=500, detail="Eroare server.")

    except Exception as e:
        # Prindem eroarea de suprapunere (Exclusion Constraint)
        if "conflicting key" in str(e) or "exclusion constraint" in str(e):
            raise HTTPException(status_code=409, detail="Boxa este deja ocupată în acest moment!")
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/rezervari/{rezervare_id}/checkout", response_model=RezervareResponse)
def early_checkout(rezervare_id: str):
    """
    Eliberează boxa mai devreme.
    """
    try:
        now = datetime.now(timezone.utc).isoformat()
        
        response = supabase.table('rezervari').update({
            "ora_sfarsit": now,
            "status": "finalizata"
        }).eq('rezervare_id', rezervare_id).execute()
        
        if response.data:
            return response.data[0]
        raise HTTPException(status_code=404, detail="Rezervarea nu a fost găsită.")
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/rezervari/active", response_model=List[RezervareResponse])
def get_rezervari_active():
    try:
        response = supabase.table('rezervari').select('*').eq('status', 'activa').execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

#DISPONIBILITATE BOXE ȘI SPĂLĂTORII
@app.get("/spalatorii/{spalatorie_id}/disponibilitate", response_model=List[BoxaDisponibila])
def get_disponibilitate_spalatorie(
    spalatorie_id: str,
    durata_dorita_min: int = Query(30, description="Cât timp vrei să speli?"),
    fereastra_ore: int = Query(2, description="Cât de departe în viitor căutăm?")
):
    """
    Returnează intervalele orare disponibile pentru toate boxele unei spălătorii
    în următoarele X ore (default 2).
    """
    try:
        # 1. Definim fereastra de timp (Acum -> Acum + 2h)
        now = datetime.now(timezone.utc)
        end_window = now + timedelta(hours=fereastra_ore)

        # 2. Luăm toate boxele spălătoriei
        boxe = supabase.table('boxe').select('*').eq('spalatorie_id', spalatorie_id).eq('is_available', True).execute()
        if not boxe.data:
            return [] # Nicio boxă funcțională

        # 3. Luăm toate rezervările ACTIVE din acest interval pentru această spălătorie
        # Filtrăm să se intersecteze cu fereastra noastră
        rezervari = supabase.table('rezervari')\
            .select('boxa_id, ora_start, ora_sfarsit')\
            .eq('spalatorie_id', spalatorie_id)\
            .eq('status', 'activa')\
            .gte('ora_sfarsit', now.isoformat())\
            .lte('ora_start', end_window.isoformat())\
            .execute()
        
        rezervari_list = rezervari.data

        rezultat = []

        # 4. Pentru fiecare boxă, calculăm golurile
        for boxa in boxe.data:
            # Filtrăm rezervările doar pentru boxa curentă
            rezervari_boxa = [r for r in rezervari_list if r['boxa_id'] == boxa['boxa_id']]
            
            gaps = calculeaza_gaps(now, end_window, rezervari_boxa, durata_dorita_min)
            
            if gaps:
                rezultat.append({
                    "boxa_id": boxa['boxa_id'],
                    "nume_boxa": boxa['nume_boxa'],
                    "pret_rezervare_lei": boxa['pret_rezervare_lei'],
                    "intervale": gaps
                })

        return rezultat

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/spalatorii-apropiate/disponibilitate", response_model=List[SpalatorieDisponibilaResponse])
def get_spalatorii_apropiate_disponibile(
    lat: float,
    lon: float,
    raza_km: float = 5.0,
    durata_dorita_min: int = 30
):
    """
    Cea mai complexă rută:
    1. Găsește spălătoriile apropiate.
    2. Pentru fiecare, verifică dacă are MĂCAR O BOXĂ liberă ACUM (sau în curând).
    """
    try:
        # 1. Găsim spălătoriile fizice (Geospatial)
        locatii = supabase.rpc(
            'gaseste_apropiate',
            {'user_lat': lat, 'user_lon': lon, 'raza_km': raza_km}
        ).execute()
        
        if not locatii.data:
            return []

        spalatorii_ids = [s['id'] for s in locatii.data]
        
        # 2. Definim fereastra (Următoarele 2 ore e standardul nostru)
        now = datetime.now(timezone.utc)
        end_window = now + timedelta(hours=2)

        # 3. Luăm BULK toate boxele și rezervările pentru aceste spălătorii
        # (Optimizare: facem 2 query-uri mari în loc de 50 mici)
        boxe_all = supabase.table('boxe').select('*').in_('spalatorie_id', spalatorii_ids).eq('is_available', True).execute()
        
        rezervari_all = supabase.table('rezervari')\
            .select('*')\
            .in_('spalatorie_id', spalatorii_ids)\
            .eq('status', 'activa')\
            .gte('ora_sfarsit', now.isoformat())\
            .lte('ora_start', end_window.isoformat())\
            .execute()

        # 4. Procesăm datele în Python
        rezultat_final = []

        for loc in locatii.data:
            # Găsim boxele acestei locații
            boxe_locatie = [b for b in boxe_all.data if b['spalatorie_id'] == loc['id']]
            boxe_cu_gaps = []

            for boxa in boxe_locatie:
                # Găsim rezervările acestei boxe
                rez_boxa = [r for r in rezervari_all.data if r['boxa_id'] == boxa['boxa_id']]
                
                gaps = calculeaza_gaps(now, end_window, rez_boxa, durata_dorita_min)
                
                if gaps:
                    boxe_cu_gaps.append({
                        "boxa_id": boxa['boxa_id'],
                        "nume_boxa": boxa['nume_boxa'],
                        "pret_rezervare_lei": boxa['pret_rezervare_lei'],
                        "intervale": gaps
                    })
            
            # Adăugăm spălătoria în listă DOAR dacă are boxe disponibile
            if boxe_cu_gaps:
                rezultat_final.append({
                    "spalatorie_id": loc['id'],
                    "nume": loc['nume'],
                    "distanta_km": loc['distanta_km'],
                    "boxe_libere": boxe_cu_gaps
                })

        return rezultat_final

    except Exception as e:
        print(f"Eroare: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# --- Ruta Detaliată: Disponibilitate per Spălătorie ---

@app.get("/spalatorii/{spalatorie_id}/disponibilitate", response_model=List[BoxaDisponibila])
def get_disponibilitate_spalatorie(
    spalatorie_id: str,
    durata_dorita_min: int = Query(30, description="Cât timp vrei să speli?"),
    fereastra_ore: int = Query(2, description="Cât de departe în viitor căutăm?")
):
    """
    Returnează intervalele orare disponibile pentru TOATE boxele unei spălătorii specifice.
    """
    try:
        now = datetime.now(timezone.utc)
        end_window = now + timedelta(hours=fereastra_ore)

        # 1. Luăm boxele active ale spălătoriei
        boxe = supabase.table('boxe').select('*')\
            .eq('spalatorie_id', spalatorie_id)\
            .eq('is_available', True)\
            .execute()
            
        if not boxe.data:
            return []

        # 2. Luăm rezervările active pentru această spălătorie
        rezervari = supabase.table('rezervari')\
            .select('boxa_id, ora_start, ora_sfarsit')\
            .eq('spalatorie_id', spalatorie_id)\
            .eq('status', 'activa')\
            .gte('ora_sfarsit', now.isoformat())\
            .lte('ora_start', end_window.isoformat())\
            .execute()
        
        rezervari_list = rezervari.data
        rezultat = []

        # 3. Calculăm golurile pentru fiecare boxă
        for boxa in boxe.data:
            rez_boxa = [r for r in rezervari_list if r['boxa_id'] == boxa['boxa_id']]
            gaps = calculeaza_gaps(now, end_window, rez_boxa, durata_dorita_min)
            
            if gaps:
                rezultat.append({
                    "boxa_id": boxa['boxa_id'],
                    "nume_boxa": boxa['nume_boxa'],
                    "pret_rezervare_lei": boxa['pret_rezervare_lei'],
                    "intervale": gaps
                })

        return rezultat

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)