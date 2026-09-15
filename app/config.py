from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# all environment variables
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    turso_database_url: str
    turso_auth_token: str
    eo_database_url: str | None = None
    eo_auth_token: str | None = None
    redis_url: str = "redis://localhost:6379/0" # uses local by default if .env is not set
    openai_api_key: str
    nasa_api_key: str
    # Private, tailnet-only Slicer archiver. These are intentionally not AWS keys.
    media_archiver_url: str | None = None
    media_archiver_api_token: SecretStr | None = None
    pictures_internal_token: SecretStr | None = None
