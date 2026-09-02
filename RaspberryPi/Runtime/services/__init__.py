"""Optional hardware and external-service adapters for SafeNest actions."""

from .buzzer import BuzzerProtocol, MockBuzzer, create_buzzer_from_env
from .emergency import EmergencyActionError, EmergencyActionService
from .sms_service import NaverSensSMSProvider, SMSProviderError

__all__ = [
    "BuzzerProtocol",
    "EmergencyActionError",
    "EmergencyActionService",
    "MockBuzzer",
    "NaverSensSMSProvider",
    "SMSProviderError",
    "create_buzzer_from_env",
]
