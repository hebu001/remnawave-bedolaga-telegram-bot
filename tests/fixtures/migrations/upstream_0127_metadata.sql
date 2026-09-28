-- Frozen pinned upstream metadata bootstrap, not models imported at test runtime.
CREATE TABLE apple_notifications (
	id SERIAL NOT NULL,
	notification_uuid VARCHAR(64) NOT NULL,
	notification_type VARCHAR(64) NOT NULL,
	subtype VARCHAR(64),
	environment VARCHAR(16),
	transaction_id VARCHAR(64),
	original_transaction_id VARCHAR(64),
	status VARCHAR(32) NOT NULL,
	error TEXT,
	payload_hash VARCHAR(64) NOT NULL,
	metadata_json JSON,
	received_at TIMESTAMP WITH TIME ZONE,
	processed_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id)
);

CREATE INDEX ix_apple_notifications_notification_type ON apple_notifications (notification_type);

CREATE UNIQUE INDEX ix_apple_notifications_notification_uuid ON apple_notifications (notification_uuid);

CREATE INDEX ix_apple_notifications_original_transaction_id ON apple_notifications (original_transaction_id);

CREATE INDEX ix_apple_notifications_environment ON apple_notifications (environment);

CREATE INDEX ix_apple_notifications_id ON apple_notifications (id);

CREATE INDEX ix_apple_notifications_transaction_id ON apple_notifications (transaction_id);

CREATE UNIQUE INDEX ix_apple_notifications_payload_hash ON apple_notifications (payload_hash);

CREATE TABLE promo_groups (
	id SERIAL NOT NULL,
	name VARCHAR(255) NOT NULL,
	priority INTEGER NOT NULL,
	server_discount_percent INTEGER NOT NULL,
	traffic_discount_percent INTEGER NOT NULL,
	device_discount_percent INTEGER NOT NULL,
	period_discounts JSON,
	auto_assign_total_spent_kopeks INTEGER,
	apply_discounts_to_addons BOOLEAN NOT NULL,
	is_default BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (name)
);

CREATE INDEX ix_promo_groups_priority ON promo_groups (priority);

CREATE INDEX ix_promo_groups_id ON promo_groups (id);

CREATE TABLE tariffs (
	id SERIAL NOT NULL,
	name VARCHAR(255) NOT NULL,
	description TEXT,
	display_order INTEGER NOT NULL,
	is_active BOOLEAN NOT NULL,
	traffic_limit_gb INTEGER NOT NULL,
	device_limit INTEGER NOT NULL,
	device_price_kopeks INTEGER,
	max_device_limit INTEGER,
	allowed_squads JSON,
	server_traffic_limits JSON,
	period_prices JSON NOT NULL,
	tier_level INTEGER NOT NULL,
	highlight_period_days INTEGER,
	is_highlighted BOOLEAN DEFAULT 'false' NOT NULL,
	is_trial_available BOOLEAN NOT NULL,
	allow_traffic_topup BOOLEAN NOT NULL,
	traffic_topup_enabled BOOLEAN NOT NULL,
	traffic_topup_packages JSON,
	max_topup_traffic_gb INTEGER NOT NULL,
	is_daily BOOLEAN NOT NULL,
	daily_price_kopeks INTEGER NOT NULL,
	lava_product_id VARCHAR(255),
	custom_days_enabled BOOLEAN NOT NULL,
	price_per_day_kopeks INTEGER NOT NULL,
	min_days INTEGER NOT NULL,
	max_days INTEGER NOT NULL,
	custom_traffic_enabled BOOLEAN NOT NULL,
	traffic_price_per_gb_kopeks INTEGER NOT NULL,
	min_traffic_gb INTEGER NOT NULL,
	max_traffic_gb INTEGER NOT NULL,
	show_in_gift BOOLEAN DEFAULT 'true' NOT NULL,
	traffic_reset_mode VARCHAR(20),
	external_squad_uuid VARCHAR(255),
	panel_tag VARCHAR(16),
	trial_duration_days INTEGER,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id)
);

CREATE INDEX ix_tariffs_id ON tariffs (id);

CREATE TABLE contest_templates (
	id SERIAL NOT NULL,
	name VARCHAR(100) NOT NULL,
	slug VARCHAR(50) NOT NULL,
	description TEXT,
	prize_type VARCHAR(20) NOT NULL,
	prize_value VARCHAR(50) NOT NULL,
	max_winners INTEGER NOT NULL,
	attempts_per_user INTEGER NOT NULL,
	times_per_day INTEGER NOT NULL,
	schedule_times VARCHAR(255),
	cooldown_hours INTEGER NOT NULL,
	payload JSON,
	is_enabled BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_contest_templates_slug ON contest_templates (slug);

CREATE INDEX ix_contest_templates_id ON contest_templates (id);

CREATE TABLE squads (
	id SERIAL NOT NULL,
	uuid VARCHAR(255) NOT NULL,
	name VARCHAR(255) NOT NULL,
	country_code VARCHAR(5),
	is_available BOOLEAN,
	price_kopeks INTEGER,
	description TEXT,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (uuid)
);

CREATE INDEX ix_squads_id ON squads (id);

CREATE TABLE service_rules (
	id SERIAL NOT NULL,
	"order" INTEGER,
	title VARCHAR(255) NOT NULL,
	content TEXT NOT NULL,
	is_active BOOLEAN,
	language VARCHAR(5),
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id)
);

CREATE INDEX ix_service_rules_id ON service_rules (id);

CREATE TABLE privacy_policies (
	id SERIAL NOT NULL,
	language VARCHAR(10) NOT NULL,
	content TEXT NOT NULL,
	is_enabled BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (language)
);

CREATE INDEX ix_privacy_policies_id ON privacy_policies (id);

CREATE TABLE public_offers (
	id SERIAL NOT NULL,
	language VARCHAR(10) NOT NULL,
	content TEXT NOT NULL,
	is_enabled BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (language)
);

CREATE INDEX ix_public_offers_id ON public_offers (id);

CREATE TABLE recurrent_payments (
	id SERIAL NOT NULL,
	language VARCHAR(10) NOT NULL,
	content TEXT NOT NULL,
	is_enabled BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (language)
);

CREATE INDEX ix_recurrent_payments_id ON recurrent_payments (id);

CREATE TABLE faq_settings (
	id SERIAL NOT NULL,
	language VARCHAR(10) NOT NULL,
	is_enabled BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (language)
);

CREATE INDEX ix_faq_settings_id ON faq_settings (id);

CREATE TABLE faq_pages (
	id SERIAL NOT NULL,
	language VARCHAR(10) NOT NULL,
	title VARCHAR(255) NOT NULL,
	content TEXT NOT NULL,
	display_order INTEGER NOT NULL,
	is_active BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id)
);

CREATE INDEX ix_faq_pages_id ON faq_pages (id);

CREATE INDEX ix_faq_pages_language ON faq_pages (language);

CREATE TABLE system_settings (
	id SERIAL NOT NULL,
	key VARCHAR(255) NOT NULL,
	value TEXT,
	description TEXT,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (key)
);

CREATE INDEX ix_system_settings_id ON system_settings (id);

CREATE TABLE email_templates (
	id SERIAL NOT NULL,
	notification_type VARCHAR(100) NOT NULL,
	language VARCHAR(10) NOT NULL,
	subject VARCHAR(500) NOT NULL,
	body_html TEXT NOT NULL,
	is_active BOOLEAN DEFAULT 'true' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_email_templates_type_lang UNIQUE (notification_type, language)
);

CREATE INDEX ix_email_templates_notification_type ON email_templates (notification_type);

CREATE TABLE monitoring_logs (
	id SERIAL NOT NULL,
	event_type VARCHAR(100) NOT NULL,
	message TEXT NOT NULL,
	data JSON,
	is_success BOOLEAN,
	created_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id)
);

CREATE INDEX ix_monitoring_logs_id ON monitoring_logs (id);

CREATE TABLE server_squads (
	id SERIAL NOT NULL,
	squad_uuid VARCHAR(255) NOT NULL,
	display_name VARCHAR(255) NOT NULL,
	original_name VARCHAR(255),
	country_code VARCHAR(5),
	is_available BOOLEAN,
	is_trial_eligible BOOLEAN NOT NULL,
	price_kopeks INTEGER,
	description TEXT,
	sort_order INTEGER,
	max_users INTEGER,
	current_users INTEGER,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_server_squads_squad_uuid ON server_squads (squad_uuid);

CREATE INDEX ix_server_squads_id ON server_squads (id);

CREATE TABLE web_api_tokens (
	id SERIAL NOT NULL,
	name VARCHAR(255) NOT NULL,
	token_hash VARCHAR(128) NOT NULL,
	token_prefix VARCHAR(32) NOT NULL,
	description TEXT,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE,
	last_used_at TIMESTAMP WITH TIME ZONE,
	last_used_ip VARCHAR(64),
	is_active BOOLEAN NOT NULL,
	created_by VARCHAR(255),
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_web_api_tokens_token_hash ON web_api_tokens (token_hash);

CREATE INDEX ix_web_api_tokens_token_prefix ON web_api_tokens (token_prefix);

CREATE INDEX ix_web_api_tokens_id ON web_api_tokens (id);

CREATE TABLE main_menu_buttons (
	id SERIAL NOT NULL,
	text VARCHAR(64) NOT NULL,
	action_type VARCHAR(20) NOT NULL,
	action_value TEXT NOT NULL,
	visibility VARCHAR(20) NOT NULL,
	is_active BOOLEAN NOT NULL,
	display_order INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id)
);

CREATE INDEX ix_main_menu_buttons_order ON main_menu_buttons (display_order, id);

CREATE INDEX ix_main_menu_buttons_id ON main_menu_buttons (id);

CREATE TABLE menu_layout_history (
	id SERIAL NOT NULL,
	config_json TEXT NOT NULL,
	action VARCHAR(50) NOT NULL,
	changes_summary TEXT,
	user_info VARCHAR(255),
	created_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id)
);

CREATE INDEX ix_menu_layout_history_id ON menu_layout_history (id);

CREATE INDEX ix_menu_layout_history_created_at ON menu_layout_history (created_at);

CREATE INDEX ix_menu_layout_history_created ON menu_layout_history (created_at);

CREATE TABLE webhooks (
	id SERIAL NOT NULL,
	name VARCHAR(255) NOT NULL,
	url TEXT NOT NULL,
	secret VARCHAR(128),
	event_type VARCHAR(50) NOT NULL,
	is_active BOOLEAN NOT NULL,
	description TEXT,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	last_triggered_at TIMESTAMP WITH TIME ZONE,
	failure_count INTEGER NOT NULL,
	success_count INTEGER NOT NULL,
	PRIMARY KEY (id)
);

CREATE INDEX ix_webhooks_event_type ON webhooks (event_type);

CREATE INDEX ix_webhooks_is_active ON webhooks (is_active);

CREATE INDEX ix_webhooks_id ON webhooks (id);

CREATE TABLE wheel_configs (
	id SERIAL NOT NULL,
	is_enabled BOOLEAN NOT NULL,
	name VARCHAR(255) NOT NULL,
	spin_cost_stars INTEGER NOT NULL,
	spin_cost_days INTEGER NOT NULL,
	spin_cost_stars_enabled BOOLEAN NOT NULL,
	spin_cost_days_enabled BOOLEAN NOT NULL,
	rtp_percent INTEGER NOT NULL,
	daily_spin_limit INTEGER NOT NULL,
	min_subscription_days_for_day_payment INTEGER NOT NULL,
	promo_prefix VARCHAR(20) NOT NULL,
	promo_validity_days INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id)
);

CREATE INDEX ix_wheel_configs_id ON wheel_configs (id);

CREATE TABLE payment_method_configs (
	id SERIAL NOT NULL,
	method_id VARCHAR(50) NOT NULL,
	sort_order INTEGER NOT NULL,
	is_enabled BOOLEAN NOT NULL,
	display_name VARCHAR(255),
	description TEXT,
	sub_options JSON,
	quick_amounts JSON,
	min_amount_kopeks INTEGER,
	max_amount_kopeks INTEGER,
	user_type_filter VARCHAR(20) NOT NULL,
	first_topup_filter VARCHAR(10) NOT NULL,
	promo_group_filter_mode VARCHAR(20) NOT NULL,
	open_url_direct BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id)
);

CREATE INDEX ix_payment_method_configs_sort_order ON payment_method_configs (sort_order);

CREATE INDEX ix_payment_method_configs_id ON payment_method_configs (id);

CREATE UNIQUE INDEX ix_payment_method_configs_method_id ON payment_method_configs (method_id);

CREATE TABLE required_channels (
	id SERIAL NOT NULL,
	channel_id VARCHAR(100) NOT NULL,
	channel_link VARCHAR(500),
	title VARCHAR(255),
	is_active BOOLEAN DEFAULT 'true' NOT NULL,
	sort_order INTEGER DEFAULT '0' NOT NULL,
	disable_trial_on_leave BOOLEAN DEFAULT 'true' NOT NULL,
	disable_paid_on_leave BOOLEAN DEFAULT 'false' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (channel_id)
);

