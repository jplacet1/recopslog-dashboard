import logging
import math
import os
import signal
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import psycopg2
import requests
from dotenv import load_dotenv


API_URL_DEFAULT = "https://combustivelapi.com.br/api/precos/"
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(message)s"
STOP_REQUESTED = False


def setup_logging() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )
    return logging.getLogger("collector")


logger = setup_logging()


def request_stop(signum: int, frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    logger.info("Stop signal received (%s). Shutting down...", signum)


signal.signal(signal.SIGTERM, request_stop)
signal.signal(signal.SIGINT, request_stop)


load_dotenv()


def env(name: str, default: Optional[str] = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    if value is None:
        raise RuntimeError(f"Environment variable {name} is not configured")
    return value


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer") from exc


@dataclass(frozen=True)
class Settings:
    api_url: str
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    db_connect_timeout: int
    interval_seconds: int
    request_timeout_seconds: int
    startup_retry_attempts: int
    startup_retry_delay_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            api_url=env("API_URL", API_URL_DEFAULT),
            db_host=env("DATABASE_HOST", required=True),
            db_port=env_int("DATABASE_PORT", 5432),
            db_name=env("DATABASE_NAME", required=True),
            db_user=env("DATABASE_USER", required=True),
            db_password=env("DATABASE_PASSWORD", required=True),
            db_connect_timeout=env_int("DATABASE_CONNECT_TIMEOUT", 10),
            interval_seconds=env_int("COLLECTOR_INTERVAL_SECONDS", 3600),
            request_timeout_seconds=env_int("API_REQUEST_TIMEOUT_SECONDS", 20),
            startup_retry_attempts=env_int("STARTUP_RETRY_ATTEMPTS", 30),
            startup_retry_delay_seconds=env_int("STARTUP_RETRY_DELAY_SECONDS", 5),
        )

    def db_config(self) -> Dict[str, Any]:
        return {
            "host": self.db_host,
            "port": self.db_port,
            "dbname": self.db_name,
            "user": self.db_user,
            "password": self.db_password,
            "connect_timeout": self.db_connect_timeout,
        }


def normalize_price(value: Any) -> float:
    if value is None:
        raise ValueError("Price value cannot be null")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("%", "")
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    return float(text)


def connect_db(settings: Settings):
    logger.info(
        "Connecting to PostgreSQL %s:%s/%s",
        settings.db_host,
        settings.db_port,
        settings.db_name,
    )
    return psycopg2.connect(**settings.db_config())


def wait_for_database(settings: Settings) -> None:
    logger.info("Waiting for database to become available...")

    last_error: Optional[Exception] = None
    for attempt in range(1, settings.startup_retry_attempts + 1):
        if STOP_REQUESTED:
            return
        try:
            conn = connect_db(settings)
            conn.close()
            logger.info("Database is ready")
            return
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Database not ready yet (%s/%s): %s",
                attempt,
                settings.startup_retry_attempts,
                exc,
            )
            if attempt < settings.startup_retry_attempts:
                time.sleep(settings.startup_retry_delay_seconds)

    raise RuntimeError("Database did not become ready") from last_error


