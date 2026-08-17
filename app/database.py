import turso_serverless as typeshit
from app.config import Settings

def connect_database():
    settings = Settings()
    return typeshit.connect(settings.turso_database_url, auth_token=settings.turso_auth_token)