CREATE TABLE user_channel_subscriptions (
	id SERIAL NOT NULL,
	telegram_id BIGINT NOT NULL,
	channel_id VARCHAR(100) NOT NULL,
	is_member BOOLEAN DEFAULT 'false' NOT NULL,
	checked_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_user_channel_sub UNIQUE (telegram_id, channel_id)
);

CREATE INDEX ix_user_channel_sub_telegram_id ON user_channel_subscriptions (telegram_id);

CREATE INDEX ix_user_channel_sub_channel_id ON user_channel_subscriptions (channel_id);

CREATE TABLE landing_pages (
	id SERIAL NOT NULL,
	slug VARCHAR(100) NOT NULL,
	is_active BOOLEAN NOT NULL,
	title JSON NOT NULL,
	subtitle JSON,
	features JSON NOT NULL,
	footer_text JSON,
	allowed_tariff_ids JSON NOT NULL,
	allowed_periods JSON NOT NULL,
	payment_methods JSON NOT NULL,
	gift_enabled BOOLEAN NOT NULL,
	custom_css TEXT,
	meta_title JSON,
	meta_description JSON,
	display_order INTEGER NOT NULL,
	discount_percent INTEGER,
	discount_overrides JSON,
	discount_starts_at TIMESTAMP WITH TIME ZONE,
	discount_ends_at TIMESTAMP WITH TIME ZONE,
	discount_badge_text JSON,
	background_config JSON,
	sticky_pay_button BOOLEAN DEFAULT false NOT NULL,
	analytics_view_enabled BOOLEAN DEFAULT false NOT NULL,
	analytics_view_goal VARCHAR(64),
	analytics_click_enabled BOOLEAN DEFAULT false NOT NULL,
	analytics_click_goal VARCHAR(64),
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
	PRIMARY KEY (id),
	CONSTRAINT chk_landing_discount_percent_range CHECK (discount_percent IS NULL OR (discount_percent >= 1 AND discount_percent <= 99)),
	CONSTRAINT chk_landing_discount_dates_order CHECK (discount_starts_at IS NULL OR discount_ends_at IS NULL OR discount_starts_at < discount_ends_at)
);

CREATE UNIQUE INDEX ix_landing_pages_slug ON landing_pages (slug);

CREATE INDEX ix_landing_pages_id ON landing_pages (id);

