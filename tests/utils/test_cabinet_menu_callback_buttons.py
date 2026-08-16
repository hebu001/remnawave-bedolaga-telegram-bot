"""Tests for configurable raw callback buttons in the Cabinet-mode menu."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.cabinet.routes.admin_menu_layout import (
    ButtonConfig,
    RowConfig,
    _build_merged_response,
    _split_update,
    _validate_update_payload,
)
from app.keyboards.inline import _build_cabinet_main_menu_keyboard
from app.utils import button_styles_cache, menu_layout_cache


class _Texts:
    MENU_BUY_SUBSCRIPTION = '💎 Купить подписку'

    @staticmethod
    def t(_key: str, default: str) -> str:
        return default


def _callback_button(**updates) -> ButtonConfig:
    data = {
        'id': 'custom_buy',
        'type': 'callback',
        'style': 'success',
        'icon_custom_emoji_id': '',
        'enabled': True,
        'labels': {'ru': '💎 Купить сейчас'},
        'callback_data': 'menu_buy',
    }
    data.update(updates)
    return ButtonConfig(**data)


def test_callback_button_round_trips_through_layout_storage() -> None:
    rows = [RowConfig(id='actions', max_per_row=1, buttons=[_callback_button()])]

    layout, style_updates = _split_update(rows)

    assert style_updates == {}
    assert layout['custom_buttons']['custom_buy'] == {
        'id': 'custom_buy',
        'type': 'callback',
        'style': 'success',
        'icon_custom_emoji_id': '',
        'enabled': True,
        'labels': {'ru': '💎 Купить сейчас'},
        'callback_data': 'menu_buy',
    }

    merged = _build_merged_response(layout, {})
    restored = merged.rows[0].buttons[0]
    assert restored.type == 'callback'
    assert restored.callback_data == 'menu_buy'
    assert restored.url is None
    assert 'menu_buy' in merged.callback_actions


def test_arbitrary_callback_action_is_rejected_by_api() -> None:
    rows = [
        RowConfig(
            id='unsafe',
            max_per_row=1,
            buttons=[_callback_button(callback_data='admin_panel')],
        )
    ]

    with pytest.raises(HTTPException, match='Unsupported callback action'):
        _validate_update_payload(rows)


def test_cache_accepts_allowed_callback_and_drops_unknown_action() -> None:
    allowed = menu_layout_cache._validate_custom_button(
        'custom_buy',
        {
            'type': 'callback',
            'callback_data': 'menu_buy',
            'labels': {'ru': 'Купить'},
            'style': 'success',
        },
    )
    blocked = menu_layout_cache._validate_custom_button(
        'custom_admin',
        {'type': 'callback', 'callback_data': 'admin_panel'},
    )

    assert allowed is not None
    assert allowed['callback_data'] == 'menu_buy'
    assert blocked is None


def test_legacy_url_button_remains_backward_compatible() -> None:
    cleaned = menu_layout_cache._validate_custom_button(
        'custom_docs',
        {
            'url': 'https://example.com/docs',
            'labels': {'ru': 'Документация'},
            'open_in': 'webapp',
        },
    )

    assert cleaned is not None
    assert cleaned['type'] == 'custom'
    assert cleaned['url'] == 'https://example.com/docs'
    assert cleaned['open_in'] == 'webapp'


def test_cabinet_keyboard_renders_configured_action_as_raw_callback(monkeypatch) -> None:
    layout = {
        'row_1': {'id': 'actions', 'buttons': ['custom_buy'], 'max_per_row': 1},
        'custom_buttons': {
            'custom_buy': {
                'id': 'custom_buy',
                'type': 'callback',
                'callback_data': 'menu_buy',
                'style': 'success',
                'icon_custom_emoji_id': '',
                'enabled': True,
                'labels': {'ru': '💎 Купить сейчас'},
            }
        },
    }
    monkeypatch.setattr(menu_layout_cache, 'get_cached_menu_layout', lambda: layout)
    monkeypatch.setattr(button_styles_cache, 'get_cached_button_styles', dict)

    keyboard = _build_cabinet_main_menu_keyboard(
        'ru',
        _Texts(),
        is_admin=False,
        is_moderator=False,
    )

    button = keyboard.inline_keyboard[0][0]
    assert button.text == '💎 Купить сейчас'
    assert button.callback_data == 'menu_buy'
    assert button.url is None
    assert button.web_app is None
    assert button.style == 'success'
