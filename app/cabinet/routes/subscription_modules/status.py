"""Subscription status endpoints.

GET /subscription — subscription info
GET /subscription/connection-link
GET /subscription/happ-downloads
GET /subscription/app-config
"""

from __future__ import annotations

import base64
import re
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.tariff import get_tariff_by_id
from app.database.models import ServerSquad, User
from app.services.remnawave_service import RemnaWaveService
from app.services.system_settings_service import bot_configuration_service
from app.utils.incy_crypto import encrypt_incy_link

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from ...schemas.subscription import (
    ServerInfo,
    SubscriptionStatusResponse,
)
from .helpers import _subscription_to_response, resolve_subscription


logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get('/info', response_model=SubscriptionStatusResponse)
async def get_subscription(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = Query(None, description='Subscription ID for multi-tariff'),
):
    """Get current user's subscription details."""
    # Reload user from current session to get fresh data
    # (user object is from different session in get_current_cabinet_user)
    from app.database.crud.user import get_user_by_id

    fresh_user = await get_user_by_id(db, user.id)

    if not fresh_user:
        return SubscriptionStatusResponse(has_subscription=False, subscription=None)

    subscription = await resolve_subscription(db, fresh_user, subscription_id)

    if not subscription:
        # Return 200 with has_subscription: false instead of 404
        return SubscriptionStatusResponse(has_subscription=False, subscription=None)

    # Load tariff for daily subscription check and tariff name
    tariff_name = None
    if subscription.tariff_id:
        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        if tariff:
            subscription.tariff = tariff
            tariff_name = tariff.name

    # Fetch server names for connected squads
    servers: list[ServerInfo] = []
    connected_squads = subscription.connected_squads or []
    if connected_squads:
        result = await db.execute(select(ServerSquad).where(ServerSquad.squad_uuid.in_(connected_squads)))
        server_squads = result.scalars().all()
        servers = [
            ServerInfo(uuid=sq.squad_uuid, name=sq.display_name, country_code=sq.country_code) for sq in server_squads
        ]

    # Fetch traffic purchases (monthly packages)
    traffic_purchases_data = []
    from app.database.models import TrafficPurchase

    now = datetime.now(UTC)
    purchases_query = (
        select(TrafficPurchase)
        .where(TrafficPurchase.subscription_id == subscription.id)
        .where(TrafficPurchase.expires_at > now)
        .order_by(TrafficPurchase.expires_at.asc())
    )
    purchases_result = await db.execute(purchases_query)
    purchases = purchases_result.scalars().all()

    for purchase in purchases:
        time_remaining = purchase.expires_at - now
        days_remaining = max(0, int(time_remaining.total_seconds() / 86400))
        total_duration_seconds = (purchase.expires_at - purchase.created_at).total_seconds()
        elapsed_seconds = (now - purchase.created_at).total_seconds()
        progress_percent = min(
            100.0, max(0.0, (elapsed_seconds / total_duration_seconds * 100) if total_duration_seconds > 0 else 0)
        )

        traffic_purchases_data.append(
            {
                'id': purchase.id,
                'traffic_gb': purchase.traffic_gb,
                'expires_at': purchase.expires_at,
                'created_at': purchase.created_at,
                'days_remaining': days_remaining,
                'progress_percent': round(progress_percent, 1),
            }
        )

    subscription_data = _subscription_to_response(
        subscription, servers, tariff_name, traffic_purchases_data, user=fresh_user
    )
    return SubscriptionStatusResponse(has_subscription=True, subscription=subscription_data)


# ============ Connection Link ============


