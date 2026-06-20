from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

import pandas as pd
import plotly.express as px
import psycopg2
import streamlit as st
from dotenv import load_dotenv


APP_DIR = Path(__file__).resolve().parent
DOTENV_PATH = APP_DIR / ".env"
API_URL_DEFAULT = "https://combustivelapi.com.br/api/precos/"

BRAZILIAN_STATES = [
    "AC",
    "AL",
    "AP",
    "AM",
    "BA",
    "CE",
    "DF",
    "ES",
    "GO",
    "MA",
    "MT",
    "MS",
    "MG",
    "PA",
    "PB",
    "PR",
    "PE",
    "PI",
    "RJ",
    "RN",
    "RS",
    "RO",
    "RR",
    "SC",
    "SP",
    "SE",
    "TO",
]

REGIONS = {
    "Norte": ["AC", "AP", "AM", "PA", "RO", "RR", "TO"],
    "Nordeste": ["AL", "BA", "CE", "MA", "PB", "PE", "PI", "RN", "SE"],
    "Centro-Oeste": ["DF", "GO", "MT", "MS"],
    "Sudeste": ["ES", "MG", "RJ", "SP"],
    "Sul": ["PR", "RS", "SC"],
}


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

    def db_config(self) -> dict[str, Any]:
        return {
            "host": self.db_host,
            "port": self.db_port,
            "dbname": self.db_name,
            "user": self.db_user,
            "password": self.db_password,
            "connect_timeout": self.db_connect_timeout,
        }


