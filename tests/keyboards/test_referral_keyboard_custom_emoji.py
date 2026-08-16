from app.keyboards import inline


def test_referral_keyboard_uses_custom_emoji_icons(monkeypatch):
    monkeypatch.setattr(type(inline.settings), 'is_referral_withdrawal_enabled', lambda self: False)

    keyboard = inline.get_referral_keyboard('ru')
    buttons = [row[0] for row in keyboard.inline_keyboard]

    assert [button.text for button in buttons] == [
        'Отправить приглашение',
        'Показать QR код',
        'Список рефералов',
        'Аналитика',
        'Назад',
    ]
    assert [button.callback_data for button in buttons] == [
        'referral_create_invite',
        'referral_show_qr',
        'referral_list',
        'referral_analytics',
        'back_to_menu',
    ]
    assert [button.icon_custom_emoji_id for button in buttons] == [
        '5258362837411045098',
        '5226513232549664618',
        '5258513401784573443',
        '5258330865674494479',
        '5258236805890710909',
    ]