@router.get('/connection-link')
async def get_connection_link(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = Query(None, description='Subscription ID for multi-tariff'),
) -> dict[str, Any]:
    """Get subscription connection link and instructions."""
    from app.utils.subscription_utils import (
        convert_subscription_link_to_happ_scheme,
        get_display_subscription_link,
        get_happ_cryptolink_redirect_link,
    )

    subscription = await resolve_subscription(db, user, subscription_id)

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    subscription_url = subscription.subscription_url
    if not subscription_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Subscription link not yet generated',
        )

    display_link = get_display_subscription_link(subscription)
    happ_redirect = get_happ_cryptolink_redirect_link(subscription_url) if settings.is_happ_cryptolink_mode() else None
    happ_scheme_link = (
        convert_subscription_link_to_happ_scheme(subscription_url) if settings.is_happ_cryptolink_mode() else None
    )

    connect_mode = settings.CONNECT_BUTTON_MODE
    hide_subscription_link = settings.should_hide_subscription_link()

    return {
        'subscription_url': subscription_url if not hide_subscription_link else None,
        'display_link': display_link if not hide_subscription_link else None,
        'happ_redirect_link': happ_redirect,
        'happ_scheme_link': happ_scheme_link,
        'connect_mode': connect_mode,
        'hide_link': hide_subscription_link,
        'instructions': {
            'steps': [
                'Copy the subscription link',
                'Open your VPN application',
                "Find 'Add subscription' or 'Import' option",
                'Paste the copied link',
            ]
        },
    }


# ============ hApp Downloads ============


@router.get('/happ-downloads')
async def get_happ_downloads(
    user: User = Depends(get_current_cabinet_user),
) -> dict[str, Any]:
    """Get hApp download links for different platforms."""
    platforms = {
        'ios': {
            'name': 'iOS (iPhone/iPad)',
            'icon': '🍎',
            'link': settings.get_happ_download_link('ios'),
        },
        'android': {
            'name': 'Android',
            'icon': '🤖',
            'link': settings.get_happ_download_link('android'),
        },
        'macos': {
            'name': 'macOS',
            'icon': '🖥️',
            'link': settings.get_happ_download_link('macos'),
        },
        'windows': {
            'name': 'Windows',
            'icon': '💻',
            'link': settings.get_happ_download_link('windows'),
        },
    }

    # Filter out platforms without links
    available_platforms = {k: v for k, v in platforms.items() if v['link']}

    return {
        'platforms': available_platforms,
        'happ_enabled': bool(available_platforms),
    }


# ============ App Config for Connection ============


def _get_remnawave_config_uuid() -> str | None:
    """Get RemnaWave config UUID from system settings or env."""
    try:
        return bot_configuration_service.get_current_value('CABINET_REMNA_SUB_CONFIG')
    except Exception:
        return settings.CABINET_REMNA_SUB_CONFIG


def _extract_scheme_from_buttons(buttons: list[dict[str, Any]]) -> tuple[str, str]:
    """Extract URL scheme from buttons list.

    Returns:
        Tuple of (scheme, payload_kind) where payload_kind is one of
        'plain', 'happ_crypto', 'incy_crypto'.
    """
    for btn in buttons:
        if not isinstance(btn, dict):
            continue
        link = btn.get('link', '') or btn.get('url', '') or btn.get('buttonLink', '')
        if not link:
            continue
        link_upper = link.upper()

        # Check for {{INCY_CRYPT1_LINK}} -- uses subscription_incy_crypto_link as payload
        if '{{INCY_CRYPT1_LINK}}' in link_upper or 'INCY_CRYPT1_LINK' in link_upper:
            scheme = re.sub(r'\{\{INCY_CRYPT1_LINK\}\}', '', link, flags=re.IGNORECASE)
            if scheme and '://' in scheme:
                return scheme, 'incy_crypto'

        # Check for {{HAPP_CRYPT4_LINK}} -- uses subscription_crypto_link as payload
        if '{{HAPP_CRYPT4_LINK}}' in link_upper or 'HAPP_CRYPT4_LINK' in link_upper:
            scheme = re.sub(r'\{\{HAPP_CRYPT4_LINK\}\}', '', link, flags=re.IGNORECASE)
            if scheme and '://' in scheme:
                return scheme, 'happ_crypto'

        # Check for {{SUBSCRIPTION_LINK}} -- uses plain subscription_url as payload
        if '{{SUBSCRIPTION_LINK}}' in link_upper or 'SUBSCRIPTION_LINK' in link_upper:
            scheme = re.sub(r'\{\{SUBSCRIPTION_LINK\}\}', '', link, flags=re.IGNORECASE)
            if scheme and '://' in scheme:
                return scheme, 'plain'

        # Also check for type="subscriptionLink" buttons with custom schemes
        btn_type = btn.get('type', '')
        if btn_type == 'subscriptionLink' and '://' in link and not link.startswith('http'):
            scheme = link.split('{{')[0] if '{{' in link else link
            if scheme and '://' in scheme:
                return scheme, 'plain'
    return '', 'plain'


