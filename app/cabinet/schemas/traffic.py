"""Schemas for admin traffic usage."""

import re
from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TrafficTopRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    period: Literal[1, 3, 7, 14, 30] = 7
    start_date: date | None = None
    end_date: date | None = None
    node_uuids: list[UUID] | None = Field(None, min_length=1, max_length=256)

    @field_validator('period', mode='before')
    @classmethod
    def integer_period(cls, value):
        if type(value) is not int:
            raise ValueError('period must be an integer')
        return value

    @field_validator('start_date', 'end_date', mode='before')
    @classmethod
    def calendar_date(cls, value):
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError('Dates must use YYYY-MM-DD')
        return value

    @model_validator(mode='after')
    def date_pair(self):
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError('Both start_date and end_date are required')
        if self.start_date is not None and 'period' in self.model_fields_set:
            raise ValueError('Use either period or custom dates')
        return self


class TrafficTopNode(BaseModel):
    uuid: str
    name: str
    country_code: str | None


class TrafficTopNodesResponse(BaseModel):
    nodes: list[TrafficTopNode]


class TrafficTopScope(BaseModel):
    kind: Literal['all', 'selected']
    node_uuids: list[str]
    node_count: int


class TrafficTopRange(BaseModel):
    start_date: str
    end_date: str
    days: int
    timezone: Literal['UTC'] = 'UTC'


class TrafficTopDay(BaseModel):
    date: str
    total_bytes: int


class TrafficTopLocalUser(BaseModel):
    id: int
    display_name: str


class TrafficTopItem(BaseModel):
    rank: int
    panel_user_id: int
    panel_username: str
    total_bytes: int
    mapping_status: Literal['matched', 'unmapped', 'ambiguous']
    local_user: TrafficTopLocalUser | None


class TrafficTopResponse(BaseModel):
    scope: TrafficTopScope
    range: TrafficTopRange
    top_limit: Literal[100] = 100
    returned_count: int
    all_recorded_total_bytes: int
    top_sum_bytes: int
    daily: list[TrafficTopDay]
    items: list[TrafficTopItem]
    generated_at: str


class TrafficNodeInfo(BaseModel):
    node_uuid: str
    node_name: str
    country_code: str


class SubscriptionTrafficInfo(BaseModel):
    """Per-subscription traffic metadata for multi-subscription display."""

    subscription_id: int
    tariff_name: str | None
    status: str | None
    traffic_limit_gb: float
    device_limit: int


class UserTrafficItem(BaseModel):
    user_id: int
    telegram_id: int | None
    username: str | None
    email: str | None
    full_name: str
    # Primary subscription fields (backward compat — reflect the active/first sub)
    tariff_name: str | None
    subscription_status: str | None
    traffic_limit_gb: float
    device_limit: int
    node_traffic: dict[str, int]  # {node_uuid: total_bytes}
    total_bytes: int
    # All subscriptions for multi-subscription display
    subscriptions: list[SubscriptionTrafficInfo] = Field(default_factory=list)


class TrafficUsageResponse(BaseModel):
    items: list[UserTrafficItem]
    nodes: list[TrafficNodeInfo]
    total: int
    offset: int
    limit: int
    period_days: int
    available_tariffs: list[str]
    available_statuses: list[str]


class SubscriptionEnrichmentInfo(BaseModel):
    """Per-subscription enrichment (dates) for multi-subscription display."""

    subscription_id: int
    tariff_name: str | None
    start_date: str | None
    end_date: str | None


class UserTrafficEnrichment(BaseModel):
    devices_connected: int = 0
    total_spent_kopeks: int = 0
    # Primary subscription dates (backward compat — reflect the active/first sub)
    subscription_start_date: str | None = None
    subscription_end_date: str | None = None
    last_node_name: str | None = None
    # All subscriptions for multi-subscription display
    subscriptions: list[SubscriptionEnrichmentInfo] = Field(default_factory=list)


class TrafficEnrichmentResponse(BaseModel):
    data: dict[int, UserTrafficEnrichment]


class ExportCsvRequest(BaseModel):
    period: int = Field(30, ge=1, le=30)
    start_date: str | None = None
    end_date: str | None = None
    tariffs: str | None = None
    statuses: str | None = None
    nodes: str | None = None
    total_threshold_gb: float | None = Field(None, ge=0, description='Total GB/day threshold for risk column')
    node_threshold_gb: float | None = Field(None, ge=0, description='Per-node GB/day threshold for risk column')


class ExportCsvResponse(BaseModel):
    success: bool
    message: str
