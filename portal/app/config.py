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

    # Super-admin access key for /admin pages (set in SaaS)
    ADMIN_KEY: str = ""

    # SSO signing secret used to grant short-lived access to the SMS app.
    # Set the same value on BOTH Portal + SMS services in Render.
    # If empty, falls back to SECRET_KEY (not recommended for SaaS).
    SSO_SHARED_SECRET: str = ""

    # Security
    ALLOWED_HOSTS: str = ""  # comma-separated, e.g. calendo-portal.onrender.com,calendo-sms.onrender.com
    SESSION_MAX_AGE_SECONDS: int = 60 * 60 * 12  # 12 hours
    COOKIE_SECURE: bool = True  # should be True in production (HTTPS)
    COOKIE_SAMESITE: str = "lax"  # lax|strict|none

    # Admin-key bootstrap via query string is risky. Disable by default.
    ALLOW_ADMIN_KEY_QUERY: bool = False

    # ------------------------------------
    # Cloudflare Turnstile (bot protection)
    # ------------------------------------
    # When enabled, login can require a Turnstile token (anti-bot).
    # IMPORTANT:
    # - If TURNSTILE_ENABLED=true but keys are missing, the app will NOT enforce Turnstile
    #   (to avoid accidental lockouts) and will log a warning.
    TURNSTILE_ENABLED: bool = False
    TURNSTILE_SITE_KEY: str = ""     # public key (safe to expose in HTML)
    TURNSTILE_SECRET_KEY: str = ""   # secret key (server-side only)
    TURNSTILE_TIMEOUT_SECONDS: int = 5


settings = Settings()
