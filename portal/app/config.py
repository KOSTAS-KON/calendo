from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    DATABASE_URL: str = "sqlite:///./therapy.db"
    SECRET_KEY: str = "change-me"

    # Offline license verification (Ed25519 public key)
    #
    # - This must be the PUBLIC key (32 bytes) encoded as base64 or base64url.
    # - Keep the PRIVATE key only on your machine (issuer) and NEVER ship it to clients.
    #
    # If empty, activation codes cannot be verified and the app falls back to
    # the manual trial controls.
    LICENSE_PUBLIC_KEY: str = ""

    # Cross-app navigation (SaaS deployments)
    # If set to a full URL, the Portal will link out to the SMS Calendar service.
    # If left empty, defaults to /sms/ (works for the on-prem Docker gateway).
    SMS_APP_URL: str = ""

    # Internal service-to-service authentication (Portal <-> SMS)
    # Set this in Render for BOTH services. Keep it secret.
    INTERNAL_API_KEY: str = ""

settings = Settings()
