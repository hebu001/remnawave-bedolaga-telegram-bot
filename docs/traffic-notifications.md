# Traffic limit notifications

User traffic alerts use fixed stages: 80%, 90%, and exhausted (100%). Each stage is reserved once per subscription and traffic cycle in PostgreSQL. A jump across several stages sends only the highest current stage. The user's on/off preference remains supported; the legacy configurable percentage is retained as a compatibility field with value 80.

The hourly monitoring pass reads paginated counters from Remnawave. It never sends based on cached database usage when the panel is unavailable. Signed bandwidth and limited webhooks fetch current panel data and pass through the same reservation logic. Active, trial and limited subscriptions are eligible; unlimited subscriptions and disabled preferences are excluded.

A cycle consists of the panel user UUID, the panel's last traffic reset timestamp and the subscription allowance. A changed allowance or actual traffic reset re-arms alerts; merely extending the subscription does not. For panels without reset timestamps, an observed counter decrease starts a new generation. Out-of-order observations are ignored.

`traffic_notification_states` is deliberately separate from expiring-subscription notification history, which renewal routines clear. Subscription row locking serializes polls/webhooks, including first creation of a state row. Existing `traffic_warn:<subscription_id>` Redis markers are adopted on first observation where available; their original 24-hour expiry does not affect new durable state.

Telegram does not offer an idempotency key for sendMessage. A reservation is committed before sending. After a crash or ambiguous network failure, it is retained to avoid daily duplicates; this can sacrifice a single uncertain delivery. An explicit flood rejection can retry after Telegram's delay. Delivery outcomes are retained for inspection.

## Deployment

Apply Alembic revision 0106 before starting the updated backend. It adds a new table and does not rewrite subscriptions. Roll back application code/configuration first if needed; keep the state table for a subsequent retry.

Configure the receiver using `REMNAWAVE_WEBHOOK_ENABLED=true`, `/remnawave-webhook` and a strong `REMNAWAVE_WEBHOOK_SECRET`. Keep the secret outside Git. The panel must use the same value for `WEBHOOK_SECRET_HEADER`, with `WEBHOOK_ENABLED=true`, the public HTTPS receiver URL, and `BANDWIDTH_USAGE_NOTIFICATIONS_ENABLED=true`.

Remnawave 2.8.1 allows bandwidth percentages only between 25 and 95, so configure `[80,90]` and use `user.limited` for exhaustion. For this rollout, enable webhook delivery for `user.bandwidth_usage_threshold_reached`, `user.limited` and `user.traffic_reset`. Preserve the panel's existing Telegram event settings.

Verify signature rejection, signed delivery of an inert unknown event, configured thresholds, migrations and container health. Do not generate artificial customer usage or send test messages to real users.
