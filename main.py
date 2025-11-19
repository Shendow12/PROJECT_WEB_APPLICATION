import os
from fastapi import FastAPI, HTTPException, Query, Body, status
from pydantic import BaseModel
from typing import List, Optional
from dotenv import load_dotenv
from supabase import create_client, Client

# --- 1. Configurare ---
load_dotenv()

app = FastAPI(
    title="QuickWash API",
    description="Backend actualizat: Filtrare disponibilitate & Management Boxe."
)

SUPABASE_URL: str = os.environ.get("SUPABASE_URL")
SUPABASE_KEY: str = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Verifică fișierul .env!")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ==========================================
# 2. MODELE DE DATE (Schemas)
# ==========================================

# --- SPĂLĂTORII ---
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
    distanta_km: float

# --- BOXE ---
class BoxaBase(BaseModel):
    nume_boxa: str
    # Definim valorile aici, dar pot fi suprascrise
    pret_rezervare_lei: float = 15.0 
    timp_rezervare_minute: int = 60
    # Implicit este liberă la creare, conform cerinței
    is_available: bool = True 

class BoxaCreate(BoxaBase):
    spalatorie_id: str 

class BoxaUpdate(BaseModel):
    # La update, putem modifica ORICE, inclusiv disponibilitatea
    nume_boxa: Optional[str] = None
    pret_rezervare_lei: Optional[float] = None
    timp_rezervare_minute: Optional[int] = None
    is_available: Optional[bool] = None

class BoxaResponse(BoxaBase):
    boxa_id: str
    spalatorie_id: str

# ==========================================
# 3. RUTE API
# ==========================================

@app.get("/", summary="Health Check")
def read_root():
    return {"status": "QuickWash API v2 este live!"}

# --- A. SPĂLĂTORII ---

@app.post("/spalatorii", status_code=status.HTTP_201_CREATED, summary="Adaugă Spălătorie")
def add_spalatorie(spalatorie: SpalatorieCreate = Body(...)):
    """
    Adaugă o spălătorie nouă.
    """
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

@app.get("/spalatorii-apropiate", response_model=List[SpalatorieResponse], summary="Căutare Smart")
def get_spalatorii_apropiate(
    lat: float,
    lon: float,
    raza_km: float = 5.0
):
    """
    Returnează spălătoriile din rază CARE AU CEL PUȚIN O BOXĂ DISPONIBILĂ.
    """
    try:
        response = supabase.rpc(
            'gaseste_apropiate',
            {'user_lat': lat, 'user_lon': lon, 'raza_km': raza_km}
        ).execute()

        if response.data:
            return [SpalatorieResponse(**item) for item in response.data]
        return []
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- B. BOXE (CRUD) ---

@app.post("/boxe", status_code=status.HTTP_201_CREATED, response_model=BoxaResponse, summary="Adaugă Boxă")
def adauga_boxa(boxa: BoxaCreate):
    """
    Adaugă o boxă la o spălătorie.
    - Primește preț și timp (sau folosește default).
    - Default 'is_available' este True.
    """
    try:
        response = supabase.table('boxe').insert(boxa.model_dump()).execute()
        if response.data:
            return response.data[0]
        raise HTTPException(status_code=500, detail="Eroare la creare.")
    except Exception as e:
        if "foreign key" in str(e):
            raise HTTPException(status_code=404, detail="Spălătoria nu există.")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/spalatorii/{spalatorie_id}/boxe", response_model=List[BoxaResponse], summary="Vezi Boxele")
def get_boxe_spalatorie(spalatorie_id: str):
    """
    Listează toate boxele unei spălătorii.
    Include parametrul 'is_available' pentru a vedea dacă sunt libere.
    """
    try:
        response = supabase.table('boxe').select('*').eq('spalatorie_id', spalatorie_id).execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.patch("/boxe/{boxa_id}", response_model=BoxaResponse, summary="Update Boxă")
def update_boxa(boxa_id: str, boxa_update: BoxaUpdate):
    """
    Modifică o boxă.
    - Poți schimba prețul, timpul.
    - Poți schimba statusul 'is_available' (ex: din True în False dacă s-a ocupat).
    """
    try:
        update_data = boxa_update.model_dump(exclude_unset=True)
        if not update_data:
            raise HTTPException(status_code=400, detail="Fără date de update.")

        response = supabase.table('boxe').update(update_data).eq('boxa_id', boxa_id).execute()
        
        if response.data:
            return response.data[0]
        raise HTTPException(status_code=404, detail="Boxa nu există.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/boxe/{boxa_id}", status_code=status.HTTP_204_NO_CONTENT)
def sterge_boxa(boxa_id: str):
    try:
        supabase.table('boxe').delete().eq('boxa_id', boxa_id).execute()
        return None
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)