from pydantic_settings import BaseSettings, SettingsConfigDict

# all environment variables
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    turso_database_url: str
    turso_auth_token: str
    redis_url: str = "redis://localhost:6379/0" # uses local by default if .env is not set
    openai_api_key: str
    nasa_api_key: str
