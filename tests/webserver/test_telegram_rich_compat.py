from aiogram.types import Update

from app.webserver.telegram import _normalize_callback_rich_blocks


def _callback_payload() -> dict:
    return {
        'update_id': 101,
        'callback_query': {
            'id': 'callback-1',
            'from': {
                'id': 6925404065,
                'is_bot': False,
                'first_name': 'Test',
            },
            'chat_instance': 'chat-instance',
            'data': 'menu_referrals',
            'message': {
                'message_id': 9828,
                'date': 1_787_657_773,
                'chat': {
                    'id': 6925404065,
                    'type': 'private',
                    'first_name': 'Test',
                },
                'rich_message': {
                    'blocks': [
                        {
                            'type': 'paragraph',
                            'text': 'Profile',
                        },
                        {
                            'type': 'expandable_blockquote',
                            'text': {
                                'type': 'url',
                                'text': 'https://sub.example.com/u/abc',
                                'url': 'https://sub.example.com/u/abc',
                            },
                        },
                    ]
                },
            },
        },
    }


def test_normalize_callback_rich_blocks_allows_aiogram_update_validation() -> None:
    payload = _callback_payload()

    normalized = _normalize_callback_rich_blocks(payload)
    update = Update.model_validate(normalized)

    blocks = normalized['callback_query']['message']['rich_message']['blocks']
    assert blocks[1]['type'] == 'blockquote'
    assert blocks[1]['blocks'] == [
        {
            'type': 'paragraph',
            'text': {
                'type': 'url',
                'text': 'https://sub.example.com/u/abc',
                'url': 'https://sub.example.com/u/abc',
            },
        }
    ]
    assert 'text' not in blocks[1]
    assert update.callback_query is not None
    assert update.callback_query.data == 'menu_referrals'


def test_normalize_callback_rich_blocks_does_not_change_message_entities() -> None:
    payload = _callback_payload()
    payload['callback_query']['message']['entities'] = [
        {
            'type': 'expandable_blockquote',
            'offset': 0,
            'length': 7,
        }
    ]

    _normalize_callback_rich_blocks(payload)

    assert payload['callback_query']['message']['entities'][0]['type'] == 'expandable_blockquote'
