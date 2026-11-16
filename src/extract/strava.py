from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
import time
import webbrowser
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Identity,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

LOGGER = logging.getLogger("strava")
API_BASE_URL = "https://www.strava.com/api/v3"
OAUTH_AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
OAUTH_TOKEN_URL = "https://www.strava.com/oauth/token"
PAGE_SIZE = 200
REQUEST_TIMEOUT_SECONDS = 30
STREAM_KEYS = "time,distance,altitude,heartrate,velocity_smooth"
REQUIRED_SCOPES = {"activity:read_all"}
DEFAULT_REDIRECT_URI = "http://localhost/exchange_token"
DEFAULT_TOKEN_PATH = Path("data/strava_tokens.json")

metadata = MetaData()

raw_strava_activities = Table(
    "raw_strava_activities",
    metadata,
    Column(
        "raw_activity_id",
        Integer().with_variant(BigInteger, "postgresql"),
        Identity(),
        primary_key=True,
    ),
    Column("strava_activity_id", BigInteger, nullable=False),
    Column(
        "payload",
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
    ),
    Column("streams", JSON().with_variant(JSONB, "postgresql")),
    Column("streams_status", String(20), nullable=False, server_default="pending"),
    Column(
        "ingested_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Index(
        "ix_raw_strava_activities_strava_ingested",
        "strava_activity_id",
        "ingested_at",
    ),
)

strava_sync_state = Table(
    "strava_sync_state",
    metadata,
    Column("sync_name", String(50), primary_key=True),
    Column("sync_mode", String(20), nullable=False),
    Column("backfill_before", BigInteger, nullable=False),
    Column("incremental_after", BigInteger),
    Column("incremental_before", BigInteger),
    Column("page", Integer, nullable=False),
    Column("latest_activity_at", DateTime(timezone=True)),
    Column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
)


class StravaActivity(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    type: str
    sport_type: str | None = None
    start_date: datetime

    @property
    def is_run(self) -> bool:
        return self.sport_type == "Run" or self.type == "Run"


class StravaStream(BaseModel):
    model_config = ConfigDict(extra="allow")

    data: list[Any]


class StravaTokens(BaseModel):
    access_token: str
    refresh_token: str
    expires_at: int
    expires_in: int | None = None
    token_type: str = "Bearer"


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    redirect_uri: str
    token_path: Path
    database_url: str | None = None

    @classmethod
    def from_environment(cls, require_database: bool) -> Settings:
        load_dotenv()
        client_id = os.getenv("STRAVA_CLIENT_ID", "").strip()
        client_secret = os.getenv("STRAVA_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise ValueError(
                "Configure STRAVA_CLIENT_ID e STRAVA_CLIENT_SECRET no arquivo .env."
            )

        database_url = os.getenv("DATABASE_URL", "").strip() or None
        if require_database and not database_url:
            raise ValueError("Configure DATABASE_URL no arquivo .env.")

        token_path = Path(os.getenv("STRAVA_TOKEN_FILE", str(DEFAULT_TOKEN_PATH)))
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=os.getenv("STRAVA_REDIRECT_URI", DEFAULT_REDIRECT_URI).strip(),
            token_path=token_path,
            database_url=database_url,
        )


class StravaError(RuntimeError):
    """Base exception for Strava extraction failures."""


class DailyRateLimitReached(StravaError):
    """Raised when the app has exhausted its daily read request limit."""


class TransientAPIError(StravaError):
    """Raised for transient Strava API server errors."""


class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> StravaTokens | None:
        if not self.path.exists():
            return None
        try:
            return StravaTokens.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as error:
            raise StravaError(
                f"Não foi possível ler os tokens em {self.path}."
            ) from error

    def save(self, tokens: StravaTokens) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_name(
            f"{self.path.name}.{secrets.token_hex(4)}.tmp"
        )
        try:
            temporary_path.write_text(
                tokens.model_dump_json(),
                encoding="utf-8",
            )
            os.replace(temporary_path, self.path)
        except OSError as error:
            raise StravaError(
                f"Não foi possível salvar os tokens em {self.path}."
            ) from error
        finally:
            if temporary_path.exists():
                temporary_path.unlink()


