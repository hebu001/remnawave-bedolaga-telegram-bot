"""Both menu editors manage one public callback, never a personal credential."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.cabinet.auth import personal_link
from app.cabinet.routes import admin_menu_layout as editor
from app.config import settings
from app.keyboards.inline import get_main_menu_keyboard, get_main_menu_keyboard_async
from app.services.menu_layout import service as layout_module
from app.services.menu_layout.context import MenuContext
from app.services.menu_layout.service import MenuLayoutService
from app.utils import button_styles_cache, menu_layout_cache


ACTION = 'cabinet_personal_link'
MARKER = menu_layout_cache.PERSONAL_LINK_MANAGED_KEY


def _links(keyboard):
    return [button for row in keyboard.inline_keyboard for button in row if button.callback_data == ACTION]


def _callback(action=ACTION, *, name='custom_access', **updates):
    return editor.ButtonConfig(id=name, type='callback', callback_data=action, **updates)


def _row(*buttons):
    return editor.RowConfig(id='actions', buttons=list(buttons), max_per_row=2)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(personal_link, 'personal_link_enabled', lambda: True)
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'cabinet')
    monkeypatch.setattr(settings, 'MENU_LAYOUT_ENABLED', False)
    monkeypatch.setattr(button_styles_cache, 'get_cached_button_styles', dict)


@pytest.fixture
def cabinet_store(monkeypatch, enabled):
    storage = {}
    db = MagicMock()
    db.commit = AsyncMock()

    async def read(_db, key):
        return storage.get(key)

    async def write(_db, key, value):
        storage[key] = value

    async def reload_cache():
        value = storage.get(menu_layout_cache.MENU_LAYOUT_KEY)
        layout = menu_layout_cache._validate_layout(json.loads(value)) if value else {}
        monkeypatch.setattr(menu_layout_cache, '_cached_layout', layout)

    monkeypatch.setattr(editor, '_get_setting_value', read)
    monkeypatch.setattr(editor, '_upsert_setting', write)
    monkeypatch.setattr(editor, 'load_menu_layout_cache', reload_cache)
    monkeypatch.setattr(editor, 'load_button_styles_cache', AsyncMock())
    monkeypatch.setattr(menu_layout_cache, '_cached_layout', None)
    return storage, db, SimpleNamespace(telegram_id=101)


@pytest.mark.asyncio
async def test_cabinet_save_move_disable_remove_add_and_reset(cabinet_store):
    storage, db, admin = cabinet_store
    assert len(_links(get_main_menu_keyboard('ru'))) == 1  # Existing menu stays compatible.
    assert ACTION in (await editor.get_menu_layout(admin)).callback_actions
    other = _callback('menu_buy', name='custom_buy')
    access = _callback(labels={'ru': 'Мой вход'}, style='success', icon_custom_emoji_id='123456')

    async def save(*buttons):
        return await editor.update_menu_layout(
            editor.MenuConfigUpdateRequest(rows=[_row(*buttons)] if buttons else []), admin, db
        )

    result = await save(access, other)
    restored = result.rows[0].buttons[0]
    assert restored.callback_data == ACTION and restored.url is None
    configured = get_main_menu_keyboard('ru')
    assert configured.inline_keyboard[0][0].callback_data == ACTION
    assert len(_links(configured)) == 1
    assert _links(configured)[0].text == 'Мой вход'
    assert _links(configured)[0].style == 'success'
    assert _links(configured)[0].icon_custom_emoji_id == '123456'
    assert _links(configured)[0].url is None and _links(configured)[0].web_app is None

    await save(other, access)
    assert get_main_menu_keyboard('ru').inline_keyboard[0][1].callback_data == ACTION
    await save(other, access.model_copy(update={'enabled': False}))
    assert not _links(get_main_menu_keyboard('ru'))
    await save(other)
    assert not _links(get_main_menu_keyboard('ru'))
    saved = json.loads(storage[menu_layout_cache.MENU_LAYOUT_KEY])
    assert saved[MARKER] is True and not saved['custom_buttons'].get('custom_access')
    assert ACTION in (await editor.get_menu_layout(admin)).callback_actions  # Can add again.
    await save()
    assert get_main_menu_keyboard('ru').inline_keyboard == []
    await save(access)
    assert len(_links(get_main_menu_keyboard('ru'))) == 1
    await editor.reset_menu_layout(admin, db)
    assert MARKER not in json.loads(storage[menu_layout_cache.MENU_LAYOUT_KEY])
    assert len(_links(get_main_menu_keyboard('ru'))) == 1


@pytest.mark.asyncio
async def test_cabinet_deduplicates_in_place_and_feature_off_hides(cabinet_store, monkeypatch):
    _, db, admin = cabinet_store
    payload = editor.MenuConfigUpdateRequest(
        rows=[_row(_callback(labels={'ru': 'Первый'}), _callback(name='custom_duplicate', labels={'ru': 'Второй'}))]
    )
    await editor.update_menu_layout(payload, admin, db)
    links = _links(get_main_menu_keyboard('ru'))
    assert len(links) == 1 and links[0].text == 'Первый'
    monkeypatch.setattr(personal_link, 'personal_link_enabled', lambda: False)
    assert not _links(get_main_menu_keyboard('ru'))


@pytest.mark.asyncio
async def test_empty_managed_cabinet_layout_survives_actual_cache_reload(monkeypatch):
    result = MagicMock()
    result.scalar_one_or_none.return_value = SimpleNamespace(value=json.dumps({MARKER: True, 'custom_buttons': {}}))
    session = AsyncMock()
    session.execute.return_value = result
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    monkeypatch.setattr(menu_layout_cache, 'AsyncSessionLocal', factory)
    monkeypatch.setattr(menu_layout_cache, '_cached_layout', None)
    loaded = await menu_layout_cache.load_menu_layout_cache()
    assert loaded == {MARKER: True, 'custom_buttons': {}}
    assert menu_layout_cache.get_cached_menu_layout() == loaded


def _generic_base():
    return {
        'version': 1,
        'rows': [{'id': 'actions', 'buttons': ['other'], 'max_per_row': 2}],
        'buttons': {'other': {'type': 'callback', 'action': 'menu_buy', 'text': {'ru': 'Купить'}}},
    }


def _generic_link(**updates):
    value = {'type': 'callback', 'action': ACTION, 'text': {'ru': 'Мой доступ'}, 'enabled': True}
    value.update(updates)
    return value


@pytest.fixture
def generic_store(monkeypatch, enabled):
    storage = {'json': json.dumps(_generic_base())}
    db = MagicMock()
    db.commit = AsyncMock()

    async def execute(_query):
        result = MagicMock()
        result.scalar_one_or_none.return_value = SimpleNamespace(value=storage['json'], updated_at=None)
        return result

    async def upsert(_db, _key, value, **_kwargs):
        storage['json'] = value

    db.execute = AsyncMock(side_effect=execute)
    monkeypatch.setattr(layout_module, 'upsert_system_setting', upsert)
    monkeypatch.setattr(MenuLayoutService, '_cache', None)
    monkeypatch.setattr(MenuLayoutService, '_cache_updated_at', None)
    monkeypatch.setattr(settings, 'MENU_LAYOUT_ENABLED', True)
    return storage, db


@pytest.mark.asyncio
async def test_generic_editor_catalog_save_disable_remove_add_reset_and_main_entrypoint(generic_store):
    storage, db = generic_store

    async def rendered():
        # Public entrypoint must not append a second button after the builder.
        return await get_main_menu_keyboard_async(db, 'ru')

    def preview_links(preview):
        return [button for row in preview for button in row['buttons'] if button['action'] == ACTION]

    assert len(_links(await rendered())) == 1
    available = next(
        item for item in await MenuLayoutService.get_available_callbacks(db) if item['callback_data'] == ACTION
    )
    assert available['name'] == 'Сохранить доступ к боту'
    assert available['category'] == 'menu' and not available['is_in_menu']
    assert len(preview_links(await MenuLayoutService.preview_keyboard(db, MenuContext()))) == 1

    await MenuLayoutService.add_custom_button(db, 'access', _generic_link(), row_id='actions')
    keyboard = await rendered()
    assert len(_links(keyboard)) == 1 and keyboard.inline_keyboard[0][1].callback_data == ACTION
    assert _links(keyboard)[0].text == 'Мой доступ'
    assert _links(keyboard)[0].url is None and _links(keyboard)[0].web_app is None
    assert json.loads(storage['json'])[MARKER] is True
    assert next(
        item for item in await MenuLayoutService.get_available_callbacks(db) if item['callback_data'] == ACTION
    )['is_in_menu']
    await MenuLayoutService.update_button(db, 'access', {'enabled': False})
    assert not _links(await rendered())
    assert not preview_links(await MenuLayoutService.preview_keyboard(db, MenuContext()))
    await MenuLayoutService.delete_custom_button(db, 'access')
    assert not _links(await rendered())
    assert json.loads(storage['json'])[MARKER] is True
    assert not next(
        item for item in await MenuLayoutService.get_available_callbacks(db) if item['callback_data'] == ACTION
    )['is_in_menu']
    await MenuLayoutService.add_custom_button(db, 'access', _generic_link(), row_id='actions')
    assert len(_links(await rendered())) == 1
    await MenuLayoutService.reset_to_default(db)
    assert MARKER not in json.loads(storage['json'])
    assert len(_links(await rendered())) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('hidden_by', ['disabled', 'visibility', 'condition', 'row_condition', 'unplaced'])
async def test_generic_explicit_hidden_button_never_resurrects(monkeypatch, enabled, hidden_by):
    config = _generic_base()
    config['buttons']['access'] = _generic_link()
    config['rows'][0]['buttons'].insert(0, 'access')
    if hidden_by == 'disabled':
        config['buttons']['access']['enabled'] = False
    elif hidden_by == 'visibility':
        config['buttons']['access']['visibility'] = 'admins'
    elif hidden_by == 'condition':
        config['buttons']['access']['conditions'] = {'min_balance_kopeks': 100}
    elif hidden_by == 'row_condition':
        config['rows'][0]['conditions'] = {'is_admin': True}
    else:
        config['rows'][0]['buttons'].remove('access')
    monkeypatch.setattr(MenuLayoutService, '_cache', config)
    assert not _links(await MenuLayoutService.build_keyboard(MagicMock(), MenuContext()))


@pytest.mark.asyncio
async def test_generic_deduplication_and_global_feature_gate(monkeypatch, enabled):
    config = _generic_base()
    config['buttons'].update({'access': _generic_link(), 'duplicate': _generic_link(text={'ru': 'Дубль'})})
    config['rows'][0]['buttons'] = ['access', 'other', 'duplicate']
    monkeypatch.setattr(MenuLayoutService, '_cache', copy.deepcopy(config))
    keyboard = await MenuLayoutService.build_keyboard(MagicMock(), MenuContext())
    assert keyboard.inline_keyboard[0][0].text == 'Мой доступ'
    assert len(_links(keyboard)) == 1
    monkeypatch.setattr(personal_link, 'personal_link_enabled', lambda: False)
    assert not _links(await MenuLayoutService.build_keyboard(MagicMock(), MenuContext()))
    preview = await MenuLayoutService.preview_keyboard(MagicMock(), MenuContext())
    assert all(button['action'] != ACTION for row in preview for button in row['buttons'])