def read_env(name: str, default: Optional[str] = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    if value is None:
        raise RuntimeError(f"Environment variable {name} is not configured")
    return value


def read_env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer") from exc


def load_settings() -> Settings:
    load_dotenv(DOTENV_PATH, override=False)
    return Settings(
        api_url=read_env("API_URL", API_URL_DEFAULT),
        db_host=read_env("DATABASE_HOST", required=True),
        db_port=read_env_int("DATABASE_PORT", 5432),
        db_name=read_env("DATABASE_NAME", required=True),
        db_user=read_env("DATABASE_USER", required=True),
        db_password=read_env("DATABASE_PASSWORD", required=True),
        db_connect_timeout=read_env_int("DATABASE_CONNECT_TIMEOUT", 10),
        interval_seconds=read_env_int("COLLECTOR_INTERVAL_SECONDS", 3600),
        request_timeout_seconds=read_env_int("API_REQUEST_TIMEOUT_SECONDS", 20),
        startup_retry_attempts=read_env_int("STARTUP_RETRY_ATTEMPTS", 30),
        startup_retry_delay_seconds=read_env_int("STARTUP_RETRY_DELAY_SECONDS", 5),
    )


def connect_db(settings: Settings):
    return psycopg2.connect(**settings.db_config())


@st.cache_data(ttl=30)
def query_dataframe(settings: Settings, sql: str, params: Optional[Sequence[Any]] = None) -> pd.DataFrame:
    conn = None
    try:
        conn = connect_db(settings)
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        if conn is not None:
            conn.close()


@st.cache_data(ttl=30)
def fetch_scalar(settings: Settings, sql: str, params: Optional[Sequence[Any]] = None) -> Any:
    conn = None
    try:
        conn = connect_db(settings)
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            row = cursor.fetchone()
            return None if row is None else row[0]
    finally:
        if conn is not None:
            conn.close()


def table_exists(settings: Settings, table_name: str) -> bool:
    sql = """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = %s
        )
    """
    result = fetch_scalar(settings, sql, (table_name,))
    return bool(result)


def find_gasoline_name(settings: Settings) -> Optional[str]:
    if not table_exists(settings, "precos_combustiveis"):
        return None

    df = query_dataframe(
        settings,
        """
        SELECT DISTINCT combustivel
        FROM precos_combustiveis
        WHERE combustivel IS NOT NULL
        ORDER BY combustivel
        """,
    )
    if df.empty:
        return None

    candidates = df["combustivel"].astype(str).tolist()
    for candidate in candidates:
        normalized = candidate.strip().lower()
        if "gasolina" in normalized:
            return candidate
    return candidates[0]


def get_available_states(settings: Settings, fuel: str) -> list[str]:
    if not table_exists(settings, "precos_combustiveis"):
        return []
    df = query_dataframe(
        settings,
        """
        SELECT DISTINCT estado
        FROM precos_combustiveis
        WHERE combustivel = %s
          AND estado IS NOT NULL
        ORDER BY estado
        """,
        (fuel,),
    )
    if df.empty:
        return []
    return df["estado"].astype(str).tolist()


def get_price_history(
    settings: Settings,
    fuel: str,
    states: Sequence[str],
) -> pd.DataFrame:
    if not table_exists(settings, "precos_combustiveis"):
        return pd.DataFrame()

    sql = """
        SELECT
            DATE_TRUNC('day', c.data_coleta)::date AS data_dia,
            p.estado,
            AVG(p.preco::numeric) AS preco_medio
        FROM precos_combustiveis p
        JOIN coletas c ON c.id = p.coleta_id
        WHERE p.combustivel = %s
    """
    params: list[Any] = [fuel]

    if states:
        sql += " AND p.estado = ANY(%s)"
        params.append(list(states))

    sql += """
        GROUP BY 1, 2
        ORDER BY 1, 2
    """

    df = query_dataframe(settings, sql, tuple(params))
    if not df.empty:
        df["data_dia"] = pd.to_datetime(df["data_dia"])
        df["preco_medio"] = pd.to_numeric(df["preco_medio"], errors="coerce")
    return df


def apply_region_preset(region_name: str, available_states: list[str]) -> None:
    if region_name == "Todos":
        st.session_state.state_picker = available_states
        st.session_state.region_selected = "Todos"
        return

    region_states = REGIONS.get(region_name, [])
    selected = [state for state in region_states if state in available_states]
    st.session_state.state_picker = selected
    st.session_state.region_selected = region_name


def render_region_buttons(available_states: list[str]) -> None:
    st.caption("Regioes")
    buttons = st.columns(6)
    region_names = ["Todos", "Norte", "Nordeste", "Centro-Oeste", "Sudeste", "Sul"]

    for index, region_name in enumerate(region_names):
        with buttons[index]:
            pressed = st.button(region_name, use_container_width=True, key=f"region_{region_name}")
            if pressed:
                apply_region_preset(region_name, available_states)


def render_chart(settings: Settings, fuel: str) -> None:
    available_states = get_available_states(settings, fuel)
    if not available_states:
        st.warning("Nao encontrei estados para montar o grafico.")
        return

    if "region_selected" not in st.session_state:
        st.session_state.region_selected = "PE"
    if "state_picker" not in st.session_state:
        st.session_state.state_picker = ["PE"] if "PE" in available_states else available_states[:1]

    render_region_buttons(available_states)

    if st.session_state.region_selected == "PE":
        st.session_state.state_picker = ["PE"] if "PE" in available_states else available_states[:1]

    selected_states = st.multiselect(
        "Estados",
        options=available_states,
        default=st.session_state.state_picker,
        key="state_picker",
    )
    st.session_state.state_picker = selected_states

    history = get_price_history(settings, fuel, selected_states)
    if history.empty:
        st.info("Nenhum dado encontrado para os filtros atuais.")
        return

    fig = px.line(
        history,
        x="data_dia",
        y="preco_medio",
        color="estado",
        markers=True,
        title="Gasolina por estado ao longo do tempo",
        labels={
            "data_dia": "Dia",
            "preco_medio": "Preco medio",
            "estado": "Estado",
        },
    )
    fig.update_layout(
        height=620,
        legend_title_text="Estado",
        xaxis_title="Dia",
        yaxis_title="Preco medio",
    )

    st.plotly_chart(fig, use_container_width=True)
    st.dataframe(history, use_container_width=True, hide_index=True)


def build_layout(settings: Settings) -> None:
    st.markdown(
        """
        <style>
        .block-container {
            padding-top: 1.2rem;
            padding-bottom: 1.8rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.title("Monitor de preco da gasolina")
    st.caption("Grafico diario por estado com presets de regiao.")

    fuel_name = find_gasoline_name(settings)
    if not fuel_name:
        st.warning("Ainda nao consegui identificar a gasolina na base.")
        return

    render_chart(settings, fuel_name)


def main() -> None:
    st.set_page_config(
        page_title="Fuel Price Monitor",
        page_icon="P",
        layout="wide",
    )

    try:
        settings = load_settings()
    except Exception as exc:
        st.error(f"Configuracao invalida: {exc}")
        st.stop()

    with st.sidebar:
        if st.button("Recarregar dados"):
            st.cache_data.clear()
            st.rerun()

    try:
        build_layout(settings)
    except Exception as exc:
        st.error(f"Nao foi possivel carregar o painel: {exc}")
        st.stop()


if __name__ == "__main__":
    main()
