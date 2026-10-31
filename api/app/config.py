from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    # .env holds real third-party keys; .env.local (generated) overrides Supabase
    # settings for the local stack. Process env vars win over both.
    model_config = SettingsConfigDict(
        env_file=(ROOT / ".env", ROOT / ".env.local"),
        extra="ignore",
    )

    anthropic_api_key: str = ""
    sarvam_api_key: str = ""
    resend_api_key: str = ""
    resend_from: str = "onboarding@resend.dev"
    demo_customer_email: str = ""
    email_mode: Literal["resend", "log"] = "resend"

    supabase_url: str
    supabase_publishable_key: str = ""
    supabase_secret_key: str
    supabase_jwks_url: str
    database_url: str

    paytm_mode: Literal["mock", "staging", "production"] = "mock"
    paytm_mid: str = ""
    paytm_merchant_key: str = ""
    paytm_website: str = "WEBSTAGING"
    paytm_mock_key: str = "HisaabMockKey#01"
    paytm_mock_mid: str = "HISAAB00MOCK0000001"
    # The shop's own UPI ID. In mock mode, invoice QR codes and the pay page's "Pay with UPI app" button
    # send real money straight to it (no Paytm notification, so the merchant marks those invoices paid).
    merchant_upi_id: str = ""
    merchant_upi_name: str = ""

    # Reminder calls through a Sarvam voice agent on the merchant's Twilio connection.
    # sarvam = real calls; mock = pretend the customer picked up and promised to pay; log = record only.
    call_mode: Literal["sarvam", "mock", "log"] = "mock"
    sarvam_agents_api_key: str = ""  # Voice Agents key (Settings -> API Key), not the STT/TTS key
    sarvam_org_id: str = ""
    sarvam_workspace_id: str = ""
    sarvam_app_id: str = ""
    sarvam_app_version: int = 1
    sarvam_connection_id: str = ""
    sarvam_agent_phone: str = ""  # the Twilio number the agent calls from, E.164
    sarvam_call_language: str = ""  # e.g. Hindi; empty keeps the agent's default
    sarvam_webhook_secret: str = ""  # falls back to CRON_SECRET
    # Twilio trial accounts only reach verified numbers: every call goes here instead when set.
    demo_customer_phone: str = ""
    # Seeded customers given real phones for the call demo: "Sharma Traders=+91..., Priya Caterers=+91..."
    demo_call_phones: str = ""
    call_after_overdue_days: int = 2  # automatic call this many days after the due went overdue
    call_window_ist: tuple[int, int] = (9, 20)  # automatic calls only between these IST hours

    public_api_base: str = "http://localhost:8000"
    public_web_base: str = "http://localhost:3000"
    web_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    cron_secret: str = ""
    run_scheduler: bool = False

    agent_model: str = "claude-haiku-5-5"
    agent_fallback_model: str = "claude-sonnet-5-5"
    agent_warmup: bool = True
    ai_descriptions: bool = True

    demo_merchant_email: str = "demo@hisaabbolo.test"
    demo_merchant_password: str = "hisaab-demo-2026"

    @property
    def mock_mode(self) -> bool:
        return self.paytm_mode == "mock"

    @property
    def paytm_key(self) -> str:
        """Key used to sign requests and verify webhooks for the active mode."""
        return self.paytm_mock_key if self.mock_mode else self.paytm_merchant_key

    @property
    def call_webhook_secret(self) -> str:
        return self.sarvam_webhook_secret or self.cron_secret

    @property
    def paytm_mid_active(self) -> str:
        return self.paytm_mock_mid if self.mock_mode else self.paytm_mid


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
