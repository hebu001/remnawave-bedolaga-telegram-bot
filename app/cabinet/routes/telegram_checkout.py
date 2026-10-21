"""One-click provider navigation for scoped Telegram purchase links."""

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.cabinet.dependencies import get_cabinet_db
from app.services.telegram_checkout import CheckoutError, CheckoutRateLimited, checkout_origin, complete_checkout


router = APIRouter(prefix='/telegram-checkout', tags=['Telegram checkout'])


class CheckoutBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    token: str = Field(min_length=43, max_length=43, pattern=r'^[A-Za-z0-9_-]{43}$')
    csrf: str = Field(min_length=32, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')


async def _read_checkout_body(request: Request) -> CheckoutBody:
    # Read a bounded stream before JSON parsing; never echo bearer credentials in
    # FastAPI's default validation response, including for malformed requests.
    if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
        raise HTTPException(415, 'Ожидается JSON-запрос.')
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 1024:
            raise HTTPException(413, 'Запрос слишком большой.')
        raw.extend(chunk)
    try:
        return CheckoutBody.model_validate_json(bytes(raw))
    except ValidationError:
        raise HTTPException(422, 'Некорректный запрос оплаты.') from None


@router.get('', response_class=HTMLResponse)
async def checkout_page():
    """No provider, Redis or DB operations; credentials never travel in the URL path."""
    csrf = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    content = """<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Оплата подписки</title><body><p id="status">Открываем страницу оплаты…</p>
<script nonce="NONCE">
let generation = 0;
async function openCheckout() {
  const attempt = ++generation;
  const token = location.hash.slice(1);
  history.replaceState(null, '', location.pathname);
  const status = document.getElementById('status');
  status.textContent = 'Открываем страницу оплаты…';
  if (!/^[A-Za-z0-9_-]{43}$/.test(token)) { status.textContent = 'Ссылка истекла. Вернитесь в бот.'; return; }
  try {
    const response = await fetch(location.pathname, {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({token, csrf:'CSRF'})});
    const data = await response.json();
    if (attempt !== generation) return;
    if (!response.ok) { status.textContent = typeof data.detail === 'string' ? data.detail : 'Оплата недоступна. Вернитесь в бот.'; return; }
    const target = new URL(data.url);
    if (target.protocol !== 'https:') throw new Error('invalid URL');
    location.replace(target.href);
  } catch (_) {
    if (attempt === generation) status.textContent = 'Не удалось получить результат. Повторно откройте ту же ссылку из бота позже.';
  }
}
window.addEventListener('hashchange', openCheckout);
openCheckout();
</script></body></html>""".replace('NONCE', nonce).replace('CSRF', csrf)
    response = HTMLResponse(
        content,
        headers={
            'Cache-Control': 'no-store',
            'Referrer-Policy': 'no-referrer',
            'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': f"default-src 'none'; script-src 'nonce-{nonce}'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        },
    )
    response.set_cookie(
        'telegram_checkout_csrf',
        csrf,
        max_age=900,
        secure=True,
        httponly=True,
        samesite='strict',
        path='/cabinet/telegram-checkout',
    )
    return response


@router.post('')
async def checkout_submit(request: Request, db=Depends(get_cabinet_db)):
    try:
        origin = checkout_origin()
    except CheckoutError as error:
        raise HTTPException(503, str(error)) from None
    if request.headers.get('origin') != origin:
        raise HTTPException(403, 'Откройте ссылку оплаты из бота заново.')
    body = await _read_checkout_body(request)
    cookie = request.cookies.get('telegram_checkout_csrf', '')
    if not secrets.compare_digest(cookie, body.csrf):
        raise HTTPException(403, 'Откройте ссылку оплаты из бота заново.')
    try:
        url = await complete_checkout(body.token, db)
    except CheckoutRateLimited as error:
        raise HTTPException(429, str(error)) from None
    except CheckoutError as error:
        raise HTTPException(409, str(error)) from None
    except Exception:
        raise HTTPException(503, 'Оплата временно недоступна. Повторно откройте эту же ссылку позже.') from None
    return JSONResponse({'url': url}, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
