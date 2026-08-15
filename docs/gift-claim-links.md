# Gift claim links

Gift purchases use a separate, public `claim_code` instead of exposing or
truncating the internal payment token. The code contains 72 bits of randomness
encoded as 12 URL-safe characters and is resolved by exact indexed lookup.

The existing API field name `purchase_token` is retained for frontend
compatibility, but for gifts its value is the public claim code. Share links are:

- Telegram: `https://t.me/<bot>?start=GIFT_<claim_code>`
- MiniApp: `<CABINET_URL>/gift?tab=activate&code=<claim_code>`

Both paths use the same locked activation flow, reject buyer self-activation,
and allow exactly one recipient to claim a gift. The Telegram `GIFT_` namespace
is consumed before referral handling, including for malformed gift links.

The migration replaces existing 22-character claim codes and backfills missing
codes so every gift uses the same 12-character format. Previously issued
12-character token-prefix links and older long token links remain readable for
compatibility. Previously issued 22-character claim-code links are intentionally
not supported.
