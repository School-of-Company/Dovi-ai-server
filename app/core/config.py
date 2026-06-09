from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "Dovi AI Server"
    debug: bool = False

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


def get_settings() -> Settings:
    return Settings()
