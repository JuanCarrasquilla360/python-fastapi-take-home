from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://postgres:postgres@localhost:5433/coffee"
    # Connections held by each API process. Requests beyond this wait for a free one for up
    # to `db_pool_timeout` seconds and then fail fast instead of piling up (see NOTES.md).
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10
    db_pool_timeout: float = 5.0


settings = Settings()
