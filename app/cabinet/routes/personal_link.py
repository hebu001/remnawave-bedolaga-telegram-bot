"""Cabinet personal link management and unauthenticated exchange."""

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, SecretStr, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from app.database.models import User
from app.utils.cache import RateLimitCache

from ..auth.personal_link import get_personal_link, personal_link_session_id, resolve_personal_link
from ..dependencies import get_cabinet_db, get_personal_link_copy_user, get_personal_link_rotation_user
from ..ip_utils import get_client_ip
from ..schemas.auth import AuthResponse
from .auth import _create_auth_response, _store_refresh_token


logger = structlog.get_logger(__name__)


class PersonalLinkRoute(APIRoute):
    """Exclude bearer inputs from validation errors and cacheable responses."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                result = await handler(request)
            except RequestValidationError:
                result = JSONResponse({'detail': 'Invalid request'}, status_code=400)
            except HTTPException as exc:
                result = JSONResponse({'detail': exc.detail}, status_code=exc.status_code, headers=exc.headers)
            except Exception as exc:
                logger.error('Personal link request failed', error_type=type(exc).__name__)
                result = JSONResponse({'detail': 'Personal link request failed'}, status_code=500)
            result.headers['Cache-Control'] = 'no-store'
            result.headers['Pragma'] = 'no-cache'
            return result

        return handle


router = APIRouter(prefix='/auth/personal-link', tags=['Cabinet Auth'], route_class=PersonalLinkRoute)


class PersonalLinkResponse(BaseModel):
    url: str


class PersonalLinkExchange(BaseModel):
    token: SecretStr = Field(min_length=22, max_length=79)

    @field_validator('token')
    @classmethod
    def supported_token_length(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) not in (22, 79):
            raise ValueError('Unsupported token length')
        return value


@router.get('', response_model=PersonalLinkResponse)
async def read_my_link(
    request: Request,
    response: Response,
    user: User = Depends(get_personal_link_copy_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PersonalLinkResponse:
    response.headers['Cache-Control'] = 'no-store'
    return PersonalLinkResponse(
        url=await get_personal_link(
            db,
            user.id,
            auth_payload=request.state.cabinet_auth_payload,
            verified_telegram_id=getattr(request.state, 'cabinet_verified_telegram_id', None),
        )
    )


@router.post('/rotate', response_model=PersonalLinkResponse)
async def rotate_my_link(
    request: Request,
    response: Response,
    user: User = Depends(get_personal_link_rotation_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PersonalLinkResponse:
    response.headers['Cache-Control'] = 'no-store'
    return PersonalLinkResponse(
        url=await get_personal_link(db, user.id, auth_payload=request.state.cabinet_auth_payload, rotate=True)
    )


@router.post('/exchange', response_model=AuthResponse)
async def exchange_personal_link(
    body: PersonalLinkExchange,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_cabinet_db),
) -> AuthResponse:
    response.headers['Cache-Control'] = 'no-store'
    if await RateLimitCache.is_ip_rate_limited(
        get_client_ip(request), 'personal_link_exchange', limit=20, window=60, fail_closed=True
    ):
        raise HTTPException(status_code=429, detail='Too many requests', headers={'Retry-After': '60'})
    user = await resolve_personal_link(db, body.token.get_secret_value())
    result = await _create_auth_response(
        user, db, auth_method='personal_link', personal_link_id=str(personal_link_session_id(user))
    )
    user.cabinet_last_login = datetime.now(UTC)
    await _store_refresh_token(db, user.id, result.refresh_token, device_info='personal_link')
    return result
