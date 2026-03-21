from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: str = ""
    whatsapp_verify_token: str = "my_verify_token"

    openai_api_key: str = ""
    anthropic_api_key: str = ""

    fatsecret_consumer_key: str = ""
    fatsecret_consumer_secret: str = ""

    database_url: str = "sqlite:///./calorie_bot.db"
    app_base_url: str = "http://localhost:8000"
    admin_secret: str = ""

    class Config:
        env_file = ".env"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