def save_data(settings: Settings, payload: Dict[str, Any]) -> None:
    start_time = time.time()
    conn = None

    try:
        conn = connect_db(settings)
        cursor = conn.cursor()

        logger.info("Inserting coleta record")

        cursor.execute(
            """
            INSERT INTO coletas (
                data_coleta,
                fonte,
                moeda,
                tempo_execucao_segundos
            )
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (
                payload["data_coleta"],
                payload["fonte"],
                payload["moeda"],
                payload["tempo_execucao_segundos"],
            ),
        )

        coleta_id = cursor.fetchone()[0]
        total_prices = 0

        logger.info("Coleta created with id=%s", coleta_id)

        prices = payload.get("precos") or {}
        for combustivel, estados in prices.items():
            for estado, preco in estados.items():
                cursor.execute(
                    """
                    INSERT INTO precos_combustiveis (
                        coleta_id,
                        combustivel,
                        estado,
                        preco
                    )
                    VALUES (%s, %s, %s, %s)
                    """,
                    (coleta_id, combustivel, estado, normalize_price(preco)),
                )
                total_prices += 1

        logger.info("%s prices inserted", total_prices)

        analise = payload["analise"]

        cursor.execute(
            """
            INSERT INTO analises (
                coleta_id,
                estado_barato_gasolina,
                preco_barato_gasolina,
                estado_caro_gasolina,
                preco_caro_gasolina,
                diferenca_gasolina,
                variacao_percentual_gasolina,
                estado_barato_diesel,
                preco_barato_diesel,
                estado_caro_diesel,
                preco_caro_diesel,
                diferenca_diesel,
                variacao_percentual_diesel
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s
            )
            """,
            (
                coleta_id,
                analise["estado_mais_barato_gasolina"]["sigla"],
                normalize_price(analise["estado_mais_barato_gasolina"]["preco"]),
                analise["estado_mais_caro_gasolina"]["sigla"],
                normalize_price(analise["estado_mais_caro_gasolina"]["preco"]),
                normalize_price(analise["diferenca_gasolina"]),
                normalize_price(analise["variacao_percentual_gasolina"]),
                analise["estado_mais_barato_diesel"]["sigla"],
                normalize_price(analise["estado_mais_barato_diesel"]["preco"]),
                analise["estado_mais_caro_diesel"]["sigla"],
                normalize_price(analise["estado_mais_caro_diesel"]["preco"]),
                normalize_price(analise["diferenca_diesel"]),
                normalize_price(analise["variacao_percentual_diesel"]),
            ),
        )

        conn.commit()

        elapsed = round(time.time() - start_time, 2)
        logger.info("Database updated successfully in %ss", elapsed)

    except Exception:
        if conn:
            conn.rollback()
        logger.exception("Error while saving data")
        raise

    finally:
        if conn:
            conn.close()


def fetch_api(settings: Settings) -> Dict[str, Any]:
    logger.info("Requesting fuel API: %s", settings.api_url)

    headers = {
        "Accept": "application/json",
        "User-Agent": "recopslog-collector/1.0",
    }

    response = requests.get(
        settings.api_url,
        headers=headers,
        timeout=settings.request_timeout_seconds,
    )

    logger.info("API response status=%s", response.status_code)
    response.raise_for_status()

    preview = response.text[:200].replace("\n", " ")
    logger.info("API response preview=%s", preview)

    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(f"API returned error: {payload.get('message', 'unknown error')}")

    return payload


def collect_once(settings: Settings) -> None:
    started_at = time.time()
    payload = fetch_api(settings)

    payload.setdefault("tempo_execucao_segundos", 0)
    payload["tempo_execucao_segundos"] = round(time.time() - started_at, 2)

    save_data(settings, payload)
    logger.info("Collection finished")


def sleep_with_stop(total_seconds: int) -> None:
    remaining = max(total_seconds, 0)
    while remaining > 0 and not STOP_REQUESTED:
        chunk = min(remaining, 1)
        time.sleep(chunk)
        remaining -= chunk


def run_worker(settings: Settings) -> int:
    logger.info(
        "Collector started | interval=%ss | db=%s:%s/%s",
        settings.interval_seconds,
        settings.db_host,
        settings.db_port,
        settings.db_name,
    )

    wait_for_database(settings)

    while not STOP_REQUESTED:
        cycle_started = time.time()
        try:
            collect_once(settings)
        except Exception:
            logger.exception("Collector cycle failed")

        elapsed = time.time() - cycle_started
        sleep_time = max(0, math.ceil(settings.interval_seconds - elapsed))

        if sleep_time > 0 and not STOP_REQUESTED:
            logger.info("Sleeping for %ss before next cycle", sleep_time)
            sleep_with_stop(sleep_time)

    logger.info("Collector stopped")
    return 0


def main() -> int:
    try:
        settings = Settings.from_env()
    except Exception as exc:
        logger.exception("Invalid configuration: %s", exc)
        return 2

    try:
        return run_worker(settings)
    except Exception:
        logger.exception("Fatal collector error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