def _get_url_scheme_for_app(app: dict[str, Any]) -> tuple[str, str]:
    """Get URL scheme for app - from config, buttons, or fallback by name.

    Returns:
        Tuple of (scheme, payload_kind) where payload_kind is one of
        'plain', 'happ_crypto', 'incy_crypto'.
    """
    # 1. Check urlScheme field (cabinet format)
    scheme = str(app.get('urlScheme', '')).strip()
    if scheme:
        # Explicit cryptoLinkKind takes precedence; legacy usesCryptoLink maps to HAPP.
        kind = app.get('cryptoLinkKind')
        if kind in ('plain', 'happ_crypto', 'incy_crypto'):
            return scheme, kind
        if app.get('usesCryptoLink'):
            return scheme, 'happ_crypto'
        return scheme, 'plain'

    # 2. Extract from buttons in blocks (RemnaWave format)
    blocks = app.get('blocks', [])
    for block in blocks:
        if not isinstance(block, dict):
            continue
        buttons = block.get('buttons', [])
        scheme, kind = _extract_scheme_from_buttons(buttons)
        if scheme:
            return scheme, kind

    # 3. Check buttons directly in app (alternative structure)
    direct_buttons = app.get('buttons', [])
    if direct_buttons:
        scheme, kind = _extract_scheme_from_buttons(direct_buttons)
        if scheme:
            return scheme, kind

    # No scheme found
    logger.debug(
        '_get_url_scheme_for_app: No scheme found for app has blocks: has buttons: has urlScheme',
        get=app.get('name'),
        get_2=bool(app.get('blocks')),
        get_3=bool(app.get('buttons')),
        get_4=bool(app.get('urlScheme')),
    )
    return '', 'plain'


async def _load_app_config_async() -> dict[str, Any] | None:
    """Load app config from RemnaWave API (if configured).

    Returns None when no Remnawave config is set or API fails.
    """
    remnawave_uuid = _get_remnawave_config_uuid()

    if remnawave_uuid:
        try:
            service = RemnaWaveService()
            async with service.get_api_client() as api:
                config = await api.get_subscription_page_config(remnawave_uuid)
                if config and config.config:
                    logger.debug('Loaded app config from RemnaWave', remnawave_uuid=remnawave_uuid)
                    raw = dict(config.config)
                    raw['_isRemnawave'] = True
                    return raw
        except Exception as e:
            logger.warning('Failed to load RemnaWave config', error=e)

    return None


