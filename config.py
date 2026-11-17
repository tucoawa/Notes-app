import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, 'instance')
os.makedirs(INSTANCE_DIR, exist_ok=True)


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-change-me')
    DB_PATH = os.environ.get('NOTES_DB_PATH', os.path.join(INSTANCE_DIR, 'notes.db'))
    SQLALCHEMY_DATABASE_URI = 'sqlite:///' + DB_PATH
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    UPLOAD_FOLDER = os.environ.get(
        'NOTES_UPLOAD_FOLDER',
        '/storage/emulated/0/Documents/apps/notes_app/uploads'
    )
    MAX_CONTENT_LENGTH = 128 * 1024 * 1024  # 128 MB
    NTFY_URL = 'http://localhost:8080/Stef'

    # Single-user login
    APP_USERNAME = os.environ.get('APP_USERNAME', 'stef')
    APP_PASSWORD = os.environ.get('APP_PASSWORD', 'ssss')
    
    APP_BASE_URL = os.environ.get('APP_BASE_URL', 'http://localhost:5000')
    
    ASSET_VERSION = os.environ.get('ASSET_VERSION', 'dev300')
    
    SQLALCHEMY_ECHO = False