from sqlalchemy import create_engine, Column, Integer, String, Boolean, Float, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship

SQLALCHEMY_DATABASE_URL = "sqlite:///./designhub.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    models = relationship("Model", back_populates="owner")

class Model(Base):
    __tablename__ = "models"
    id = Column(String, primary_key=True, index=True) # Using UUID string
    filename = Column(String)
    title = Column(String, default="Untitled")
    description = Column(String, default="")
    is_public = Column(Boolean, default=False)
    status = Column(String)
    volume_mm3 = Column(String)
    bbox = Column(String)
    file_path = Column(String)
    owner_id = Column(Integer, ForeignKey("users.id"))
    
    owner = relationship("User", back_populates="models")
    
# We will add Comments and Upvotes later as we expand the feed