def _create_deep_link(
    app: dict[str, Any],
    subscription_url: str | None,
    subscription_crypto_link: str | None = None,
    subscription_incy_crypto_link: str | None = None,
) -> str | None:
    """Create deep link for app with subscription URL.

    Uses urlScheme from RemnaWave config (e.g. "happ://add/", "v2rayng://install-config?url=")
    combined with the appropriate payload URL.

    Scheme/payload pairings:
      - happ://add/{{SUBSCRIPTION_LINK}}       -> uses plain subscription_url
      - happ://crypt4/{{HAPP_CRYPT4_LINK}}     -> uses subscription_crypto_link (HAPP AES-RSA)
      - incy://crypt1/{{INCY_CRYPT1_LINK}}     -> uses subscription_incy_crypto_link (INCY AES-GCM)
    """
    if not isinstance(app, dict):
        return None

    if not subscription_url and not subscription_crypto_link and not subscription_incy_crypto_link:
        return None

    scheme, kind = _get_url_scheme_for_app(app)
    if not scheme:
        logger.debug('_create_deep_link: no urlScheme for app', get=app.get('name', 'unknown'))
        return None

    # Pick the correct payload based on which template the app uses
    if kind == 'incy_crypto':
        if not subscription_incy_crypto_link:
            logger.debug(
                '_create_deep_link: app requires incy crypto link but none available',
                get=app.get('name', 'unknown'),
            )
            return None
        payload = subscription_incy_crypto_link
    elif kind == 'happ_crypto':
        if not subscription_crypto_link:
            logger.debug(
                '_create_deep_link: app requires crypto link but none available', get=app.get('name', 'unknown')
            )
            return None
        payload = subscription_crypto_link
    else:
        if not subscription_url:
            logger.debug(
                '_create_deep_link: app requires subscription_url but none available', get=app.get('name', 'unknown')
            )
            return None
        payload = subscription_url

    if app.get('isNeedBase64Encoding'):
        try:
            payload = base64.b64encode(payload.encode('utf-8')).decode('utf-8')
        except Exception as e:
            logger.warning('Failed to encode payload to base64', error=e)

    return f'{scheme}{payload}'


def _resolve_button_url(
    url: str,
    subscription_url: str | None,
    subscription_crypto_link: str | None,
    subscription_incy_crypto_link: str | None = None,
) -> str:
    """Resolve template variables in button URLs.

    Matches remnawave/subscription-page frontend TemplateEngine:
    - {{SUBSCRIPTION_LINK}} -> plain subscription URL
    - {{HAPP_CRYPT3_LINK}} -> HAPP crypto link
    - {{HAPP_CRYPT4_LINK}} -> HAPP crypto link
    - {{INCY_CRYPT1_LINK}} -> INCY crypto link (AES-256-GCM, generated by bot)
    """
    if not url:
        return url
    result = url
    if subscription_url:
        result = result.replace('{{SUBSCRIPTION_LINK}}', subscription_url)
    if subscription_crypto_link:
        result = result.replace('{{HAPP_CRYPT3_LINK}}', subscription_crypto_link)
        result = result.replace('{{HAPP_CRYPT4_LINK}}', subscription_crypto_link)
    if subscription_incy_crypto_link:
        result = result.replace('{{INCY_CRYPT1_LINK}}', subscription_incy_crypto_link)
    return result