CREATE TABLE news_categories (
	id SERIAL NOT NULL,
	name VARCHAR(100) NOT NULL,
	color VARCHAR(20) DEFAULT '#00e5a0' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_news_categories_name_lower ON news_categories (lower(name));

CREATE TABLE news_tags (
	id SERIAL NOT NULL,
	name VARCHAR(50) NOT NULL,
	color VARCHAR(20) DEFAULT '#94a3b8' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_news_tags_name_lower ON news_tags (lower(name));

CREATE TABLE info_pages (
	id SERIAL NOT NULL,
	slug VARCHAR(200) NOT NULL,
	title JSONB DEFAULT '{}' NOT NULL,
	content JSONB DEFAULT '{}' NOT NULL,
	page_type VARCHAR(20) DEFAULT 'page' NOT NULL,
	is_active BOOLEAN DEFAULT 'true' NOT NULL,
	sort_order INTEGER DEFAULT '0' NOT NULL,
	icon VARCHAR(50),
	replaces_tab VARCHAR(20),
	display_mode VARCHAR(10) DEFAULT 'both' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
	PRIMARY KEY (id),
	UNIQUE (slug)
);

CREATE INDEX ix_info_pages_id ON info_pages (id);

CREATE TABLE system_error_events (
	id SERIAL NOT NULL,
	event_uid VARCHAR(32) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	level VARCHAR(16) NOT NULL,
	logger_name VARCHAR(255),
	event TEXT NOT NULL,
	error_type VARCHAR(255),
	traceback TEXT,
	context JSON,
	user_id BIGINT,
	dedup_hash VARCHAR(32),
	delivery_status VARCHAR(16) NOT NULL,
	delivery_attempts INTEGER NOT NULL,
	last_attempt_at TIMESTAMP WITH TIME ZONE,
	delivered_at TIMESTAMP WITH TIME ZONE,
	delivery_error TEXT,
	PRIMARY KEY (id)
);

CREATE INDEX ix_system_error_events_id ON system_error_events (id);

CREATE INDEX ix_system_error_events_dedup_created ON system_error_events (dedup_hash, created_at);

CREATE INDEX ix_system_error_events_logger_name ON system_error_events (logger_name);

CREATE INDEX ix_system_error_events_created_at ON system_error_events (created_at);

CREATE INDEX ix_system_error_events_user_id ON system_error_events (user_id);

CREATE INDEX ix_system_error_events_level ON system_error_events (level);

CREATE UNIQUE INDEX ix_system_error_events_event_uid ON system_error_events (event_uid);

CREATE INDEX ix_system_error_events_status_created ON system_error_events (delivery_status, created_at);

CREATE INDEX ix_system_error_events_error_type ON system_error_events (error_type);

CREATE TABLE email_queue (
	id SERIAL NOT NULL,
	to_email VARCHAR(320) NOT NULL,
	subject TEXT NOT NULL,
	body_html TEXT NOT NULL,
	body_text TEXT,
	unsubscribe_url TEXT,
	attachments_json JSON,
	status VARCHAR(16) NOT NULL,
	attempts INTEGER NOT NULL,
	next_attempt_at TIMESTAMP WITH TIME ZONE,
	last_error TEXT,
	expires_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	sent_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id)
);

CREATE INDEX ix_email_queue_status ON email_queue (status);

CREATE INDEX ix_email_queue_created_at ON email_queue (created_at);

CREATE INDEX ix_email_queue_to_email ON email_queue (to_email);

CREATE INDEX ix_email_queue_id ON email_queue (id);

CREATE INDEX ix_email_queue_status_next_attempt ON email_queue (status, next_attempt_at);

CREATE TABLE user_reminders (
	id SERIAL NOT NULL,
	name VARCHAR(120) NOT NULL,
	is_active BOOLEAN DEFAULT 'false' NOT NULL,
	builtin_key VARCHAR(64),
	channels VARCHAR(16) NOT NULL,
	category VARCHAR(16) DEFAULT 'service' NOT NULL,
	conditions JSON NOT NULL,
	repeat_every_days INTEGER DEFAULT '7' NOT NULL,
	max_sends INTEGER DEFAULT '1' NOT NULL,
	texts JSON NOT NULL,
	button_kind VARCHAR(16) DEFAULT 'none' NOT NULL,
	button_target VARCHAR(500),
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (builtin_key)
);

CREATE INDEX ix_user_reminders_id ON user_reminders (id);

CREATE TABLE server_squad_promo_groups (
	server_squad_id INTEGER NOT NULL,
	promo_group_id INTEGER NOT NULL,
	PRIMARY KEY (server_squad_id, promo_group_id),
	FOREIGN KEY(server_squad_id) REFERENCES server_squads (id) ON DELETE CASCADE,
	FOREIGN KEY(promo_group_id) REFERENCES promo_groups (id) ON DELETE CASCADE
);

CREATE TABLE tariff_promo_groups (
	tariff_id INTEGER NOT NULL,
	promo_group_id INTEGER NOT NULL,
	PRIMARY KEY (tariff_id, promo_group_id),
	FOREIGN KEY(tariff_id) REFERENCES tariffs (id) ON DELETE CASCADE,
	FOREIGN KEY(promo_group_id) REFERENCES promo_groups (id) ON DELETE CASCADE
);

CREATE TABLE payment_method_promo_groups (
	payment_method_config_id INTEGER NOT NULL,
	promo_group_id INTEGER NOT NULL,
	PRIMARY KEY (payment_method_config_id, promo_group_id),
	FOREIGN KEY(payment_method_config_id) REFERENCES payment_method_configs (id) ON DELETE CASCADE,
	FOREIGN KEY(promo_group_id) REFERENCES promo_groups (id) ON DELETE CASCADE
);

CREATE TABLE users (
	id SERIAL NOT NULL,
	telegram_id BIGINT,
	auth_type VARCHAR(20) NOT NULL,
	username VARCHAR(255),
	first_name VARCHAR(255),
	last_name VARCHAR(255),
	status VARCHAR(20),
	language VARCHAR(5),
	balance_kopeks INTEGER,
	used_promocodes INTEGER,
	has_had_paid_subscription BOOLEAN NOT NULL,
	trial_reset_at TIMESTAMP WITH TIME ZONE,
	referred_by_id INTEGER,
	referral_code VARCHAR(20),
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	last_activity TIMESTAMP WITH TIME ZONE,
	remnawave_id BIGINT,
	remnawave_uuid VARCHAR(255),
	email VARCHAR(255),
	email_verified BOOLEAN NOT NULL,
	email_verified_at TIMESTAMP WITH TIME ZONE,
	email_verification_source VARCHAR(32),
	password_hash VARCHAR(255),
	email_verification_token VARCHAR(255),
	email_verification_expires TIMESTAMP WITH TIME ZONE,
	password_reset_token VARCHAR(255),
	password_reset_expires TIMESTAMP WITH TIME ZONE,
	cabinet_last_login TIMESTAMP WITH TIME ZONE,
	pending_campaign_slug VARCHAR(64),
	email_change_new VARCHAR(255),
	email_change_code VARCHAR(6),
	email_change_expires TIMESTAMP WITH TIME ZONE,
	google_id VARCHAR(255),
	yandex_id VARCHAR(255),
	discord_id VARCHAR(255),
	vk_id BIGINT,
	lifetime_used_traffic_bytes BIGINT,
	auto_promo_group_assigned BOOLEAN NOT NULL,
	auto_promo_group_threshold_kopeks BIGINT NOT NULL,
	referral_commission_percent INTEGER,
	referral_days_subscription_id INTEGER,
	referral_reward_preference VARCHAR(10),
	promo_offer_discount_percent INTEGER NOT NULL,
	promo_offer_discount_source VARCHAR(100),
	promo_offer_discount_expires_at TIMESTAMP WITH TIME ZONE,
	last_remnawave_sync TIMESTAMP WITH TIME ZONE,
	trojan_password VARCHAR(255),
	vless_uuid VARCHAR(255),
	ss_password VARCHAR(255),
	has_made_first_topup BOOLEAN NOT NULL,
	promo_group_id INTEGER,
	notification_settings JSONB,
	last_pinned_message_id INTEGER,
	restriction_topup BOOLEAN NOT NULL,
	restriction_subscription BOOLEAN NOT NULL,
	restriction_reason VARCHAR(500),
	partner_status VARCHAR(20) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(referred_by_id) REFERENCES users (id) ON DELETE SET NULL,
	UNIQUE (referral_code),
	UNIQUE (remnawave_uuid),
	FOREIGN KEY(promo_group_id) REFERENCES promo_groups (id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX ix_users_remnawave_id ON users (remnawave_id);

CREATE INDEX ix_users_partner_status ON users (partner_status);

CREATE INDEX ix_users_promo_group_id ON users (promo_group_id);

CREATE INDEX ix_users_referred_by_id ON users (referred_by_id);

CREATE UNIQUE INDEX ix_users_yandex_id ON users (yandex_id);

CREATE UNIQUE INDEX ix_users_telegram_id ON users (telegram_id);

CREATE UNIQUE INDEX ix_users_vk_id ON users (vk_id);

CREATE UNIQUE INDEX ix_users_discord_id ON users (discord_id);

CREATE UNIQUE INDEX ix_users_google_id ON users (google_id);

CREATE INDEX ix_users_id ON users (id);

CREATE UNIQUE INDEX ix_users_email ON users (email);

CREATE TABLE referral_reward_levels (
	id SERIAL NOT NULL,
	level INTEGER NOT NULL,
	is_active BOOLEAN DEFAULT 'true' NOT NULL,
	reward_mode VARCHAR(10) DEFAULT 'money' NOT NULL,
	trigger VARCHAR(20) DEFAULT 'first_topup' NOT NULL,
	referrer_percent INTEGER,
	referrer_fixed_kopeks INTEGER,
	referrer_days INTEGER DEFAULT '0' NOT NULL,
	referrer_tariff_id INTEGER,
	referee_fixed_kopeks INTEGER,
	referee_days INTEGER DEFAULT '0' NOT NULL,
	referee_tariff_id INTEGER,
	max_payments INTEGER DEFAULT '0' NOT NULL,
	required_referrals INTEGER DEFAULT '0' NOT NULL,
	required_referrals_active_only BOOLEAN DEFAULT 'true' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(referrer_tariff_id) REFERENCES tariffs (id) ON DELETE SET NULL,
	FOREIGN KEY(referee_tariff_id) REFERENCES tariffs (id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX ix_referral_reward_levels_level ON referral_reward_levels (level);

CREATE INDEX ix_referral_reward_levels_id ON referral_reward_levels (id);

CREATE TABLE contest_rounds (
	id SERIAL NOT NULL,
	template_id INTEGER NOT NULL,
	starts_at TIMESTAMP WITH TIME ZONE NOT NULL,
	ends_at TIMESTAMP WITH TIME ZONE NOT NULL,
	status VARCHAR(20) NOT NULL,
	payload JSON,
	winners_count INTEGER NOT NULL,
	max_winners INTEGER NOT NULL,
	attempts_per_user INTEGER NOT NULL,
	message_id BIGINT,
	chat_id BIGINT,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(template_id) REFERENCES contest_templates (id) ON DELETE CASCADE
);

CREATE INDEX idx_contest_round_status ON contest_rounds (status);

CREATE INDEX idx_contest_round_template ON contest_rounds (template_id);

CREATE INDEX ix_contest_rounds_id ON contest_rounds (id);

CREATE TABLE webhook_deliveries (
	id SERIAL NOT NULL,
	webhook_id INTEGER NOT NULL,
	event_type VARCHAR(50) NOT NULL,
	payload JSON NOT NULL,
	response_status INTEGER,
	response_body TEXT,
	status VARCHAR(20) NOT NULL,
	error_message TEXT,
	attempt_number INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE,
	delivered_at TIMESTAMP WITH TIME ZONE,
	next_retry_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(webhook_id) REFERENCES webhooks (id) ON DELETE CASCADE
);

CREATE INDEX ix_webhook_deliveries_id ON webhook_deliveries (id);

CREATE INDEX ix_webhook_deliveries_webhook_created ON webhook_deliveries (webhook_id, created_at);

CREATE INDEX ix_webhook_deliveries_status ON webhook_deliveries (status);

CREATE TABLE wheel_prizes (
	id SERIAL NOT NULL,
	config_id INTEGER NOT NULL,
	prize_type VARCHAR(50) NOT NULL,
	prize_value INTEGER NOT NULL,
	display_name VARCHAR(100) NOT NULL,
	emoji VARCHAR(10) NOT NULL,
	color VARCHAR(20) NOT NULL,
	prize_value_kopeks INTEGER NOT NULL,
	sort_order INTEGER NOT NULL,
	manual_probability FLOAT,
	is_active BOOLEAN NOT NULL,
	promo_balance_bonus_kopeks INTEGER,
	promo_subscription_days INTEGER,
	promo_traffic_gb INTEGER,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(config_id) REFERENCES wheel_configs (id) ON DELETE CASCADE
);

CREATE INDEX ix_wheel_prizes_id ON wheel_prizes (id);

CREATE TABLE saved_payment_methods (
	id SERIAL NOT NULL,
	user_id INTEGER NOT NULL,
	yookassa_payment_method_id VARCHAR(255) NOT NULL,
	method_type VARCHAR(50) NOT NULL,
	card_first6 VARCHAR(6),
	card_last4 VARCHAR(4),
	card_type VARCHAR(50),
	card_expiry_month VARCHAR(2),
	card_expiry_year VARCHAR(4),
	title VARCHAR(255),
	is_active BOOLEAN,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE INDEX ix_saved_payment_methods_user_active ON saved_payment_methods (user_id, is_active);

CREATE INDEX ix_saved_payment_methods_id ON saved_payment_methods (id);

CREATE INDEX ix_saved_payment_methods_user_id ON saved_payment_methods (user_id);

CREATE UNIQUE INDEX ix_saved_payment_methods_yookassa_payment_method_id ON saved_payment_methods (yookassa_payment_method_id);

CREATE TABLE apple_iap_accounts (
	id SERIAL NOT NULL,
	user_id INTEGER NOT NULL,
	account_token_uuid VARCHAR(36) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE,
	rotated_at TIMESTAMP WITH TIME ZONE,
	disabled_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT uq_apple_iap_accounts_user_id UNIQUE (user_id),
	CONSTRAINT uq_apple_iap_accounts_token UNIQUE (account_token_uuid),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX ix_apple_iap_accounts_id ON apple_iap_accounts (id);

CREATE TABLE apple_iap_abuse_events (
	id SERIAL NOT NULL,
	user_id INTEGER,
	event_type VARCHAR(64) NOT NULL,
	severity VARCHAR(16) NOT NULL,
	transaction_id VARCHAR(64),
	product_id VARCHAR(128),
	ip_address VARCHAR(64),
	details_json JSON,
	created_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX ix_apple_iap_abuse_events_id ON apple_iap_abuse_events (id);

CREATE INDEX ix_apple_iap_abuse_events_user_id ON apple_iap_abuse_events (user_id);

CREATE INDEX ix_apple_iap_abuse_events_transaction_id ON apple_iap_abuse_events (transaction_id);

CREATE INDEX ix_apple_iap_abuse_events_event_type ON apple_iap_abuse_events (event_type);

CREATE TABLE user_promo_groups (
	user_id INTEGER NOT NULL,
	promo_group_id INTEGER NOT NULL,
	assigned_at TIMESTAMP WITH TIME ZONE,
	assigned_by VARCHAR(50),
	PRIMARY KEY (user_id, promo_group_id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(promo_group_id) REFERENCES promo_groups (id) ON DELETE CASCADE
);

CREATE TABLE subscriptions (
	id SERIAL NOT NULL,
	user_id INTEGER NOT NULL,
	status VARCHAR(20),
	is_trial BOOLEAN,
	start_date TIMESTAMP WITH TIME ZONE,
	end_date TIMESTAMP WITH TIME ZONE NOT NULL,
	traffic_limit_gb INTEGER,
	traffic_used_gb FLOAT,
	purchased_traffic_gb INTEGER,
	traffic_reset_at TIMESTAMP WITH TIME ZONE,
	subscription_url VARCHAR,
	subscription_crypto_link VARCHAR,
	device_limit INTEGER,
	modem_enabled BOOLEAN,
	connected_squads JSON,
	autopay_enabled BOOLEAN,
	autopay_days_before INTEGER,
	autopay_period_days INTEGER,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	last_webhook_update_at TIMESTAMP WITH TIME ZONE,
	last_revoke_at TIMESTAMP WITH TIME ZONE,
	grace_candidate_reason VARCHAR(16),
	grace_candidate_at TIMESTAMP WITH TIME ZONE,
	grace_suppressed_until TIMESTAMP WITH TIME ZONE,
	grace_tail_expire_at TIMESTAMP WITH TIME ZONE,
	grace_session_open BOOLEAN DEFAULT false NOT NULL,
	grace_overlay_expire_at TIMESTAMP WITH TIME ZONE,
	remnawave_short_uuid VARCHAR(255),
	remnawave_id BIGINT,
	remnawave_uuid VARCHAR(255),
	remnawave_short_id VARCHAR(16) DEFAULT '' NOT NULL,
	tariff_id INTEGER,
	is_daily_paused BOOLEAN NOT NULL,
	last_daily_charge_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	UNIQUE (remnawave_short_id),
	FOREIGN KEY(tariff_id) REFERENCES tariffs (id) ON DELETE RESTRICT
);

CREATE INDEX ix_subscriptions_grace_expiry_scan ON subscriptions (status, is_trial, end_date);

CREATE INDEX ix_subscriptions_id ON subscriptions (id);

CREATE INDEX ix_subscriptions_user_status ON subscriptions (user_id, status);

CREATE INDEX ix_subscriptions_grace_candidate ON subscriptions (grace_candidate_at, grace_candidate_reason);

CREATE INDEX ix_subscriptions_remnawave_short_uuid ON subscriptions (remnawave_short_uuid);

CREATE UNIQUE INDEX uq_subscriptions_user_tariff_active ON subscriptions (user_id, tariff_id) WHERE tariff_id IS NOT NULL AND status IN ('active', 'trial', 'limited');

CREATE INDEX ix_subscriptions_user_tariff_status ON subscriptions (user_id, tariff_id, status);

CREATE INDEX ix_subscriptions_trial_created ON subscriptions (is_trial, created_at);

CREATE UNIQUE INDEX uq_subscriptions_remnawave_id ON subscriptions (remnawave_id) WHERE remnawave_id IS NOT NULL;

CREATE INDEX ix_subscriptions_tariff_id ON subscriptions (tariff_id);

CREATE INDEX ix_subscriptions_user_id ON subscriptions (user_id);

CREATE INDEX ix_subscriptions_status_trial ON subscriptions (status, is_trial);

CREATE TABLE transactions (
	id SERIAL NOT NULL,
	user_id INTEGER NOT NULL,
	type VARCHAR(50) NOT NULL,
	amount_kopeks INTEGER NOT NULL,
	description TEXT,
	payment_method VARCHAR(50),
	external_id VARCHAR(255),
	is_completed BOOLEAN,
	receipt_uuid VARCHAR(255),
	receipt_created_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE,
	completed_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT uq_transaction_external_id_method UNIQUE (external_id, payment_method),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX ix_transactions_type_method_created ON transactions (type, payment_method, created_at);

CREATE INDEX ix_transactions_receipt_uuid ON transactions (receipt_uuid);

CREATE INDEX ix_transactions_user_type_completed_amount ON transactions (user_id, type, is_completed, amount_kopeks);

CREATE INDEX ix_transactions_type_created_completed ON transactions (type, created_at, is_completed);

CREATE INDEX ix_transactions_user_created ON transactions (user_id, created_at);

CREATE INDEX ix_transactions_id ON transactions (id);

CREATE TABLE subscription_conversions (
	id SERIAL NOT NULL,
	user_id INTEGER NOT NULL,
	converted_at TIMESTAMP WITH TIME ZONE,
	trial_duration_days INTEGER,
	payment_method VARCHAR(50),
	first_payment_amount_kopeks INTEGER,
	first_paid_period_days INTEGER,
	created_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX ix_sub_conversions_converted_at ON subscription_conversions (converted_at);

CREATE INDEX ix_subscription_conversions_id ON subscription_conversions (id);

CREATE INDEX ix_sub_conversions_user_id ON subscription_conversions (user_id);

CREATE TABLE promocodes (
	id SERIAL NOT NULL,
	code VARCHAR(50) NOT NULL,
	type VARCHAR(50) NOT NULL,
	balance_bonus_kopeks INTEGER,
	subscription_days INTEGER,
	traffic_gb INTEGER DEFAULT '0' NOT NULL,
	max_uses INTEGER,
	current_uses INTEGER,
	valid_from TIMESTAMP WITH TIME ZONE,
	valid_until TIMESTAMP WITH TIME ZONE,
	is_active BOOLEAN,
	first_purchase_only BOOLEAN,
	tariff_id INTEGER,
	created_by INTEGER,
	promo_group_id INTEGER,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(tariff_id) REFERENCES tariffs (id) ON DELETE SET NULL,
	FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE SET NULL,
	FOREIGN KEY(promo_group_id) REFERENCES promo_groups (id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX ix_promocodes_code ON promocodes (code);

CREATE INDEX ix_promocodes_promo_group_id ON promocodes (promo_group_id);

CREATE INDEX ix_promocodes_id ON promocodes (id);

CREATE INDEX ix_promocodes_tariff_id ON promocodes (tariff_id);

CREATE TABLE coupon_batches (
	id SERIAL NOT NULL,
	name VARCHAR(255) NOT NULL,
	tariff_id INTEGER,
	period_days INTEGER NOT NULL,
	coupons_total INTEGER NOT NULL,
	wholesale_price_kopeks INTEGER NOT NULL,
	max_per_user INTEGER NOT NULL,
	valid_until TIMESTAMP WITH TIME ZONE,
	is_revoked BOOLEAN NOT NULL,
	created_by INTEGER,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
	PRIMARY KEY (id),
	FOREIGN KEY(tariff_id) REFERENCES tariffs (id) ON DELETE SET NULL,
	FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX ix_coupon_batches_id ON coupon_batches (id);

CREATE INDEX ix_coupon_batches_tariff_id ON coupon_batches (tariff_id);

CREATE TABLE withdrawal_requests (
	id SERIAL NOT NULL,
	user_id INTEGER NOT NULL,
	amount_kopeks INTEGER NOT NULL,
	status VARCHAR(50) NOT NULL,
	payment_details TEXT,
	risk_score INTEGER,
	risk_analysis TEXT,
	processed_by INTEGER,
	processed_at TIMESTAMP WITH TIME ZONE,
	admin_comment TEXT,
	last_reminder_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(processed_by) REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX ix_withdrawal_requests_status ON withdrawal_requests (status);

CREATE INDEX ix_withdrawal_requests_user_id ON withdrawal_requests (user_id);

CREATE INDEX ix_withdrawal_requests_id ON withdrawal_requests (id);

CREATE TABLE partner_applications (
	id SERIAL NOT NULL,
	user_id INTEGER NOT NULL,
	company_name VARCHAR(255),
	website_url VARCHAR(500),
	telegram_channel VARCHAR(255),
	description TEXT,
	expected_monthly_referrals INTEGER,
	desired_commission_percent INTEGER,
	status VARCHAR(20) NOT NULL,
	admin_comment TEXT,
	approved_commission_percent INTEGER,
	processed_by INTEGER,
	processed_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(processed_by) REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX ix_partner_applications_id ON partner_applications (id);

CREATE TABLE referral_contests (
	id SERIAL NOT NULL,
	title VARCHAR(255) NOT NULL,
	description TEXT,
	prize_text TEXT,
	contest_type VARCHAR(50) NOT NULL,
	start_at TIMESTAMP WITH TIME ZONE NOT NULL,
	end_at TIMESTAMP WITH TIME ZONE NOT NULL,
	daily_summary_time TIME WITHOUT TIME ZONE NOT NULL,
	daily_summary_times VARCHAR(255),
	timezone VARCHAR(64) NOT NULL,
	is_active BOOLEAN NOT NULL,
	last_daily_summary_date DATE,
	last_daily_summary_at TIMESTAMP WITH TIME ZONE,
	final_summary_sent BOOLEAN NOT NULL,
	created_by INTEGER,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX ix_referral_contests_id ON referral_contests (id);

CREATE TABLE contest_attempts (
	id SERIAL NOT NULL,
	round_id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	answer TEXT,
	is_winner BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT uq_round_user_attempt UNIQUE (round_id, user_id),
	FOREIGN KEY(round_id) REFERENCES contest_rounds (id) ON DELETE CASCADE,
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX idx_contest_attempt_round ON contest_attempts (round_id);

CREATE INDEX ix_contest_attempts_id ON contest_attempts (id);

CREATE TABLE legal_consents (
	id SERIAL NOT NULL,
	user_id INTEGER NOT NULL,
	document VARCHAR(32) NOT NULL,
	accepted_at TIMESTAMP WITH TIME ZONE NOT NULL,
	source VARCHAR(32),
	ip_address VARCHAR(64),
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX ix_legal_consents_user_document ON legal_consents (user_id, document);

CREATE INDEX ix_legal_consents_id ON legal_consents (id);

CREATE TABLE promo_offer_templates (
	id SERIAL NOT NULL,
	name VARCHAR(255) NOT NULL,
	offer_type VARCHAR(50) NOT NULL,
	message_text TEXT NOT NULL,
	button_text VARCHAR(255) NOT NULL,
	valid_hours INTEGER NOT NULL,
	discount_percent INTEGER NOT NULL,
	bonus_amount_kopeks INTEGER NOT NULL,
	active_discount_hours INTEGER,
	test_duration_hours INTEGER,
	test_squad_uuids JSON,
	is_active BOOLEAN NOT NULL,
	created_by INTEGER,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX ix_promo_offer_templates_id ON promo_offer_templates (id);

CREATE INDEX ix_promo_offer_templates_type ON promo_offer_templates (offer_type);

CREATE TABLE broadcast_history (
	id SERIAL NOT NULL,
	target_type VARCHAR(100) NOT NULL,
	message_text TEXT,
	has_media BOOLEAN,
	media_type VARCHAR(20),
	media_file_id VARCHAR(255),
	media_caption TEXT,
	total_count INTEGER,
	sent_count INTEGER,
	failed_count INTEGER,
	blocked_count INTEGER,
	status VARCHAR(50),
	admin_id INTEGER,
	admin_name VARCHAR(255),
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
	completed_at TIMESTAMP WITH TIME ZONE,
	category VARCHAR(20) NOT NULL,
	channel VARCHAR(20) NOT NULL,
	email_subject VARCHAR(255),
	email_html_content TEXT,
	PRIMARY KEY (id),
	FOREIGN KEY(admin_id) REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX ix_broadcast_history_id ON broadcast_history (id);

CREATE TABLE polls (
	id SERIAL NOT NULL,
	title VARCHAR(255) NOT NULL,
	description TEXT,
	reward_enabled BOOLEAN NOT NULL,
	reward_amount_kopeks INTEGER NOT NULL,
	created_by INTEGER,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX ix_polls_id ON polls (id);

CREATE TABLE user_messages (
	id SERIAL NOT NULL,
	message_text TEXT NOT NULL,
	is_active BOOLEAN,
	sort_order INTEGER,
	created_by INTEGER,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX ix_user_messages_id ON user_messages (id);

CREATE TABLE welcome_texts (
	id SERIAL NOT NULL,
	text_content TEXT NOT NULL,
	is_active BOOLEAN,
	is_enabled BOOLEAN,
	created_by INTEGER,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX ix_welcome_texts_id ON welcome_texts (id);

CREATE TABLE pinned_messages (
	id SERIAL NOT NULL,
	content TEXT NOT NULL,
	media_type VARCHAR(32),
	media_file_id VARCHAR(255),
	send_before_menu BOOLEAN DEFAULT '1' NOT NULL,
	send_on_every_start BOOLEAN DEFAULT '1' NOT NULL,
	is_active BOOLEAN,
	created_by INTEGER,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX ix_pinned_messages_id ON pinned_messages (id);

CREATE TABLE advertising_campaigns (
	id SERIAL NOT NULL,
	name VARCHAR(255) NOT NULL,
	start_parameter VARCHAR(64) NOT NULL,
	bonus_type VARCHAR(20) NOT NULL,
	balance_bonus_kopeks INTEGER,
	subscription_duration_days INTEGER,
	subscription_traffic_gb INTEGER,
	subscription_device_limit INTEGER,
	subscription_squads JSON,
	tariff_id INTEGER,
	tariff_duration_days INTEGER,
	is_active BOOLEAN,
	partner_user_id INTEGER,
	created_by INTEGER,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(tariff_id) REFERENCES tariffs (id) ON DELETE SET NULL,
	FOREIGN KEY(partner_user_id) REFERENCES users (id) ON DELETE SET NULL,
	FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX ix_advertising_campaigns_partner_user_id ON advertising_campaigns (partner_user_id);

CREATE INDEX ix_advertising_campaigns_id ON advertising_campaigns (id);

CREATE UNIQUE INDEX ix_advertising_campaigns_start_parameter ON advertising_campaigns (start_parameter);

CREATE TABLE tickets (
	id SERIAL NOT NULL,
	user_id INTEGER NOT NULL,
	title VARCHAR(255) NOT NULL,
	status VARCHAR(20) NOT NULL,
	priority VARCHAR(20) NOT NULL,
	user_reply_block_permanent BOOLEAN NOT NULL,
	user_reply_block_until TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	closed_at TIMESTAMP WITH TIME ZONE,
	last_sla_reminder_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX ix_tickets_id ON tickets (id);

CREATE TABLE button_click_logs (
	id SERIAL NOT NULL,
	button_id VARCHAR(100) NOT NULL,
	user_id INTEGER,
	callback_data VARCHAR(255),
	clicked_at TIMESTAMP WITH TIME ZONE,
	button_type VARCHAR(20),
	button_text VARCHAR(255),
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX ix_button_click_logs_button_id ON button_click_logs (button_id);

CREATE INDEX ix_button_click_logs_button_type ON button_click_logs (button_type);

CREATE INDEX ix_button_click_logs_user_date ON button_click_logs (user_id, clicked_at);

CREATE INDEX ix_button_click_logs_clicked_at ON button_click_logs (clicked_at);

CREATE INDEX ix_button_click_logs_id ON button_click_logs (id);

CREATE INDEX ix_button_click_logs_user_id ON button_click_logs (user_id);

CREATE INDEX ix_button_click_logs_button_date ON button_click_logs (button_id, clicked_at);

CREATE TABLE cabinet_refresh_tokens (
	id SERIAL NOT NULL,
	user_id INTEGER NOT NULL,
	token_hash VARCHAR(255) NOT NULL,
	device_info VARCHAR(500),
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE,
	revoked_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX ix_cabinet_refresh_tokens_id ON cabinet_refresh_tokens (id);

CREATE UNIQUE INDEX ix_cabinet_refresh_tokens_token_hash ON cabinet_refresh_tokens (token_hash);

CREATE INDEX ix_cabinet_refresh_tokens_user ON cabinet_refresh_tokens (user_id);

CREATE TABLE admin_roles (
	id SERIAL NOT NULL,
	name VARCHAR(100) NOT NULL,
	description TEXT,
	level INTEGER NOT NULL,
	permissions JSONB NOT NULL,
	color VARCHAR(7),
	icon VARCHAR(50),
	is_system BOOLEAN NOT NULL,
	is_active BOOLEAN NOT NULL,
	created_by INTEGER,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
	PRIMARY KEY (id),
	UNIQUE (name),
	FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE SET NULL
);

CREATE TABLE admin_audit_log (
	id BIGSERIAL NOT NULL,
	user_id INTEGER NOT NULL,
	action VARCHAR(100) NOT NULL,
	resource_type VARCHAR(50),
	resource_id VARCHAR(100),
	details JSONB,
	ip_address VARCHAR(45),
	user_agent TEXT,
	status VARCHAR(20) NOT NULL,
	request_method VARCHAR(10),
	request_path TEXT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX ix_admin_audit_resource ON admin_audit_log (resource_type, resource_id);

CREATE INDEX ix_admin_audit_created ON admin_audit_log (created_at);

CREATE INDEX ix_admin_audit_user_created ON admin_audit_log (user_id, created_at);

CREATE TABLE guest_purchases (
	id SERIAL NOT NULL,
	token VARCHAR(64) NOT NULL,
	landing_id INTEGER,
	contact_type VARCHAR(20) NOT NULL,
	contact_value VARCHAR(255) NOT NULL,
	is_gift BOOLEAN NOT NULL,
	source VARCHAR(20) DEFAULT 'landing' NOT NULL,
	buyer_user_id INTEGER,
	gift_recipient_type VARCHAR(20),
	gift_recipient_value VARCHAR(255),
	gift_message TEXT,
	tariff_id INTEGER,
	period_days INTEGER NOT NULL,
	amount_kopeks INTEGER NOT NULL,
	currency VARCHAR(3) NOT NULL,
	payment_method VARCHAR(50),
	payment_id VARCHAR(255),
	status VARCHAR(20) NOT NULL,
	subscription_url TEXT,
	subscription_crypto_link TEXT,
	user_id INTEGER,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
	paid_at TIMESTAMP WITH TIME ZONE,
	delivered_at TIMESTAMP WITH TIME ZONE,
	cabinet_password TEXT,
	auto_login_token TEXT,
	recipient_warning VARCHAR(50),
	retry_count INTEGER DEFAULT '0' NOT NULL,
	receipt_uuid VARCHAR(255),
	receipt_created_at TIMESTAMP WITH TIME ZONE,
	yandex_cid VARCHAR(128),
	subid VARCHAR(255),
	referrer VARCHAR(500),
	campaign_slug VARCHAR(64),
	idempotency_key VARCHAR(64),
	PRIMARY KEY (id),
	FOREIGN KEY(landing_id) REFERENCES landing_pages (id) ON DELETE SET NULL,
	FOREIGN KEY(buyer_user_id) REFERENCES users (id) ON DELETE SET NULL,
	FOREIGN KEY(tariff_id) REFERENCES tariffs (id) ON DELETE SET NULL,
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX ix_guest_purchases_id ON guest_purchases (id);

CREATE INDEX ix_guest_purchases_status_paid_at ON guest_purchases (status, paid_at);

CREATE INDEX ix_guest_purchases_landing_status_paid ON guest_purchases (landing_id, status, paid_at);

CREATE INDEX ix_guest_purchases_receipt_uuid ON guest_purchases (receipt_uuid);

CREATE INDEX ix_guest_purchases_buyer_user_id ON guest_purchases (buyer_user_id);

CREATE INDEX ix_guest_purchases_source ON guest_purchases (source);

CREATE INDEX ix_guest_purchases_status ON guest_purchases (status);

CREATE UNIQUE INDEX ux_guest_purchases_idempotency_key ON guest_purchases (idempotency_key);

CREATE UNIQUE INDEX ix_guest_purchases_token ON guest_purchases (token);

CREATE INDEX ix_guest_purchases_user_gift_status ON guest_purchases (user_id, is_gift, status);

CREATE INDEX ix_guest_purchases_contact ON guest_purchases (contact_type, contact_value);

CREATE TABLE news_articles (
	id SERIAL NOT NULL,
	title VARCHAR(500) NOT NULL,
	slug VARCHAR(500) NOT NULL,
	content TEXT DEFAULT '' NOT NULL,
	excerpt TEXT,
	category VARCHAR(100) DEFAULT '' NOT NULL,
	category_color VARCHAR(20) DEFAULT '#00e5a0' NOT NULL,
	tag VARCHAR(50),
	featured_image_url TEXT,
	is_published BOOLEAN DEFAULT 'false' NOT NULL,
	is_featured BOOLEAN DEFAULT 'false' NOT NULL,
	published_at TIMESTAMP WITH TIME ZONE,
	read_time_minutes INTEGER DEFAULT '1' NOT NULL,
	views_count INTEGER DEFAULT '0' NOT NULL,
	created_by INTEGER,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
	category_id INTEGER,
	tag_id INTEGER,
	PRIMARY KEY (id),
	FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE SET NULL,
	FOREIGN KEY(category_id) REFERENCES news_categories (id) ON DELETE SET NULL,
	FOREIGN KEY(tag_id) REFERENCES news_tags (id) ON DELETE SET NULL
);

CREATE INDEX ix_news_articles_id ON news_articles (id);

CREATE UNIQUE INDEX ix_news_articles_slug ON news_articles (slug);

CREATE INDEX ix_news_articles_published_at_published ON news_articles (is_published, published_at);

CREATE INDEX ix_news_articles_published_category ON news_articles (is_published, category);

CREATE INDEX ix_news_articles_created_at ON news_articles (created_at);

CREATE TABLE yandex_client_id_map (
	id SERIAL NOT NULL,
	user_id INTEGER NOT NULL,
	yandex_cid VARCHAR(128) NOT NULL,
	source VARCHAR(20) DEFAULT 'web' NOT NULL,
	counter_id VARCHAR(32),
	registration_sent BOOLEAN DEFAULT false NOT NULL,
	trial_sent BOOLEAN DEFAULT false NOT NULL,
	subid VARCHAR(255),
	yclid VARCHAR(64),
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
	PRIMARY KEY (id),
	UNIQUE (user_id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE user_device_aliases (
	id SERIAL NOT NULL,
	user_id INTEGER NOT NULL,
	hwid VARCHAR(255) NOT NULL,
	alias VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_user_device_aliases_user_hwid UNIQUE (user_id, hwid),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX ix_user_device_aliases_user_id ON user_device_aliases (user_id);

CREATE TABLE reachability_batches (
	id SERIAL NOT NULL,
	status VARCHAR(16) NOT NULL,
	phase VARCHAR(32),
	started_by_user_id INTEGER,
	scope JSON NOT NULL,
	request JSON NOT NULL,
	total_targets INTEGER NOT NULL,
	estimated_kopeks INTEGER,
	cost_kopeks INTEGER,
	error_message TEXT,
	created_at TIMESTAMP WITH TIME ZONE,
	started_at TIMESTAMP WITH TIME ZONE,
	finished_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(started_by_user_id) REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX ix_reachability_batches_id ON reachability_batches (id);

CREATE INDEX ix_reachability_batches_status ON reachability_batches (status);

CREATE INDEX ix_reachability_batches_started_by_user_id ON reachability_batches (started_by_user_id);

CREATE TABLE reachability_target_prefs (
	id SERIAL NOT NULL,
	target_kind VARCHAR(32) NOT NULL,
	target_ref VARCHAR(255) NOT NULL,
	purpose VARCHAR(16) NOT NULL,
	excluded BOOLEAN NOT NULL,
	note TEXT,
	updated_by_user_id INTEGER,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT uq_reachability_target_prefs_target UNIQUE (target_kind, target_ref),
	FOREIGN KEY(updated_by_user_id) REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX ix_reachability_target_prefs_id ON reachability_target_prefs (id);

CREATE TABLE user_reminder_states (
	id SERIAL NOT NULL,
	reminder_id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	sends_count INTEGER DEFAULT '0' NOT NULL,
	last_sent_at TIMESTAMP WITH TIME ZONE,
	last_success_at TIMESTAMP WITH TIME ZONE,
	dismissed_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT uq_user_reminder_states_reminder_user UNIQUE (reminder_id, user_id),
	FOREIGN KEY(reminder_id) REFERENCES user_reminders (id) ON DELETE CASCADE,
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX ix_user_reminder_states_user_id ON user_reminder_states (user_id);

CREATE INDEX ix_user_reminder_states_reminder_last_sent ON user_reminder_states (reminder_id, last_sent_at);

CREATE INDEX ix_user_reminder_states_id ON user_reminder_states (id);

CREATE TABLE yookassa_payments (
	id SERIAL NOT NULL,
	user_id INTEGER,
	yookassa_payment_id VARCHAR(255) NOT NULL,
	amount_kopeks INTEGER NOT NULL,
	currency VARCHAR(3) NOT NULL,
	description TEXT,
	status VARCHAR(50) NOT NULL,
	is_paid BOOLEAN,
	is_captured BOOLEAN,
	confirmation_url TEXT,
	metadata_json JSON,
	transaction_id INTEGER,
	payment_method_type VARCHAR(50),
	refundable BOOLEAN,
	test_mode BOOLEAN,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	yookassa_created_at TIMESTAMP WITH TIME ZONE,
	captured_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);

CREATE UNIQUE INDEX ix_yookassa_payments_yookassa_payment_id ON yookassa_payments (yookassa_payment_id);

CREATE INDEX ix_yookassa_payments_id ON yookassa_payments (id);

CREATE TABLE cryptobot_payments (
	id SERIAL NOT NULL,
	user_id INTEGER,
	invoice_id VARCHAR(255) NOT NULL,
	amount VARCHAR(50) NOT NULL,
	asset VARCHAR(10) NOT NULL,
	status VARCHAR(50) NOT NULL,
	description TEXT,
	payload TEXT,
	bot_invoice_url TEXT,
	mini_app_invoice_url TEXT,
	web_app_invoice_url TEXT,
	paid_at TIMESTAMP WITH TIME ZONE,
	transaction_id INTEGER,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);

CREATE INDEX ix_cryptobot_payments_id ON cryptobot_payments (id);

CREATE UNIQUE INDEX ix_cryptobot_payments_invoice_id ON cryptobot_payments (invoice_id);

CREATE TABLE apple_transactions (
	id SERIAL NOT NULL,
	user_id INTEGER NOT NULL,
	transaction_id VARCHAR(64) NOT NULL,
	original_transaction_id VARCHAR(64),
	product_id VARCHAR(128) NOT NULL,
	bundle_id VARCHAR(255) NOT NULL,
	amount_kopeks INTEGER NOT NULL,
	environment VARCHAR(16) NOT NULL,
	app_account_token VARCHAR(36),
	web_order_line_item_id VARCHAR(64),
	storefront VARCHAR(16),
	currency VARCHAR(3),
	price_micros BIGINT,
	purchase_date TIMESTAMP WITH TIME ZONE,
	revocation_date TIMESTAMP WITH TIME ZONE,
	revocation_reason VARCHAR(50),
	status VARCHAR(50),
	is_paid BOOLEAN,
	paid_at TIMESTAMP WITH TIME ZONE,
	credited_at TIMESTAMP WITH TIME ZONE,
	refunded_at TIMESTAMP WITH TIME ZONE,
	refund_reversed_at TIMESTAMP WITH TIME ZONE,
	transaction_id_fk INTEGER,
	signed_transaction_hash VARCHAR(64),
	metadata_json JSON,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(transaction_id_fk) REFERENCES transactions (id)
);

CREATE INDEX ix_apple_transactions_app_account_token ON apple_transactions (app_account_token);

CREATE INDEX ix_apple_transactions_id ON apple_transactions (id);

CREATE INDEX ix_apple_transactions_signed_transaction_hash ON apple_transactions (signed_transaction_hash);

CREATE INDEX ix_apple_transactions_original_transaction_id ON apple_transactions (original_transaction_id);

CREATE UNIQUE INDEX ix_apple_transactions_transaction_id ON apple_transactions (transaction_id);

CREATE UNIQUE INDEX ix_apple_transactions_web_order_line_item_id ON apple_transactions (web_order_line_item_id);

CREATE TABLE heleket_payments (
	id SERIAL NOT NULL,
	user_id INTEGER,
	uuid VARCHAR(255) NOT NULL,
	order_id VARCHAR(128) NOT NULL,
	amount VARCHAR(50) NOT NULL,
	currency VARCHAR(10) NOT NULL,
	payer_amount VARCHAR(50),
	payer_currency VARCHAR(10),
	exchange_rate FLOAT,
	discount_percent INTEGER,
	status VARCHAR(50) NOT NULL,
	payment_url TEXT,
	metadata_json JSON,
	paid_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE,
	transaction_id INTEGER,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);

CREATE UNIQUE INDEX ix_heleket_payments_order_id ON heleket_payments (order_id);

CREATE INDEX ix_heleket_payments_id ON heleket_payments (id);

CREATE UNIQUE INDEX ix_heleket_payments_uuid ON heleket_payments (uuid);

CREATE TABLE mulenpay_payments (
	id SERIAL NOT NULL,
	user_id INTEGER,
	mulen_payment_id INTEGER,
	uuid VARCHAR(255) NOT NULL,
	amount_kopeks INTEGER NOT NULL,
	currency VARCHAR(10) NOT NULL,
	description TEXT,
	status VARCHAR(50) NOT NULL,
	is_paid BOOLEAN,
	paid_at TIMESTAMP WITH TIME ZONE,
	payment_url TEXT,
	metadata_json JSON,
	callback_payload JSON,
	transaction_id INTEGER,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);

CREATE UNIQUE INDEX ix_mulenpay_payments_uuid ON mulenpay_payments (uuid);

CREATE INDEX ix_mulenpay_payments_id ON mulenpay_payments (id);

CREATE INDEX ix_mulenpay_payments_mulen_payment_id ON mulenpay_payments (mulen_payment_id);

CREATE TABLE pal24_payments (
	id SERIAL NOT NULL,
	user_id INTEGER,
	bill_id VARCHAR(255) NOT NULL,
	order_id VARCHAR(255),
	amount_kopeks INTEGER NOT NULL,
	currency VARCHAR(10) NOT NULL,
	description TEXT,
	type VARCHAR(20) NOT NULL,
	status VARCHAR(50) NOT NULL,
	is_active BOOLEAN,
	is_paid BOOLEAN,
	paid_at TIMESTAMP WITH TIME ZONE,
	last_status VARCHAR(50),
	last_status_checked_at TIMESTAMP WITH TIME ZONE,
	link_url TEXT,
	link_page_url TEXT,
	metadata_json JSON,
	callback_payload JSON,
	payment_id VARCHAR(255),
	payment_status VARCHAR(50),
	payment_method VARCHAR(50),
	balance_amount VARCHAR(50),
	balance_currency VARCHAR(10),
	payer_account VARCHAR(255),
	ttl INTEGER,
	expires_at TIMESTAMP WITH TIME ZONE,
	transaction_id INTEGER,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);

CREATE INDEX ix_pal24_payments_payment_id ON pal24_payments (payment_id);

CREATE UNIQUE INDEX ix_pal24_payments_bill_id ON pal24_payments (bill_id);

CREATE INDEX ix_pal24_payments_order_id ON pal24_payments (order_id);

CREATE INDEX ix_pal24_payments_id ON pal24_payments (id);

CREATE TABLE wata_payments (
	id SERIAL NOT NULL,
	user_id INTEGER,
	payment_link_id VARCHAR(64) NOT NULL,
	order_id VARCHAR(255),
	amount_kopeks INTEGER NOT NULL,
	currency VARCHAR(10) NOT NULL,
	description TEXT,
	type VARCHAR(50),
	status VARCHAR(50) NOT NULL,
	is_paid BOOLEAN,
	paid_at TIMESTAMP WITH TIME ZONE,
	last_status VARCHAR(50),
	terminal_public_id VARCHAR(64),
	url TEXT,
	success_redirect_url TEXT,
	fail_redirect_url TEXT,
	metadata_json JSON,
	callback_payload JSON,
	expires_at TIMESTAMP WITH TIME ZONE,
	transaction_id INTEGER,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);

CREATE INDEX ix_wata_payments_id ON wata_payments (id);

CREATE UNIQUE INDEX ix_wata_payments_payment_link_id ON wata_payments (payment_link_id);

CREATE INDEX ix_wata_payments_order_id ON wata_payments (order_id);

CREATE TABLE platega_payments (
	id SERIAL NOT NULL,
	user_id INTEGER,
	platega_transaction_id VARCHAR(255),
	correlation_id VARCHAR(64) NOT NULL,
	amount_kopeks INTEGER NOT NULL,
	currency VARCHAR(10) NOT NULL,
	description TEXT,
	payment_method_code INTEGER NOT NULL,
	status VARCHAR(50) NOT NULL,
	is_paid BOOLEAN,
	paid_at TIMESTAMP WITH TIME ZONE,
	redirect_url TEXT,
	return_url TEXT,
	failed_url TEXT,
	payload VARCHAR(255),
	metadata_json JSON,
	callback_payload JSON,
	expires_at TIMESTAMP WITH TIME ZONE,
	transaction_id INTEGER,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);

CREATE INDEX ix_platega_payments_id ON platega_payments (id);

CREATE UNIQUE INDEX ix_platega_payments_platega_transaction_id ON platega_payments (platega_transaction_id);

CREATE UNIQUE INDEX ix_platega_payments_correlation_id ON platega_payments (correlation_id);

CREATE TABLE platega_subscriptions (
	id SERIAL NOT NULL,
	user_id INTEGER NOT NULL,
	subscription_id INTEGER NOT NULL,
	tariff_id INTEGER,
	platega_subscription_id VARCHAR(255),
	interval INTEGER NOT NULL,
	charge_days INTEGER NOT NULL,
	amount_kopeks INTEGER NOT NULL,
	currency VARCHAR(10) NOT NULL,
	status VARCHAR(20) NOT NULL,
	redirect_url TEXT,
	next_charge_at TIMESTAMP WITH TIME ZONE,
	last_charge_at TIMESTAMP WITH TIME ZONE,
	last_charge_external_id VARCHAR(255),
	charges_success INTEGER NOT NULL,
	charges_failed INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(subscription_id) REFERENCES subscriptions (id) ON DELETE CASCADE,
	FOREIGN KEY(tariff_id) REFERENCES tariffs (id)
);

CREATE UNIQUE INDEX ix_platega_subscriptions_platega_subscription_id ON platega_subscriptions (platega_subscription_id);

CREATE INDEX ix_platega_subscriptions_user_active ON platega_subscriptions (user_id, status);

CREATE INDEX ix_platega_subscriptions_user_id ON platega_subscriptions (user_id);

CREATE INDEX ix_platega_subscriptions_id ON platega_subscriptions (id);

CREATE UNIQUE INDEX uq_platega_subscriptions_alive ON platega_subscriptions (subscription_id) WHERE status IN ('PENDING', 'ACTIVE', 'PAST_DUE');

CREATE INDEX ix_platega_subscriptions_subscription_id ON platega_subscriptions (subscription_id);

CREATE TABLE lava_subscriptions (
	id SERIAL NOT NULL,
	user_id INTEGER NOT NULL,
	subscription_id INTEGER NOT NULL,
	tariff_id INTEGER,
	lava_subscription_id VARCHAR(255),
	lava_product_id VARCHAR(255) NOT NULL,
	lava_consumer_id VARCHAR(255),
	order_id VARCHAR(255) NOT NULL,
	charge_days INTEGER NOT NULL,
	amount_kopeks INTEGER NOT NULL,
	currency VARCHAR(10) NOT NULL,
	status VARCHAR(20) NOT NULL,
	redirect_url TEXT,
	next_charge_at TIMESTAMP WITH TIME ZONE,
	last_charge_at TIMESTAMP WITH TIME ZONE,
	last_charge_external_id VARCHAR(255),
	charges_success INTEGER NOT NULL,
	charges_failed INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(subscription_id) REFERENCES subscriptions (id) ON DELETE CASCADE,
	FOREIGN KEY(tariff_id) REFERENCES tariffs (id)
);

CREATE INDEX ix_lava_subscriptions_id ON lava_subscriptions (id);

CREATE INDEX ix_lava_subscriptions_subscription_id ON lava_subscriptions (subscription_id);

CREATE UNIQUE INDEX ix_lava_subscriptions_order_id ON lava_subscriptions (order_id);

CREATE UNIQUE INDEX ix_lava_subscriptions_lava_subscription_id ON lava_subscriptions (lava_subscription_id);

CREATE INDEX ix_lava_subscriptions_user_active ON lava_subscriptions (user_id, status);

CREATE INDEX ix_lava_subscriptions_user_id ON lava_subscriptions (user_id);

CREATE UNIQUE INDEX uq_lava_subscriptions_alive ON lava_subscriptions (subscription_id) WHERE status IN ('PENDING', 'ACTIVE', 'PAST_DUE');

CREATE TABLE cloudpayments_payments (
	id SERIAL NOT NULL,
	user_id INTEGER,
	transaction_id_cp BIGINT,
	invoice_id VARCHAR(255) NOT NULL,
	amount_kopeks INTEGER NOT NULL,
	currency VARCHAR(10) NOT NULL,
	description TEXT,
	status VARCHAR(50) NOT NULL,
	is_paid BOOLEAN,
	paid_at TIMESTAMP WITH TIME ZONE,
	card_first_six VARCHAR(6),
	card_last_four VARCHAR(4),
	card_type VARCHAR(50),
	card_exp_date VARCHAR(10),
	token VARCHAR(255),
	payment_url TEXT,
	email VARCHAR(255),
	test_mode BOOLEAN,
	metadata_json JSON,
	callback_payload JSON,
	transaction_id INTEGER,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);

CREATE UNIQUE INDEX ix_cloudpayments_payments_invoice_id ON cloudpayments_payments (invoice_id);

CREATE INDEX ix_cloudpayments_payments_id ON cloudpayments_payments (id);

CREATE UNIQUE INDEX ix_cloudpayments_payments_transaction_id_cp ON cloudpayments_payments (transaction_id_cp);

CREATE TABLE freekassa_payments (
	id SERIAL NOT NULL,
	user_id INTEGER,
	order_id VARCHAR(64) NOT NULL,
	freekassa_order_id VARCHAR(64),
	amount_kopeks INTEGER NOT NULL,
	currency VARCHAR(10) NOT NULL,
	description TEXT,
	status VARCHAR(32) NOT NULL,
	is_paid BOOLEAN,
	payment_url TEXT,
	payment_system_id INTEGER,
	metadata_json JSON,
	callback_payload JSON,
	paid_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	transaction_id INTEGER,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);

CREATE UNIQUE INDEX ix_freekassa_payments_freekassa_order_id ON freekassa_payments (freekassa_order_id);

CREATE INDEX ix_freekassa_payments_id ON freekassa_payments (id);

CREATE UNIQUE INDEX ix_freekassa_payments_order_id ON freekassa_payments (order_id);

CREATE TABLE kassa_ai_payments (
	id SERIAL NOT NULL,
	user_id INTEGER,
	order_id VARCHAR(64) NOT NULL,
	kassa_ai_order_id VARCHAR(64),
	amount_kopeks INTEGER NOT NULL,
	currency VARCHAR(10) NOT NULL,
	description TEXT,
	status VARCHAR(32) NOT NULL,
	is_paid BOOLEAN,
	payment_url TEXT,
	payment_system_id INTEGER,
	metadata_json JSON,
	callback_payload JSON,
	paid_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	transaction_id INTEGER,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);

CREATE UNIQUE INDEX ix_kassa_ai_payments_kassa_ai_order_id ON kassa_ai_payments (kassa_ai_order_id);

CREATE INDEX ix_kassa_ai_payments_id ON kassa_ai_payments (id);

CREATE UNIQUE INDEX ix_kassa_ai_payments_order_id ON kassa_ai_payments (order_id);

CREATE TABLE riopay_payments (
	id SERIAL NOT NULL,
	user_id INTEGER,
	order_id VARCHAR(64) NOT NULL,
	riopay_order_id VARCHAR(64),
	amount_kopeks INTEGER NOT NULL,
	currency VARCHAR(10) NOT NULL,
	description TEXT,
	status VARCHAR(32) NOT NULL,
	is_paid BOOLEAN,
	payment_url TEXT,
	payment_method VARCHAR(32),
	metadata_json JSON,
	callback_payload JSON,
	paid_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	transaction_id INTEGER,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);

CREATE INDEX ix_riopay_payments_user_id ON riopay_payments (user_id);

CREATE UNIQUE INDEX ix_riopay_payments_riopay_order_id ON riopay_payments (riopay_order_id);

CREATE UNIQUE INDEX ix_riopay_payments_order_id ON riopay_payments (order_id);

CREATE INDEX ix_riopay_payments_id ON riopay_payments (id);

CREATE TABLE severpay_payments (
	id SERIAL NOT NULL,
	user_id INTEGER,
	order_id VARCHAR(64) NOT NULL,
	severpay_id VARCHAR(64),
	severpay_uid VARCHAR(64),
	amount_kopeks INTEGER NOT NULL,
	currency VARCHAR(10) NOT NULL,
	description TEXT,
	status VARCHAR(32) NOT NULL,
	is_paid BOOLEAN,
	payment_url TEXT,
	payment_method VARCHAR(32),
	metadata_json JSON,
	callback_payload JSON,
	paid_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	transaction_id INTEGER,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);

CREATE INDEX ix_severpay_payments_user_id ON severpay_payments (user_id);

CREATE UNIQUE INDEX ix_severpay_payments_severpay_id ON severpay_payments (severpay_id);

CREATE INDEX ix_severpay_payments_id ON severpay_payments (id);

CREATE UNIQUE INDEX ix_severpay_payments_order_id ON severpay_payments (order_id);

CREATE UNIQUE INDEX ix_severpay_payments_severpay_uid ON severpay_payments (severpay_uid);

CREATE TABLE paypear_payments (
	id SERIAL NOT NULL,
	user_id INTEGER,
	order_id VARCHAR(64) NOT NULL,
	paypear_id VARCHAR(64),
	amount_kopeks INTEGER NOT NULL,
	currency VARCHAR(10) NOT NULL,
	description TEXT,
	status VARCHAR(32) NOT NULL,
	is_paid BOOLEAN,
	payment_url TEXT,
	payment_method VARCHAR(32),
	metadata_json JSON,
	callback_payload JSON,
	paid_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	transaction_id INTEGER,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);

CREATE UNIQUE INDEX ix_paypear_payments_paypear_id ON paypear_payments (paypear_id);

CREATE UNIQUE INDEX ix_paypear_payments_order_id ON paypear_payments (order_id);

CREATE INDEX ix_paypear_payments_id ON paypear_payments (id);

CREATE INDEX ix_paypear_payments_user_id ON paypear_payments (user_id);

CREATE TABLE rollypay_payments (
	id SERIAL NOT NULL,
	user_id INTEGER,
	order_id VARCHAR(64) NOT NULL,
	rollypay_payment_id VARCHAR(128),
	amount_kopeks INTEGER NOT NULL,
	currency VARCHAR(10) NOT NULL,
	description TEXT,
	status VARCHAR(32) NOT NULL,
	is_paid BOOLEAN,
	payment_url TEXT,
	payment_method VARCHAR(32),
	metadata_json JSON,
	callback_payload JSON,
	paid_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	transaction_id INTEGER,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);

CREATE UNIQUE INDEX ix_rollypay_payments_order_id ON rollypay_payments (order_id);

CREATE UNIQUE INDEX ix_rollypay_payments_rollypay_payment_id ON rollypay_payments (rollypay_payment_id);

CREATE INDEX ix_rollypay_payments_id ON rollypay_payments (id);

CREATE INDEX ix_rollypay_payments_user_id ON rollypay_payments (user_id);

CREATE TABLE overpay_payments (
	id SERIAL NOT NULL,
	user_id INTEGER,
	order_id VARCHAR(64) NOT NULL,
	overpay_payment_id VARCHAR(128),
	amount_kopeks INTEGER NOT NULL,
	currency VARCHAR(10) NOT NULL,
	description TEXT,
	status VARCHAR(32) NOT NULL,
	is_paid BOOLEAN,
	payment_url TEXT,
	payment_method VARCHAR(32),
	metadata_json JSON,
	callback_payload JSON,
	paid_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	transaction_id INTEGER,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);

CREATE INDEX ix_overpay_payments_id ON overpay_payments (id);

CREATE INDEX ix_overpay_payments_user_id ON overpay_payments (user_id);

CREATE UNIQUE INDEX ix_overpay_payments_order_id ON overpay_payments (order_id);

CREATE UNIQUE INDEX ix_overpay_payments_overpay_payment_id ON overpay_payments (overpay_payment_id);

CREATE TABLE aurapay_payments (
	id SERIAL NOT NULL,
	user_id INTEGER,
	order_id VARCHAR(64) NOT NULL,
	aurapay_invoice_id VARCHAR(128),
	amount_kopeks INTEGER NOT NULL,
	currency VARCHAR(10) NOT NULL,
	description TEXT,
	status VARCHAR(32) NOT NULL,
	is_paid BOOLEAN,
	payment_url TEXT,
	payment_method VARCHAR(32),
	metadata_json JSON,
	callback_payload JSON,
	paid_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	transaction_id INTEGER,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);

CREATE INDEX ix_aurapay_payments_user_id ON aurapay_payments (user_id);

CREATE UNIQUE INDEX ix_aurapay_payments_aurapay_invoice_id ON aurapay_payments (aurapay_invoice_id);

CREATE UNIQUE INDEX ix_aurapay_payments_order_id ON aurapay_payments (order_id);

CREATE INDEX ix_aurapay_payments_id ON aurapay_payments (id);

CREATE TABLE etoplatezhi_payments (
	id SERIAL NOT NULL,
	user_id INTEGER,
	order_id VARCHAR(64) NOT NULL,
	etoplatezhi_payment_id VARCHAR(128),
	amount_kopeks INTEGER NOT NULL,
	currency VARCHAR(10) NOT NULL,
	description TEXT,
	status VARCHAR(32) NOT NULL,
	is_paid BOOLEAN,
	payment_url TEXT,
	payment_method VARCHAR(32),
	metadata_json JSON,
	callback_payload JSON,
	paid_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	transaction_id INTEGER,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);

CREATE INDEX ix_etoplatezhi_payments_id ON etoplatezhi_payments (id);

CREATE UNIQUE INDEX ix_etoplatezhi_payments_order_id ON etoplatezhi_payments (order_id);

CREATE UNIQUE INDEX ix_etoplatezhi_payments_etoplatezhi_payment_id ON etoplatezhi_payments (etoplatezhi_payment_id);

CREATE INDEX ix_etoplatezhi_payments_user_id ON etoplatezhi_payments (user_id);

CREATE TABLE antilopay_payments (
	id SERIAL NOT NULL,
	user_id INTEGER,
	order_id VARCHAR(64) NOT NULL,
	antilopay_payment_id VARCHAR(128),
	amount_kopeks INTEGER NOT NULL,
	currency VARCHAR(10) NOT NULL,
	description TEXT,
	status VARCHAR(32) NOT NULL,
	is_paid BOOLEAN,
	payment_url TEXT,
	payment_method VARCHAR(32),
	metadata_json JSON,
	callback_payload JSON,
	paid_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	transaction_id INTEGER,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);

CREATE UNIQUE INDEX ix_antilopay_payments_antilopay_payment_id ON antilopay_payments (antilopay_payment_id);

CREATE INDEX ix_antilopay_payments_user_id ON antilopay_payments (user_id);

CREATE UNIQUE INDEX ix_antilopay_payments_order_id ON antilopay_payments (order_id);

CREATE INDEX ix_antilopay_payments_id ON antilopay_payments (id);

CREATE TABLE jupiter_payments (
	id SERIAL NOT NULL,
	user_id INTEGER,
	order_id VARCHAR(64) NOT NULL,
	jupiter_transaction_id VARCHAR(128),
	amount_kopeks INTEGER NOT NULL,
	currency VARCHAR(10) NOT NULL,
	description TEXT,
	status VARCHAR(32) NOT NULL,
	is_paid BOOLEAN,
	payment_url TEXT,
	payment_method VARCHAR(32),
	metadata_json JSON,
	callback_payload JSON,
	paid_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	transaction_id INTEGER,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);

CREATE UNIQUE INDEX ix_jupiter_payments_order_id ON jupiter_payments (order_id);

CREATE INDEX ix_jupiter_payments_id ON jupiter_payments (id);

CREATE UNIQUE INDEX ix_jupiter_payments_jupiter_transaction_id ON jupiter_payments (jupiter_transaction_id);

CREATE INDEX ix_jupiter_payments_user_id ON jupiter_payments (user_id);

CREATE TABLE donut_payments (
	id SERIAL NOT NULL,
	user_id INTEGER,
	order_id VARCHAR(64) NOT NULL,
	donut_transaction_id VARCHAR(128),
	amount_kopeks INTEGER NOT NULL,
	currency VARCHAR(10) NOT NULL,
	description TEXT,
	status VARCHAR(32) NOT NULL,
	is_paid BOOLEAN,
	payment_url TEXT,
	payment_method VARCHAR(32),
	metadata_json JSON,
	callback_payload JSON,
	paid_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	transaction_id INTEGER,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);

CREATE INDEX ix_donut_payments_user_id ON donut_payments (user_id);

CREATE UNIQUE INDEX ix_donut_payments_order_id ON donut_payments (order_id);

CREATE UNIQUE INDEX ix_donut_payments_donut_transaction_id ON donut_payments (donut_transaction_id);

CREATE INDEX ix_donut_payments_id ON donut_payments (id);

CREATE TABLE lava_payments (
	id SERIAL NOT NULL,
	user_id INTEGER,
	order_id VARCHAR(64) NOT NULL,
	lava_invoice_id VARCHAR(128),
	amount_kopeks INTEGER NOT NULL,
	currency VARCHAR(10) NOT NULL,
	description TEXT,
	status VARCHAR(32) NOT NULL,
	is_paid BOOLEAN,
	payment_url TEXT,
	payment_method VARCHAR(32),
	metadata_json JSON,
	callback_payload JSON,
	paid_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	transaction_id INTEGER,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);

CREATE UNIQUE INDEX ix_lava_payments_order_id ON lava_payments (order_id);

CREATE UNIQUE INDEX ix_lava_payments_lava_invoice_id ON lava_payments (lava_invoice_id);

CREATE INDEX ix_lava_payments_id ON lava_payments (id);

CREATE INDEX ix_lava_payments_user_id ON lava_payments (user_id);

CREATE TABLE cispay_payments (
	id SERIAL NOT NULL,
	user_id INTEGER,
	order_id VARCHAR(64) NOT NULL,
	cispay_payment_id VARCHAR(64),
	amount_kopeks INTEGER NOT NULL,
	charged_amount_kopeks INTEGER,
	currency VARCHAR(10) NOT NULL,
	description TEXT,
	status VARCHAR(32) NOT NULL,
	is_paid BOOLEAN,
	payment_url TEXT,
	payment_method VARCHAR(32),
	metadata_json JSON,
	callback_payload JSON,
	paid_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	transaction_id INTEGER,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);

CREATE UNIQUE INDEX ix_cispay_payments_order_id ON cispay_payments (order_id);

CREATE INDEX ix_cispay_payments_user_id ON cispay_payments (user_id);

CREATE UNIQUE INDEX ix_cispay_payments_cispay_payment_id ON cispay_payments (cispay_payment_id);

CREATE INDEX ix_cispay_payments_id ON cispay_payments (id);

CREATE TABLE tabpay_payments (
	id SERIAL NOT NULL,
	user_id INTEGER,
	order_id VARCHAR(64) NOT NULL,
	tabpay_payment_id VARCHAR(64),
	amount_kopeks INTEGER NOT NULL,
	commission_kopeks INTEGER,
	currency VARCHAR(10) NOT NULL,
	description TEXT,
	status VARCHAR(32) NOT NULL,
	is_paid BOOLEAN,
	is_test BOOLEAN NOT NULL,
	payment_url TEXT,
	payment_method VARCHAR(32),
	metadata_json JSON,
	callback_payload JSON,
	processed_events JSON,
	paid_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	transaction_id INTEGER,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);

CREATE UNIQUE INDEX ix_tabpay_payments_tabpay_payment_id ON tabpay_payments (tabpay_payment_id);

CREATE INDEX ix_tabpay_payments_id ON tabpay_payments (id);

CREATE INDEX ix_tabpay_payments_user_id ON tabpay_payments (user_id);

CREATE UNIQUE INDEX ix_tabpay_payments_order_id ON tabpay_payments (order_id);

CREATE TABLE paritypay_payments (
	id SERIAL NOT NULL,
	user_id INTEGER,
	order_id VARCHAR(64) NOT NULL,
	paritypay_payment_id VARCHAR(64),
	amount_kopeks INTEGER NOT NULL,
	credited_kopeks INTEGER,
	currency VARCHAR(10) NOT NULL,
	description TEXT,
	status VARCHAR(32) NOT NULL,
	is_paid BOOLEAN,
	payment_url TEXT,
	payment_method VARCHAR(32),
	metadata_json JSON,
	callback_payload JSON,
	processed_events JSON,
	paid_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	transaction_id INTEGER,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id)
);

CREATE INDEX ix_paritypay_payments_id ON paritypay_payments (id);

CREATE UNIQUE INDEX ix_paritypay_payments_paritypay_payment_id ON paritypay_payments (paritypay_payment_id);

CREATE UNIQUE INDEX ix_paritypay_payments_order_id ON paritypay_payments (order_id);

CREATE INDEX ix_paritypay_payments_user_id ON paritypay_payments (user_id);

CREATE TABLE grace_access_sessions (
	id VARCHAR(36) NOT NULL,
	subscription_id INTEGER NOT NULL,
	remnawave_id BIGINT,
	remnawave_uuid VARCHAR(255),
	reason VARCHAR(16) NOT NULL,
	incident_key VARCHAR(255) NOT NULL,
	state VARCHAR(16) NOT NULL,
	snapshot_version INTEGER DEFAULT '2' NOT NULL,
	version INTEGER DEFAULT '1' NOT NULL,
	billing_before JSON NOT NULL,
	panel_before JSON NOT NULL,
	overlay JSON NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	grace_until TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	completion_reason VARCHAR(16),
	completed_at TIMESTAMP WITH TIME ZONE,
	last_error TEXT,
	PRIMARY KEY (id),
	CONSTRAINT uq_grace_access_sessions_incident UNIQUE (subscription_id, incident_key),
	CONSTRAINT ck_grace_access_sessions_reason CHECK (reason IN ('expired', 'limited')),
	CONSTRAINT ck_grace_access_sessions_state CHECK (state IN ('pending', 'active', 'restoring', 'completed')),
	CONSTRAINT ck_grace_access_sessions_completion CHECK (
            (
                state = 'completed'
                AND completion_reason IS NOT NULL
                AND completion_reason IN ('paid', 'timeout', 'drained', 'conflict', 'revoked')
                AND completed_at IS NOT NULL
            )
            OR
            (
                state <> 'completed'
                AND completion_reason IS NULL
                AND completed_at IS NULL
            )
            ),
	CONSTRAINT ck_grace_access_sessions_dates CHECK (grace_until > started_at),
	CONSTRAINT ck_grace_access_sessions_snapshot_version CHECK (snapshot_version > 0),
	CONSTRAINT ck_grace_access_sessions_version CHECK (version > 0),
	FOREIGN KEY(subscription_id) REFERENCES subscriptions (id) ON DELETE CASCADE
);

CREATE INDEX ix_grace_access_sessions_remnawave_id ON grace_access_sessions (remnawave_id);

CREATE UNIQUE INDEX uq_grace_access_sessions_one_open ON grace_access_sessions (subscription_id) WHERE state IN ('pending', 'active', 'restoring');

CREATE INDEX ix_grace_access_sessions_state_until ON grace_access_sessions (state, grace_until);

CREATE TABLE traffic_purchases (
	id SERIAL NOT NULL,
	subscription_id INTEGER NOT NULL,
	traffic_gb INTEGER NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(subscription_id) REFERENCES subscriptions (id) ON DELETE CASCADE
);

CREATE INDEX ix_traffic_purchases_id ON traffic_purchases (id);

CREATE INDEX ix_traffic_purchases_expires_at ON traffic_purchases (expires_at);

CREATE INDEX ix_traffic_purchases_created_at ON traffic_purchases (created_at);

CREATE INDEX ix_traffic_purchases_sub_expires ON traffic_purchases (subscription_id, expires_at);

CREATE TABLE promocode_uses (
	id SERIAL NOT NULL,
	promocode_id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	used_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT uq_promocode_uses_user_promo UNIQUE (user_id, promocode_id),
	FOREIGN KEY(promocode_id) REFERENCES promocodes (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX ix_promocode_uses_id ON promocode_uses (id);

CREATE TABLE coupons (
	id SERIAL NOT NULL,
	batch_id INTEGER NOT NULL,
	token VARCHAR(64) NOT NULL,
	status VARCHAR(20) NOT NULL,
	redeemed_by INTEGER,
	redeemed_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
	PRIMARY KEY (id),
	FOREIGN KEY(batch_id) REFERENCES coupon_batches (id) ON DELETE CASCADE,
	FOREIGN KEY(redeemed_by) REFERENCES users (id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX ix_coupons_token ON coupons (token);

CREATE INDEX ix_coupons_batch_status ON coupons (batch_id, status);

CREATE INDEX ix_coupons_redeemed_by ON coupons (redeemed_by);

CREATE INDEX ix_coupons_id ON coupons (id);

CREATE TABLE referral_earnings (
	id SERIAL NOT NULL,
	user_id INTEGER NOT NULL,
	referral_id INTEGER NOT NULL,
	amount_kopeks INTEGER NOT NULL,
	reason VARCHAR(100) NOT NULL,
	reward_type VARCHAR(10) DEFAULT 'money' NOT NULL,
	level INTEGER DEFAULT '1' NOT NULL,
	days_granted INTEGER DEFAULT '0' NOT NULL,
	tariff_id INTEGER,
	referral_transaction_id INTEGER,
	campaign_id INTEGER,
	created_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(referral_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(tariff_id) REFERENCES tariffs (id) ON DELETE SET NULL,
	FOREIGN KEY(referral_transaction_id) REFERENCES transactions (id),
	FOREIGN KEY(campaign_id) REFERENCES advertising_campaigns (id) ON DELETE SET NULL
);

CREATE INDEX ix_referral_earnings_referral_id ON referral_earnings (referral_id);

CREATE INDEX ix_referral_earnings_id ON referral_earnings (id);

CREATE INDEX ix_referral_earnings_user_id ON referral_earnings (user_id);

CREATE INDEX ix_referral_earnings_campaign_id ON referral_earnings (campaign_id);

CREATE TABLE referral_contest_events (
	id SERIAL NOT NULL,
	contest_id INTEGER NOT NULL,
	referrer_id INTEGER NOT NULL,
	referral_id INTEGER NOT NULL,
	event_type VARCHAR(50) NOT NULL,
	amount_kopeks INTEGER NOT NULL,
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_referral_contest_referral UNIQUE (contest_id, referral_id),
	FOREIGN KEY(contest_id) REFERENCES referral_contests (id) ON DELETE CASCADE,
	FOREIGN KEY(referrer_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(referral_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX idx_referral_contest_referrer ON referral_contest_events (contest_id, referrer_id);

CREATE INDEX ix_referral_contest_events_id ON referral_contest_events (id);

CREATE TABLE referral_contest_virtual_participants (
	id SERIAL NOT NULL,
	contest_id INTEGER NOT NULL,
	display_name VARCHAR(255) NOT NULL,
	referral_count INTEGER NOT NULL,
	total_amount_kopeks INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(contest_id) REFERENCES referral_contests (id) ON DELETE CASCADE
);

CREATE INDEX ix_referral_contest_virtual_participants_id ON referral_contest_virtual_participants (id);

CREATE TABLE sent_notifications (
	id SERIAL NOT NULL,
	user_id INTEGER NOT NULL,
	subscription_id INTEGER NOT NULL,
	notification_type VARCHAR(50) NOT NULL,
	days_before INTEGER,
	created_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(subscription_id) REFERENCES subscriptions (id) ON DELETE CASCADE
);

CREATE INDEX ix_sent_notifications_id ON sent_notifications (id);

CREATE TABLE subscription_events (
	id SERIAL NOT NULL,
	event_type VARCHAR(50) NOT NULL,
	user_id INTEGER NOT NULL,
	subscription_id INTEGER,
	transaction_id INTEGER,
	amount_kopeks INTEGER,
	currency VARCHAR(16),
	message TEXT,
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
	extra JSON,
	created_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(subscription_id) REFERENCES subscriptions (id) ON DELETE SET NULL,
	FOREIGN KEY(transaction_id) REFERENCES transactions (id) ON DELETE SET NULL
);

CREATE INDEX ix_subscription_events_id ON subscription_events (id);

CREATE TABLE discount_offers (
	id SERIAL NOT NULL,
	user_id INTEGER NOT NULL,
	subscription_id INTEGER,
	notification_type VARCHAR(50) NOT NULL,
	discount_percent INTEGER NOT NULL,
	bonus_amount_kopeks INTEGER NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	claimed_at TIMESTAMP WITH TIME ZONE,
	is_active BOOLEAN NOT NULL,
	effect_type VARCHAR(50) NOT NULL,
	extra_data JSON,
	created_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(subscription_id) REFERENCES subscriptions (id) ON DELETE SET NULL
);

CREATE INDEX ix_discount_offers_id ON discount_offers (id);

CREATE INDEX ix_discount_offers_user_type ON discount_offers (user_id, notification_type);

CREATE TABLE poll_questions (
	id SERIAL NOT NULL,
	poll_id INTEGER NOT NULL,
	text TEXT NOT NULL,
	"order" INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(poll_id) REFERENCES polls (id) ON DELETE CASCADE
);

CREATE INDEX ix_poll_questions_id ON poll_questions (id);

CREATE INDEX ix_poll_questions_poll_id ON poll_questions (poll_id);

CREATE TABLE poll_responses (
	id SERIAL NOT NULL,
	poll_id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	sent_at TIMESTAMP WITH TIME ZONE NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE,
	completed_at TIMESTAMP WITH TIME ZONE,
	reward_given BOOLEAN NOT NULL,
	reward_amount_kopeks INTEGER NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_poll_user UNIQUE (poll_id, user_id),
	FOREIGN KEY(poll_id) REFERENCES polls (id) ON DELETE CASCADE,
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX ix_poll_responses_poll_id ON poll_responses (poll_id);

CREATE INDEX ix_poll_responses_user_id ON poll_responses (user_id);

CREATE INDEX ix_poll_responses_id ON poll_responses (id);

CREATE TABLE subscription_servers (
	id SERIAL NOT NULL,
	subscription_id INTEGER NOT NULL,
	server_squad_id INTEGER NOT NULL,
	connected_at TIMESTAMP WITH TIME ZONE,
	paid_price_kopeks INTEGER,
	PRIMARY KEY (id),
	FOREIGN KEY(subscription_id) REFERENCES subscriptions (id) ON DELETE CASCADE,
	FOREIGN KEY(server_squad_id) REFERENCES server_squads (id)
);

CREATE INDEX ix_subscription_servers_id ON subscription_servers (id);

CREATE INDEX ix_subscription_servers_subscription_id ON subscription_servers (subscription_id);

CREATE TABLE support_audit_logs (
	id SERIAL NOT NULL,
	actor_user_id INTEGER,
	actor_telegram_id BIGINT,
	is_moderator BOOLEAN,
	action VARCHAR(50) NOT NULL,
	ticket_id INTEGER,
	target_user_id INTEGER,
	details JSON,
	created_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(actor_user_id) REFERENCES users (id) ON DELETE SET NULL,
	FOREIGN KEY(ticket_id) REFERENCES tickets (id) ON DELETE SET NULL,
	FOREIGN KEY(target_user_id) REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX ix_support_audit_logs_id ON support_audit_logs (id);

CREATE TABLE advertising_campaign_registrations (
	id SERIAL NOT NULL,
	campaign_id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	bonus_type VARCHAR(20) NOT NULL,
	balance_bonus_kopeks INTEGER,
	subscription_duration_days INTEGER,
	tariff_id INTEGER,
	tariff_duration_days INTEGER,
	created_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT uq_campaign_user UNIQUE (campaign_id, user_id),
	FOREIGN KEY(campaign_id) REFERENCES advertising_campaigns (id) ON DELETE CASCADE,
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(tariff_id) REFERENCES tariffs (id) ON DELETE SET NULL
);

CREATE INDEX ix_advertising_campaign_registrations_id ON advertising_campaign_registrations (id);

CREATE INDEX ix_campaign_reg_user_created ON advertising_campaign_registrations (user_id, created_at);

CREATE TABLE ticket_messages (
	id SERIAL NOT NULL,
	ticket_id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	message_text TEXT NOT NULL,
	is_from_admin BOOLEAN NOT NULL,
	has_media BOOLEAN,
	media_type VARCHAR(20),
	media_file_id VARCHAR(255),
	media_caption TEXT,
	media_items JSONB,
	created_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(ticket_id) REFERENCES tickets (id) ON DELETE CASCADE,
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX ix_ticket_messages_id ON ticket_messages (id);

CREATE TABLE wheel_spins (
	id SERIAL NOT NULL,
	user_id INTEGER NOT NULL,
	prize_id INTEGER,
	payment_type VARCHAR(50) NOT NULL,
	payment_amount INTEGER NOT NULL,
	payment_value_kopeks INTEGER NOT NULL,
	prize_type VARCHAR(50) NOT NULL,
	prize_value INTEGER NOT NULL,
	prize_display_name VARCHAR(100) NOT NULL,
	prize_value_kopeks INTEGER NOT NULL,
	generated_promocode_id INTEGER,
	telegram_charge_id VARCHAR(255),
	is_applied BOOLEAN NOT NULL,
	applied_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(prize_id) REFERENCES wheel_prizes (id) ON DELETE SET NULL,
	FOREIGN KEY(generated_promocode_id) REFERENCES promocodes (id),
	UNIQUE (telegram_charge_id)
);

CREATE INDEX ix_wheel_spins_user_created ON wheel_spins (user_id, created_at);

CREATE INDEX ix_wheel_spins_id ON wheel_spins (id);

CREATE TABLE ticket_notifications (
	id SERIAL NOT NULL,
	ticket_id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	notification_type VARCHAR(50) NOT NULL,
	message TEXT,
	is_for_admin BOOLEAN NOT NULL,
	is_read BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE,
	read_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(ticket_id) REFERENCES tickets (id) ON DELETE CASCADE,
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX ix_ticket_notifications_id ON ticket_notifications (id);

CREATE INDEX ix_ticket_notifications_user_id ON ticket_notifications (user_id);

CREATE INDEX ix_ticket_notifications_ticket_id ON ticket_notifications (ticket_id);

CREATE INDEX ix_ticket_notifications_user_read ON ticket_notifications (user_id, is_read);

CREATE INDEX ix_ticket_notifications_admin_read ON ticket_notifications (is_for_admin, is_read);

CREATE TABLE user_roles (
	id SERIAL NOT NULL,
	user_id INTEGER NOT NULL,
	role_id INTEGER NOT NULL,
	assigned_by INTEGER,
	assigned_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
	expires_at TIMESTAMP WITH TIME ZONE,
	is_active BOOLEAN NOT NULL,
	revocation_source VARCHAR(20),
	PRIMARY KEY (id),
	CONSTRAINT uq_user_role UNIQUE (user_id, role_id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(role_id) REFERENCES admin_roles (id) ON DELETE CASCADE,
	FOREIGN KEY(assigned_by) REFERENCES users (id) ON DELETE SET NULL
);

CREATE TABLE access_policies (
	id SERIAL NOT NULL,
	name VARCHAR(200) NOT NULL,
	description TEXT,
	role_id INTEGER,
	priority INTEGER NOT NULL,
	effect VARCHAR(10) NOT NULL,
	conditions JSONB NOT NULL,
	resource VARCHAR(100) NOT NULL,
	actions JSONB NOT NULL,
	is_active BOOLEAN NOT NULL,
	created_by INTEGER,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
	PRIMARY KEY (id),
	FOREIGN KEY(role_id) REFERENCES admin_roles (id) ON DELETE CASCADE,
	FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE SET NULL
);

CREATE TABLE reachability_jobs (
	id SERIAL NOT NULL,
	kind VARCHAR(16) NOT NULL,
	status VARCHAR(16) NOT NULL,
	phase VARCHAR(32),
	trigger VARCHAR(16) NOT NULL,
	started_by_user_id INTEGER,
	batch_id INTEGER,
	idempotency_key VARCHAR(64) NOT NULL,
	external_id INTEGER,
	last_request_id VARCHAR(64),
	request JSON NOT NULL,
	targets JSON NOT NULL,
	units_requested JSON,
	units_resolved JSON,
	units_effective JSON,
	skipped JSON,
	dpi VARCHAR(8) NOT NULL,
	estimated_kopeks INTEGER,
	estimate_is_exact BOOLEAN NOT NULL,
	cost_kopeks INTEGER,
	refunded_kopeks INTEGER,
	result JSON,
	error_code VARCHAR(64),
	error_message TEXT,
	retryable BOOLEAN,
	attempts INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE,
	started_at TIMESTAMP WITH TIME ZONE,
	finished_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(started_by_user_id) REFERENCES users (id) ON DELETE SET NULL,
	FOREIGN KEY(batch_id) REFERENCES reachability_batches (id) ON DELETE SET NULL,
	UNIQUE (idempotency_key)
);

CREATE INDEX ix_reachability_jobs_batch_id ON reachability_jobs (batch_id);

CREATE INDEX ix_reachability_jobs_started_by_user_id ON reachability_jobs (started_by_user_id);

CREATE INDEX ix_reachability_jobs_kind_created ON reachability_jobs (kind, created_at);

CREATE INDEX ix_reachability_jobs_id ON reachability_jobs (id);

CREATE INDEX ix_reachability_jobs_external_id ON reachability_jobs (external_id);

CREATE INDEX ix_reachability_jobs_status ON reachability_jobs (status);

CREATE TABLE subscription_temporary_access (
	id SERIAL NOT NULL,
	subscription_id INTEGER NOT NULL,
	offer_id INTEGER NOT NULL,
	squad_uuid VARCHAR(255) NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE,
	deactivated_at TIMESTAMP WITH TIME ZONE,
	is_active BOOLEAN NOT NULL,
	was_already_connected BOOLEAN NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(subscription_id) REFERENCES subscriptions (id) ON DELETE CASCADE,
	FOREIGN KEY(offer_id) REFERENCES discount_offers (id) ON DELETE CASCADE
);

CREATE INDEX ix_subscription_temporary_access_id ON subscription_temporary_access (id);

CREATE TABLE promo_offer_logs (
	id SERIAL NOT NULL,
	user_id INTEGER,
	offer_id INTEGER,
	action VARCHAR(50) NOT NULL,
	source VARCHAR(100),
	percent INTEGER,
	effect_type VARCHAR(50),
	details JSON,
	created_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL,
	FOREIGN KEY(offer_id) REFERENCES discount_offers (id) ON DELETE SET NULL
);

CREATE INDEX ix_promo_offer_logs_user_id ON promo_offer_logs (user_id);

CREATE INDEX ix_promo_offer_logs_offer_id ON promo_offer_logs (offer_id);

CREATE INDEX ix_promo_offer_logs_id ON promo_offer_logs (id);

CREATE TABLE poll_options (
	id SERIAL NOT NULL,
	question_id INTEGER NOT NULL,
	text TEXT NOT NULL,
	"order" INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(question_id) REFERENCES poll_questions (id) ON DELETE CASCADE
);

CREATE INDEX ix_poll_options_id ON poll_options (id);

CREATE INDEX ix_poll_options_question_id ON poll_options (question_id);

CREATE TABLE reachability_legs (
	id SERIAL NOT NULL,
	job_id INTEGER NOT NULL,
	kind VARCHAR(16) NOT NULL,
	target_key VARCHAR(255) NOT NULL,
	target_kind VARCHAR(32),
	target_ref VARCHAR(255),
	op_key VARCHAR(64) NOT NULL,
	operator VARCHAR(32),
	region VARCHAR(32),
	dpi VARCHAR(8),
	verdict VARCHAR(16) NOT NULL,
	matches_expectation BOOLEAN,
	raw JSON,
	checked_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(job_id) REFERENCES reachability_jobs (id) ON DELETE CASCADE
);

CREATE INDEX ix_reachability_legs_job_id ON reachability_legs (job_id);

CREATE INDEX ix_reachability_legs_id ON reachability_legs (id);

CREATE INDEX ix_reachability_legs_target_unit_time ON reachability_legs (target_key, op_key, checked_at);

CREATE TABLE poll_answers (
	id SERIAL NOT NULL,
	response_id INTEGER NOT NULL,
	question_id INTEGER NOT NULL,
	option_id INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_poll_answer_unique UNIQUE (response_id, question_id),
	FOREIGN KEY(response_id) REFERENCES poll_responses (id) ON DELETE CASCADE,
	FOREIGN KEY(question_id) REFERENCES poll_questions (id) ON DELETE CASCADE,
	FOREIGN KEY(option_id) REFERENCES poll_options (id) ON DELETE CASCADE
);

CREATE INDEX ix_poll_answers_id ON poll_answers (id);

CREATE INDEX ix_poll_answers_response_id ON poll_answers (response_id);

CREATE INDEX ix_poll_answers_option_id ON poll_answers (option_id);

CREATE INDEX ix_poll_answers_question_id ON poll_answers (question_id);

CREATE OR REPLACE FUNCTION guard_open_grace_subscription_delete()
                    RETURNS trigger AS $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM grace_access_sessions
                            WHERE subscription_id = OLD.id
                              AND state IN ('pending', 'active', 'restoring')
                        ) THEN
                            RAISE EXCEPTION 'subscription has an open grace-access session'
                                USING ERRCODE = '23503';
                        END IF;
                        RETURN OLD;
                    END;
                    $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_guard_open_grace_subscription_delete
                        BEFORE DELETE ON subscriptions
                        FOR EACH ROW EXECUTE FUNCTION guard_open_grace_subscription_delete();

CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num));

INSERT INTO alembic_version VALUES ('0127');
