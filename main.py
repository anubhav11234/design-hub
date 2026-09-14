from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from passlib.context import CryptContext
import jwt
from datetime import datetime, timedelta
import uuid
import os
import trimesh
import database

# --- Setup & Config ---
app = FastAPI(title="Design Hub API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = "cad_storage"
os.makedirs(UPLOAD_DIR, exist_ok=True)
MAX_FILE_SIZE = 50 * 1024 * 1024

# Create database tables
database.Base.metadata.create_all(bind=database.engine)

# --- Authentication Settings ---
SECRET_KEY = "your-super-secret-key-change-this-in-production"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 1440 # 24 hours

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# --- Dependencies ---
def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except jwt.PyJWTError:
        raise credentials_exception
    user = db.query(database.User).filter(database.User.username == username).first()
    if user is None:
        raise credentials_exception
    return user

# --- Auth Endpoints ---
@app.post("/api/register")
def register_user(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    db_user = db.query(database.User).filter(database.User.username == username).first()
    if db_user:
        raise HTTPException(status_code=400, detail="Username already registered")
    
    # FIX: Truncate password to prevent bcrypt 72-byte limit crash
    safe_password = password[:72]
    hashed_password = pwd_context.hash(safe_password)
    
    new_user = database.User(username=username, hashed_password=hashed_password)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return {"message": "User created successfully"}

@app.post("/token")
def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(database.User).filter(database.User.username == form_data.username).first()
    
    # FIX: Truncate login password to match the registration behavior
    safe_login_password = form_data.password[:72]
    
    if not user or not pwd_context.verify(safe_login_password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = jwt.encode(
        {"sub": user.username, "exp": datetime.utcnow() + access_token_expires},
        SECRET_KEY,
        algorithm=ALGORITHM
    )
    return {"access_token": access_token, "token_type": "bearer"}

# --- Upload Endpoint (Requires Login) ---
@app.post("/api/upload")
async def upload_model(
    file: UploadFile = File(...), 
    is_public: bool = Form(False),
    title: str = Form("Untitled"),
    description: str = Form(""),
    current_user: database.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not file.filename.lower().endswith('.stl'):
        raise HTTPException(status_code=400, detail="Only .STL files are allowed.")

    project_id = str(uuid.uuid4())[:8]
    safe_filename = f"{project_id}.stl"
    
    # FIX 1: Enforce forward slash path for Linux compatibility
    file_location = f"{UPLOAD_DIR}/{safe_filename}"
    
    # FIX 2: Safely read the entire file into memory before saving to avoid 0-byte bug
    content = await file.read()
    
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large. Maximum size is 50MB.")

    # Write the actual content to disk
    with open(file_location, "wb") as buffer:
        buffer.write(content)

    try:
        # FIX 3: Force Trimesh to strictly parse it as an STL mesh
        mesh = trimesh.load(file_location, file_type='stl', force='mesh')
        
        if mesh.is_empty:
            raise ValueError("Empty geometry")
            
        is_watertight = mesh.is_watertight
        volume = str(round(mesh.volume, 2)) if mesh.is_volume else "N/A"
        bbox = " x ".join([str(round(dim, 2)) for dim in mesh.bounding_box.extents])
        status_msg = "DFM Passed" if is_watertight else "DFM Warning: Mesh not watertight"
        
    except Exception as e:
        if os.path.exists(file_location):
            os.remove(file_location)
        raise HTTPException(status_code=422, detail="Invalid STL content. File rejected and deleted.")

    # Save to database
    db_model = database.Model(
        id=project_id,
        filename=file.filename,
        title=title,
        description=description,
        is_public=is_public,
        status=status_msg,
        volume_mm3=volume,
        bbox=bbox,
        file_path=file_location,
        owner_id=current_user.id
    )
    db.add(db_model)
    db.commit()
    db.refresh(db_model)
    
    return {"message": "Upload secure & complete", "project_id": project_id}

# --- Feed, Portfolio, and Delete Endpoints ---
@app.get("/api/feed")
def get_global_feed(db: Session = Depends(get_db)):
    public_models = db.query(database.Model).filter(database.Model.is_public == True).all()
    result = []
    for m in public_models:
        owner = db.query(database.User).filter(database.User.id == m.owner_id).first()
        result.append({
            "id": m.id,
            "title": m.title,
            "description": m.description,
            "owner_name": owner.username if owner else "Unknown",
            "volume_mm3": m.volume_mm3,
            "bbox": m.bbox,
            "status": m.status,
            "is_public": m.is_public
        })
    return {"models": result}

@app.get("/api/portfolio")
def get_user_portfolio(current_user: database.User = Depends(get_current_user), db: Session = Depends(get_db)):
    user_models = db.query(database.Model).filter(database.Model.owner_id == current_user.id).all()
    result = []
    for m in user_models:
        result.append({
            "id": m.id,
            "title": m.title,
            "description": m.description,
            "owner_name": current_user.username,
            "volume_mm3": m.volume_mm3,
            "bbox": m.bbox,
            "status": m.status,
            "is_public": m.is_public
        })
    return {"models": result}

@app.get("/api/model/{project_id}")
def get_model_details(project_id: str, db: Session = Depends(get_db)):
    model = db.query(database.Model).filter(database.Model.id == project_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    if not model.is_public:
        raise HTTPException(status_code=403, detail="Model is private")
    return {"model": model}

@app.get("/api/download/{project_id}")
def download_model(project_id: str, db: Session = Depends(get_db)):
    from fastapi.responses import FileResponse
    model = db.query(database.Model).filter(database.Model.id == project_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="File not found.")
        
    # FIX 4: Protect against ephemeral storage wipes on Render
    if not os.path.exists(model.file_path):
        raise HTTPException(status_code=404, detail="File missing from server. It was likely cleared by temporary hosting. Please delete this record and re-upload.")
        
    # Security: If the model is private, the user shouldn't be able to download it blindly via URL
    if not model.is_public:
        raise HTTPException(status_code=403, detail="This file is private.")
        
    return FileResponse(path=model.file_path, filename=model.filename)

@app.delete("/api/model/{project_id}")
def delete_model(project_id: str, current_user: database.User = Depends(get_current_user), db: Session = Depends(get_db)):
    model = db.query(database.Model).filter(database.Model.id == project_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    if model.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this model")
    
    # Remove file from disk
    if os.path.exists(model.file_path):
        os.remove(model.file_path)
        
    db.delete(model)
    db.commit()
    return {"message": "Model deleted"}

@app.get("/api/search")
def search_models(q: str, db: Session = Depends(get_db)):
    search_pattern = f"%{q}%"
    # Join with User table to search by both model title and owner's username
    results = db.query(database.Model, database.User).join(database.User, database.Model.owner_id == database.User.id)\
        .filter(database.Model.is_public == True)\
        .filter((database.Model.title.ilike(search_pattern)) | (database.User.username.ilike(search_pattern)))\
        .all()
    
    output = []
    for m, u in results:
        output.append({
            "id": m.id,
            "title": m.title,
            "description": m.description,
            "owner_name": u.username,
            "volume_mm3": m.volume_mm3,
            "bbox": m.bbox,
            "status": m.status,
            "is_public": m.is_public
        })
    return {"models": output}

@app.get("/api/user/{username}")
def get_public_profile(username: str, db: Session = Depends(get_db)):
    user = db.query(database.User).filter(database.User.username == username).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    public_models = db.query(database.Model).filter(
        database.Model.owner_id == user.id, 
        database.Model.is_public == True
    ).all()
    
    result = []
    for m in public_models:
        result.append({
            "id": m.id,
            "title": m.title,
            "description": m.description,
            "owner_name": user.username,
            "volume_mm3": m.volume_mm3,
            "bbox": m.bbox,
            "status": m.status,
            "is_public": m.is_public
        })
    return {"models": result}