@router.get('/app-config')
async def get_app_config(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = Query(None, description='Subscription ID for multi-tariff'),
) -> dict[str, Any]:
    """Get app configuration for connection with deep links."""
    subscription = await resolve_subscription(db, user, subscription_id)

    subscription_url = None
    subscription_crypto_link = None
    if subscription:
        subscription_url = subscription.subscription_url
        subscription_crypto_link = subscription.subscription_crypto_link

    # Generate crypto link on the fly if subscription_url exists but crypto link is missing.
    # This covers synced users where enrich_happ_links was not called.
    if subscription_url and not subscription_crypto_link:
        try:
            service = RemnaWaveService()
            async with service.get_api_client() as api:
                encrypted = await api.encrypt_happ_crypto_link(subscription_url)
                if encrypted:
                    subscription_crypto_link = encrypted
                    if subscription:
                        subscription.subscription_crypto_link = encrypted
                        await db.commit()
                        logger.info(
                            'Generated and saved crypto link for user',
                            user_id=user.id,
                        )
        except Exception as e:
            logger.debug('Could not generate crypto link', error=e)

    config = await _load_app_config_async()

    # Generate INCY crypto link on every request — AES-GCM uses a fresh IV so the
    # ciphertext differs each call, but every payload decrypts to subscription_url.
    # Branding name (if available) is embedded in the encrypted payload and shown
    # on the INCY client's "Confirm import?" sheet.
    subscription_incy_crypto_link: str | None = None
    if subscription_url:
        branding_name: str | None = None
        if config:
            branding = config.get('brandingSettings') or {}
            if isinstance(branding, dict):
                # Remnawave uses 'title'; some configs use 'name'.
                for candidate_key in ('name', 'title'):
                    raw_name = branding.get(candidate_key)
                    if isinstance(raw_name, str) and raw_name.strip():
                        branding_name = raw_name.strip()
                        break
        try:
            subscription_incy_crypto_link = encrypt_incy_link(subscription_url, name=branding_name)
        except Exception as e:
            logger.warning('Failed to generate INCY crypto link', error=e)

    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='App configuration not set up.',
        )

    config.pop('_isRemnawave', None)
    hide_link = settings.should_hide_subscription_link()

    # Build platformNames from displayName of each platform
    platform_names: dict[str, Any] = {}
    for pk, pd in config.get('platforms', {}).items():
        if isinstance(pd, dict) and 'displayName' in pd:
            platform_names[pk] = pd['displayName']
    fallback_names = {
        'ios': {'en': 'iPhone/iPad'},
        'android': {'en': 'Android'},
        'macos': {'en': 'macOS'},
        'windows': {'en': 'Windows'},
        'linux': {'en': 'Linux'},
        'androidTV': {'en': 'Android TV'},
        'appleTV': {'en': 'Apple TV'},
    }
    for k, v in fallback_names.items():
        if k not in platform_names:
            platform_names[k] = v

    # Serve original blocks/svgLibrary enriched with deep links and resolved URLs.
    platforms: dict[str, Any] = {}
    for platform_key, platform_data in config.get('platforms', {}).items():
        if not isinstance(platform_data, dict):
            continue
        apps = platform_data.get('apps', [])
        if not isinstance(apps, list):
            continue

        enriched_apps = []
        for app in apps:
            if not isinstance(app, dict):
                continue

            # Generate deep link
            deep_link = None
            if subscription_url or subscription_crypto_link or subscription_incy_crypto_link:
                deep_link = _create_deep_link(
                    app,
                    subscription_url,
                    subscription_crypto_link,
                    subscription_incy_crypto_link,
                )
            app['deepLink'] = deep_link

            # Resolve templates only for subscriptionLink and copyButton (not external)
            for block in app.get('blocks', []):
                if not isinstance(block, dict):
                    continue
                for btn in block.get('buttons', []):
                    if not isinstance(btn, dict):
                        continue
                    btn_type = btn.get('type', '')
                    if btn_type in ('subscriptionLink', 'copyButton'):
                        url = btn.get('url', '') or btn.get('link', '')
                        if url and '{{' in url:
                            resolved = _resolve_button_url(
                                url,
                                subscription_url,
                                subscription_crypto_link,
                                subscription_incy_crypto_link,
                            )
                            # Only set resolvedUrl if ALL templates were resolved;
                            # otherwise let the frontend fall through to deepLink/subscriptionUrl
                            if '{{' not in resolved:
                                btn['resolvedUrl'] = resolved

            enriched_apps.append(app)

        if enriched_apps:
            platform_output = {k: v for k, v in platform_data.items() if k != 'apps'}
            platform_output['apps'] = enriched_apps
            platforms[platform_key] = platform_output

    return {
        'isRemnawave': True,
        'platforms': platforms,
        'svgLibrary': config.get('svgLibrary', {}),
        'baseTranslations': config.get('baseTranslations'),
        'baseSettings': config.get('baseSettings'),
        'uiConfig': config.get('uiConfig', {}),
        'platformNames': platform_names,
        'hasSubscription': bool(
            subscription_url or subscription_crypto_link or subscription_incy_crypto_link
        ),
        'subscriptionUrl': subscription_url,
        'subscriptionCryptoLink': subscription_crypto_link,
        'subscriptionIncyCryptoLink': subscription_incy_crypto_link,
        'hideLink': hide_link,
        'branding': config.get('brandingSettings', {}),
    }
