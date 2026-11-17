from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from apscheduler.schedulers.background import BackgroundScheduler

db = SQLAlchemy(session_options={'expire_on_commit': False})
login_manager = LoginManager()
login_manager.login_view = 'login'
scheduler = BackgroundScheduler(daemon=True, timezone='UTC')