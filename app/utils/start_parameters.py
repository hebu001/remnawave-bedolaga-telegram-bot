"""Reserved Telegram ``/start`` parameters shared across handlers and middleware."""

PARTNER_MENU_START_PARAMETER = 'partner'
PENDING_PARTNER_MENU_KEY = 'pending_partner_menu'


def is_partner_menu_start_parameter(value: str | None) -> bool:
    """Return whether *value* explicitly requests the partner callback section."""
    return value == PARTNER_MENU_START_PARAMETER