class StravaClient:
    def __init__(
        self,
        settings: Settings,
        session: requests.Session | None = None,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self.token_store = TokenStore(settings.token_path)
        self.tokens = self.token_store.load()
        self.oauth_state: str | None = None
        self.read_usage: tuple[int, int] | None = None
        self.read_limits: tuple[int, int] | None = None
        self.short_limit_reset_at: float | None = None
        self.rate_headers_seen = False

    def authorization_url(self) -> str:
        self.oauth_state = secrets.token_urlsafe(24)
        return f"{OAUTH_AUTHORIZE_URL}?{
            urlencode(
                {
                    'client_id': self.settings.client_id,
                    'redirect_uri': self.settings.redirect_uri,
                    'response_type': 'code',
                    'approval_prompt': 'auto',
                    'scope': 'activity:read_all',
                    'state': self.oauth_state,
                }
            )
        }"

    def authorize(self, callback_url: str) -> None:
        query = parse_qs(urlsplit(callback_url.strip()).query)
        code = self._query_value(query, "code")
        returned_state = self._query_value(query, "state")
        scopes = set(self._query_value(query, "scope").split(","))
        if not code:
            raise StravaError("A URL de retorno não contém o código de autorização.")
        if not self.oauth_state or returned_state != self.oauth_state:
            raise StravaError(
                "O state do OAuth não confere; inicie a autorização novamente."
            )
        if not REQUIRED_SCOPES.issubset(scopes):
            raise StravaError(
                "A autorização precisa incluir activity:read_all. "
                "Revogue o acesso no Strava e autorize novamente."
            )

        response = self.session.post(
            OAUTH_TOKEN_URL,
            data={
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
                "code": code,
                "grant_type": "authorization_code",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        self._raise_for_status(response)
        self._save_tokens(response.json())
        LOGGER.info(
            "Autorização Strava concluída; tokens salvos em %s.",
            self.settings.token_path,
        )

    @staticmethod
    def _query_value(query: dict[str, list[str]], key: str) -> str:
        return query.get(key, [""])[0]

    def _save_tokens(self, payload: dict[str, Any]) -> None:
        try:
            tokens = StravaTokens.model_validate(payload)
        except ValidationError as error:
            raise StravaError(
                "A resposta OAuth do Strava não contém tokens válidos."
            ) from error
        self.token_store.save(tokens)
        self.tokens = tokens

    def _access_token(self, force_refresh: bool = False) -> str:
        if self.tokens is None:
            self.tokens = self.token_store.load()
        if self.tokens is None:
            raise StravaError(
                "Tokens ausentes. Execute `python -m src.extract.strava --authorize`."
            )

        if force_refresh or self.tokens.expires_at <= int(time.time()) + 60:
            response = self.session.post(
                OAUTH_TOKEN_URL,
                data={
                    "client_id": self.settings.client_id,
                    "client_secret": self.settings.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": self.tokens.refresh_token,
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            self._raise_for_status(response)
            self._save_tokens(response.json())
        return self.tokens.access_token

    @staticmethod
    def _raise_for_status(response: requests.Response) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            raise StravaError(
                f"Requisição Strava falhou com HTTP {response.status_code}."
            ) from error

    @staticmethod
    def _parse_rate_header(value: str | None) -> tuple[int, int] | None:
        if not value:
            return None
        try:
            short_term, daily = (
                int(part.strip()) for part in value.split(",", maxsplit=1)
            )
        except (ValueError, TypeError):
            LOGGER.warning(
                "Cabeçalho de limite do Strava em formato inesperado: %r.", value
            )
            return None
        return short_term, daily

    @staticmethod
    def _next_quarter_hour(now: datetime | None = None) -> float:
        current = now or datetime.now(UTC)
        next_minute = ((current.minute // 15) + 1) * 15
        if next_minute == 60:
            reset = current.replace(minute=0, second=0, microsecond=0) + timedelta(
                hours=1
            )
        else:
            reset = current.replace(
                minute=next_minute,
                second=0,
                microsecond=0,
            )
        return reset.timestamp() + 2

    def _record_rate_headers(self, response: requests.Response) -> None:
        headers = response.headers
        self.read_usage = self._parse_rate_header(
            headers.get("X-ReadRateLimit-Usage") or headers.get("X-RateLimit-Usage")
        )
        self.read_limits = self._parse_rate_header(
            headers.get("X-ReadRateLimit-Limit") or headers.get("X-RateLimit-Limit")
        )
        if self.read_usage and self.read_limits:
            self.rate_headers_seen = True
            if self.read_usage[0] >= self.read_limits[0]:
                self.short_limit_reset_at = self._next_quarter_hour()
        elif not self.rate_headers_seen:
            LOGGER.warning(
                "Resposta sem cabeçalhos de limite de leitura do Strava; "
                "o consumo da aplicação não pôde ser conferido."
            )

    def _wait_for_rate_limit(self) -> None:
        if self.read_usage and self.read_limits:
            if self.read_usage[1] >= self.read_limits[1]:
                raise DailyRateLimitReached(
                    "O limite diário de leitura do Strava foi atingido; "
                    "execute a extração novamente após a meia-noite UTC."
                )
            if self.read_usage[0] >= self.read_limits[0]:
                reset_at = self.short_limit_reset_at or self._next_quarter_hour()
                delay = max(0, reset_at - time.time())
                if delay:
                    LOGGER.warning(
                        "Limite de leitura de 15 minutos atingido; aguardando %.0f segundos.",
                        delay,
                    )
                    time.sleep(delay)
                self.read_usage = (0, self.read_usage[1])
                self.short_limit_reset_at = None

    def _handle_rate_limited(self, response: requests.Response) -> None:
        self._record_rate_headers(response)
        retry_after = response.headers.get("Retry-After")
        try:
            delay = max(0, int(retry_after)) if retry_after else 0
        except ValueError:
            delay = 0

        if (
            self.read_usage
            and self.read_limits
            and self.read_usage[1] >= self.read_limits[1]
        ):
            raise DailyRateLimitReached(
                "Limite diário de leitura atingido; retome a extração após a meia-noite UTC."
            )
        if delay > 15 * 60:
            raise DailyRateLimitReached(
                "O Strava solicitou uma pausa superior a 15 minutos; "
                "a execução foi interrompida e pode ser retomada mais tarde."
            )

        reset_at = self._next_quarter_hour()
        delay = max(delay, int(max(0, reset_at - time.time())))
        if delay:
            LOGGER.warning("Strava retornou HTTP 429; aguardando %s segundos.", delay)
            time.sleep(delay)
        if self.read_usage is not None:
            self.read_usage = (0, self.read_usage[1])
        self.short_limit_reset_at = None

    def get_json(
        self,
        path: str,
        params: dict[str, int] | None = None,
        allow_not_found: bool = False,
    ) -> Any:
        self._wait_for_rate_limit()
        response = self._get_with_retry(path, params)
        self._record_rate_headers(response)
        if response.status_code == 401:
            response = self._get_with_retry(path, params, force_refresh=True)
            self._record_rate_headers(response)
        if response.status_code == 404 and allow_not_found:
            LOGGER.warning("Strava não disponibilizou streams para a atividade.")
            return None
        if response.status_code == 429:
            self._handle_rate_limited(response)
            raise TransientAPIError("Strava retornou HTTP 429 após aguardar o limite.")
        self._raise_for_status(response)
        return response.json()

    @retry(
        retry=retry_if_exception_type(
            (requests.ConnectionError, requests.Timeout, TransientAPIError)
        ),
        stop=stop_after_attempt(4),
        wait=wait_exponential_jitter(initial=1, max=16),
        reraise=True,
    )
    def _get_with_retry(
        self,
        path: str,
        params: dict[str, int] | None = None,
        force_refresh: bool = False,
    ) -> requests.Response:
        response = self.session.get(
            f"{API_BASE_URL}{path}",
            params=params,
            headers={"Authorization": f"Bearer {self._access_token(force_refresh)}"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code >= 500:
            raise TransientAPIError(
                f"Erro temporário HTTP {response.status_code} do Strava."
            )
        if response.status_code == 429:
            self._handle_rate_limited(response)
            raise TransientAPIError("Strava retornou HTTP 429 após aguardar o limite.")
        return response

    def activity_page(
        self,
        page: int,
        before: int,
        after: int | None = None,
    ) -> list[dict[str, Any]]:
        params = {"page": page, "per_page": PAGE_SIZE, "before": before}
        if after is not None:
            params["after"] = after
        activities = self.get_json("/athlete/activities", params=params)
        if not isinstance(activities, list):
            raise StravaError(
                "A API do Strava retornou uma página de atividades inválida."
            )
        return activities

    def activity_streams(self, activity_id: int) -> dict[str, Any] | None:
        data = self.get_json(
            f"/activities/{activity_id}/streams",
            params={
                "keys": STREAM_KEYS,
                "key_by_type": 1,
            },
            allow_not_found=True,
        )
        if data is None:
            return None
        if not isinstance(data, dict):
            raise StravaError(f"Streams inválidos para a atividade {activity_id}.")
        try:
            return {
                name: stream.model_dump(mode="json")
                for name, stream in {
                    key: StravaStream.model_validate(value)
                    for key, value in data.items()
                }.items()
            }
        except ValidationError as error:
            raise StravaError(
                f"Formato de streams inválido para a atividade {activity_id}."
            ) from error


def _activity_started_at(payload: dict[str, Any]) -> datetime:
    try:
        activity = StravaActivity.model_validate(payload)
    except ValidationError as error:
        raise StravaError(
            "A API retornou uma atividade em formato inválido."
        ) from error
    start_date = activity.start_date
    if start_date.tzinfo is None:
        start_date = start_date.replace(tzinfo=UTC)
    return start_date.astimezone(UTC)


def _ensure_sync_state(engine: Engine) -> None:
    with engine.begin() as connection:
        state = connection.execute(
            select(strava_sync_state.c.sync_name).where(
                strava_sync_state.c.sync_name == "activities"
            )
        ).scalar_one_or_none()
        if state is None:
            now = datetime.now(UTC)
            connection.execute(
                insert(strava_sync_state).values(
                    sync_name="activities",
                    sync_mode="backfill",
                    backfill_before=int(now.timestamp()),
                    page=1,
                    updated_at=now,
                )
            )


def _read_sync_state(engine: Engine) -> dict[str, Any]:
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(strava_sync_state).where(
                    strava_sync_state.c.sync_name == "activities"
                )
            )
            .mappings()
            .one()
        )
        return dict(row)


def _update_sync_state(engine: Engine, values: dict[str, Any]) -> None:
    values["updated_at"] = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            update(strava_sync_state)
            .where(strava_sync_state.c.sync_name == "activities")
            .values(**values)
        )


def _known_activity_ids(engine: Engine) -> set[int]:
    with engine.connect() as connection:
        return set(
            connection.execute(
                select(raw_strava_activities.c.strava_activity_id)
            ).scalars()
        )


def _pending_streams(engine: Engine) -> list[tuple[int, int, dict[str, Any]]]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                raw_strava_activities.c.raw_activity_id,
                raw_strava_activities.c.strava_activity_id,
                raw_strava_activities.c.payload,
            ).where(raw_strava_activities.c.streams_status == "pending")
        ).all()
    return [(row.raw_activity_id, row.strava_activity_id, row.payload) for row in rows]


def _persist_pending_streams(
    engine: Engine,
    raw_activity_id: int,
    streams: dict[str, Any] | None,
    status: str,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(raw_strava_activities)
            .where(raw_strava_activities.c.raw_activity_id == raw_activity_id)
            .values(streams=streams, streams_status=status)
        )


def _persist_page(
    engine: Engine,
    activities: list[tuple[int, dict[str, Any], dict[str, Any] | None, str]],
    state_values: dict[str, Any],
) -> None:
    with engine.begin() as connection:
        if activities:
            connection.execute(
                insert(raw_strava_activities),
                [
                    {
                        "strava_activity_id": activity_id,
                        "payload": payload,
                        "streams": streams,
                        "streams_status": status,
                    }
                    for activity_id, payload, streams, status in activities
                ],
            )
        state_values["updated_at"] = datetime.now(UTC)
        connection.execute(
            update(strava_sync_state)
            .where(strava_sync_state.c.sync_name == "activities")
            .values(**state_values)
        )


def _epoch_before(timestamp: datetime | None) -> int:
    if timestamp is None:
        return int(datetime.now(UTC).timestamp()) - 1
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return int(timestamp.timestamp()) - 1


def extract_activities(engine: Engine, client: StravaClient) -> int:
    """Backfill and incrementally persist activity payloads and run streams."""
    _ensure_sync_state(engine)
    known_ids = _known_activity_ids(engine)
    inserted_count = 0
    pending_count = 0

    for raw_activity_id, activity_id, _ in _pending_streams(engine):
        try:
            streams = client.activity_streams(activity_id)
        except DailyRateLimitReached:
            LOGGER.warning(
                "Limite diário atingido; streams pendentes serão retomados depois."
            )
            return inserted_count
        _persist_pending_streams(
            engine,
            raw_activity_id,
            streams,
            "unavailable" if streams is None else "available",
        )

    while True:
        state = _read_sync_state(engine)
        mode = state["sync_mode"]
        if mode == "idle":
            latest = state["latest_activity_at"]
            if latest is not None and latest.tzinfo is None:
                latest = latest.replace(tzinfo=UTC)
            incremental_after = _epoch_before(
                latest or datetime.fromtimestamp(state["backfill_before"], UTC)
            )
            incremental_before = int(datetime.now(UTC).timestamp())
            _update_sync_state(
                engine,
                {
                    "sync_mode": "incremental",
                    "incremental_after": incremental_after,
                    "incremental_before": incremental_before,
                    "page": 1,
                },
            )
            continue

        if mode == "backfill":
            before = state["backfill_before"]
            after = None
        elif mode == "incremental":
            before = state["incremental_before"]
            after = state["incremental_after"]
            if before is None or after is None:
                raise StravaError(
                    "Cursor incremental inválido no estado da sincronização."
                )
        else:
            raise StravaError(f"Modo de sincronização desconhecido: {mode}.")

        try:
            page = client.activity_page(
                page=state["page"],
                before=before,
                after=after,
            )
        except DailyRateLimitReached:
            LOGGER.warning(
                "Limite diário atingido; a próxima execução retomará a página atual."
            )
            break

        batch: list[tuple[int, dict[str, Any], dict[str, Any] | None, str]] = []
        latest_in_page: datetime | None = state["latest_activity_at"]
        reached_daily_limit = False
        remaining_activity_index = len(page)
        for index, raw_payload in enumerate(page):
            try:
                activity = StravaActivity.model_validate(raw_payload)
            except ValidationError as error:
                raise StravaError(
                    "A API retornou uma atividade em formato inválido."
                ) from error
            started_at = _activity_started_at(raw_payload)
            if latest_in_page is None or started_at > latest_in_page:
                latest_in_page = started_at
            if activity.id in known_ids:
                continue

            streams: dict[str, Any] | None = None
            status = "not_applicable"
            if activity.is_run:
                status = "available"
                try:
                    streams = client.activity_streams(activity.id)
                    status = "unavailable" if streams is None else "available"
                except DailyRateLimitReached:
                    status = "pending"
                    reached_daily_limit = True
                    remaining_activity_index = index + 1
            batch.append((activity.id, raw_payload, streams, status))
            if reached_daily_limit:
                break

        # Persist every activity in the page before advancing the checkpoint.
        # The overlap on the next incremental run and ID deduplication make retries safe.
        if reached_daily_limit:
            for raw_payload in page[remaining_activity_index:]:
                activity = StravaActivity.model_validate(raw_payload)
                started_at = _activity_started_at(raw_payload)
                if latest_in_page is None or started_at > latest_in_page:
                    latest_in_page = started_at
                if activity.id not in known_ids:
                    batch.append(
                        (
                            activity.id,
                            raw_payload,
                            None,
                            "pending" if activity.is_run else "not_applicable",
                        )
                    )

        complete_page = len(page) < PAGE_SIZE
        next_page = 1 if complete_page else state["page"] + 1
        next_mode = mode
        incremental_after = state["incremental_after"]
        incremental_before = state["incremental_before"]
        if mode == "backfill" and complete_page:
            next_mode = "incremental"
            incremental_after = _epoch_before(
                latest_in_page or datetime.fromtimestamp(state["backfill_before"], UTC)
            )
            incremental_before = int(datetime.now(UTC).timestamp())
        elif mode == "incremental" and complete_page:
            next_mode = "idle"
            incremental_after = None
            incremental_before = None

        _persist_page(
            engine,
            batch,
            {
                "sync_mode": next_mode,
                "backfill_before": state["backfill_before"],
                "incremental_after": incremental_after,
                "incremental_before": incremental_before,
                "page": next_page,
                "latest_activity_at": latest_in_page,
            },
        )
        inserted_count += len(batch)
        known_ids.update(activity_id for activity_id, _, _, _ in batch)
        pending_count += sum(1 for _, _, _, status in batch if status == "pending")

        if reached_daily_limit:
            LOGGER.warning(
                "Limite diário atingido; atividades sem streams serão retomadas."
            )
            break
        if complete_page and mode == "incremental":
            break

    LOGGER.info(
        "Extração concluída: %d atividades brutas inseridas; %d corridas aguardam streams.",
        inserted_count,
        pending_count,
    )
    return inserted_count


def _authorize(client: StravaClient) -> None:
    authorization_url = client.authorization_url()
    print("Autorize o acesso do app à sua conta Strava.")
    print("Escopo solicitado: activity:read_all")
    print(f"URL de autorização: {authorization_url}")
    webbrowser.open(authorization_url)
    callback_url = input(
        "Após autorizar, copie a URL completa da barra de endereço e cole aqui: "
    ).strip()
    client.authorize(callback_url)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Extrai atividades e streams da API do Strava."
    )
    parser.add_argument(
        "--authorize",
        action="store_true",
        help="executa o fluxo OAuth inicial e salva os tokens localmente",
    )
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_environment(require_database=not args.authorize)
        client = StravaClient(settings)
        if args.authorize:
            _authorize(client)
            return 0
        if settings.database_url is None:
            raise ValueError("Configure DATABASE_URL no arquivo .env.")

        from sqlalchemy import create_engine

        engine = create_engine(settings.database_url, pool_pre_ping=True)
        try:
            count = extract_activities(engine, client)
        finally:
            engine.dispose()
        LOGGER.info(
            "Execução finalizada; %d novas atividades gravadas na camada raw.", count
        )
        return 0
    except (
        DailyRateLimitReached,
        StravaError,
        ValueError,
        requests.RequestException,
    ) as error:
        LOGGER.error("%s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
