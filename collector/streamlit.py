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


def mask_secret(value: Optional[str]) -> str:
    if not value:
        return "-"
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


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


def get_available_values(settings: Settings, column: str) -> list[str]:
    if not table_exists(settings, "precos_combustiveis"):
        return []
    sql = f"""
        SELECT DISTINCT {column}
        FROM precos_combustiveis
        WHERE {column} IS NOT NULL
        ORDER BY {column}
    """
    df = query_dataframe(settings, sql)
    return df[column].astype(str).tolist() if not df.empty else []


def get_date_bounds(settings: Settings) -> tuple[Optional[pd.Timestamp], Optional[pd.Timestamp]]:
    if not table_exists(settings, "coletas"):
        return None, None
    df = query_dataframe(
        settings,
        """
        SELECT MIN(data_coleta) AS min_date, MAX(data_coleta) AS max_date
        FROM coletas
        """,
    )
    if df.empty:
        return None, None
    return df.loc[0, "min_date"], df.loc[0, "max_date"]


def get_summary(settings: Settings) -> pd.DataFrame:
    if not table_exists(settings, "coletas"):
        return pd.DataFrame()
    return query_dataframe(
        settings,
        """
        WITH coletas_cte AS (
            SELECT
                COUNT(*) AS total_coletas,
                MAX(data_coleta) AS ultima_coleta,
                AVG(tempo_execucao_segundos) AS media_execucao
            FROM coletas
        ),
        precos_cte AS (
            SELECT COUNT(*) AS total_precos
            FROM precos_combustiveis
        ),
        analises_cte AS (
            SELECT COUNT(*) AS total_analises
            FROM analises
        )
        SELECT
            coletas_cte.total_coletas,
            coletas_cte.ultima_coleta,
            ROUND(coletas_cte.media_execucao::numeric, 2) AS media_execucao,
            precos_cte.total_precos,
            analises_cte.total_analises
        FROM coletas_cte, precos_cte, analises_cte
        """,
    )


def get_last_analysis(settings: Settings) -> pd.DataFrame:
    if not table_exists(settings, "analises"):
        return pd.DataFrame()
    return query_dataframe(
        settings,
        """
        SELECT
            c.data_coleta,
            a.*
        FROM analises a
        JOIN coletas c ON c.id = a.coleta_id
        ORDER BY c.data_coleta DESC
        LIMIT 1
        """,
    )


def get_columns(settings: Settings, table_name: str) -> pd.DataFrame:
    if not table_exists(settings, table_name):
        return pd.DataFrame()
    return query_dataframe(
        settings,
        """
        SELECT
            column_name,
            data_type,
            is_nullable,
            ordinal_position
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table_name,),
    )


def get_latest_prices(settings: Settings, fuel: str) -> pd.DataFrame:
    if not table_exists(settings, "precos_combustiveis"):
        return pd.DataFrame()
    return query_dataframe(
        settings,
        """
        WITH latest AS (
            SELECT MAX(data_coleta) AS data_coleta
            FROM coletas
        )
        SELECT
            c.data_coleta,
            p.combustivel,
            p.estado,
            p.preco
        FROM precos_combustiveis p
        JOIN coletas c ON c.id = p.coleta_id
        JOIN latest l ON l.data_coleta = c.data_coleta
        WHERE p.combustivel = %s
        ORDER BY p.estado
        """,
        (fuel,),
    )


def get_price_history(
    settings: Settings,
    fuel: str,
    states: Sequence[str],
    start_date: Optional[pd.Timestamp],
    end_date: Optional[pd.Timestamp],
) -> pd.DataFrame:
    if not table_exists(settings, "precos_combustiveis"):
        return pd.DataFrame()

    state_filter = tuple(states) if states else None
    sql = """
        SELECT
            c.data_coleta,
            p.combustivel,
            p.estado,
            p.preco
        FROM precos_combustiveis p
        JOIN coletas c ON c.id = p.coleta_id
        WHERE p.combustivel = %s
    """
    params: list[Any] = [fuel]

    if start_date is not None:
        sql += " AND c.data_coleta >= %s"
        params.append(start_date)
    if end_date is not None:
        sql += " AND c.data_coleta < %s"
        params.append(end_date + pd.Timedelta(days=1))
    if state_filter:
        sql += " AND p.estado = ANY(%s)"
        params.append(list(state_filter))

    sql += " ORDER BY c.data_coleta, p.estado"
    df = query_dataframe(settings, sql, tuple(params))
    if not df.empty:
        df["data_coleta"] = pd.to_datetime(df["data_coleta"])
        df["preco"] = pd.to_numeric(df["preco"], errors="coerce")
    return df


def get_recent_collections(settings: Settings, limit: int = 20) -> pd.DataFrame:
    if not table_exists(settings, "coletas"):
        return pd.DataFrame()
    return query_dataframe(
        settings,
        """
        SELECT
            id,
            data_coleta,
            fonte,
            moeda,
            tempo_execucao_segundos
        FROM coletas
        ORDER BY data_coleta DESC
        LIMIT %s
        """,
        (limit,),
    )


def render_env_summary(settings: Settings) -> None:
    expected = [
        ("API_URL", settings.api_url, "opcional"),
        ("DATABASE_HOST", settings.db_host, "obrigatorio"),
        ("DATABASE_PORT", str(settings.db_port), "obrigatorio"),
        ("DATABASE_NAME", settings.db_name, "obrigatorio"),
        ("DATABASE_USER", settings.db_user, "obrigatorio"),
        ("DATABASE_PASSWORD", mask_secret(settings.db_password), "obrigatorio"),
        ("DATABASE_CONNECT_TIMEOUT", str(settings.db_connect_timeout), "default 10"),
        ("COLLECTOR_INTERVAL_SECONDS", str(settings.interval_seconds), "default 3600"),
        ("API_REQUEST_TIMEOUT_SECONDS", str(settings.request_timeout_seconds), "default 20"),
        ("STARTUP_RETRY_ATTEMPTS", str(settings.startup_retry_attempts), "default 30"),
        ("STARTUP_RETRY_DELAY_SECONDS", str(settings.startup_retry_delay_seconds), "default 5"),
    ]
    st.write("Valores usados pela interface e pelo collector:")
    st.dataframe(
        pd.DataFrame(expected, columns=["variavel", "valor", "observacao"]),
        use_container_width=True,
        hide_index=True,
    )


def render_schema_tab(settings: Settings) -> None:
    tables = ["coletas", "precos_combustiveis", "analises"]
    for table_name in tables:
        st.subheader(table_name)
        df = get_columns(settings, table_name)
        if df.empty:
            st.info("Tabela nao encontrada ou ainda sem metadados visiveis.")
            continue
        st.dataframe(
            df.rename(
                columns={
                    "column_name": "coluna",
                    "data_type": "tipo",
                    "is_nullable": "nulo?",
                    "ordinal_position": "ordem",
                }
            )[["ordem", "coluna", "tipo", "nulo?"]],
            use_container_width=True,
            hide_index=True,
        )


def render_summary_tab(settings: Settings) -> None:
    summary = get_summary(settings)
    latest_analysis = get_last_analysis(settings)
    recent_collections = get_recent_collections(settings, 10)

    if summary.empty:
        st.warning("Nao encontrei dados nas tabelas ainda. Assim que o collector rodar, o painel aparece aqui.")
        return

    row = summary.iloc[0]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Coletas", int(row["total_coletas"]))
    c2.metric("Precos", int(row["total_precos"]))
    c3.metric("Analises", int(row["total_analises"]))
    c4.metric("Media execucao", f'{float(row["media_execucao"]):.2f}s' if pd.notna(row["media_execucao"]) else "-")

    st.caption(
        f"Ultima coleta: {pd.to_datetime(row['ultima_coleta']).strftime('%d/%m/%Y %H:%M:%S') if pd.notna(row['ultima_coleta']) else '-'}"
    )

    left, right = st.columns([1.1, 0.9])
    with left:
        st.subheader("Ultima analise")
        if latest_analysis.empty:
            st.info("Sem linhas na tabela analises.")
        else:
            analysis_view = latest_analysis.drop(columns=["coleta_id"], errors="ignore").copy()
            st.dataframe(analysis_view, use_container_width=True, hide_index=True)

    with right:
        st.subheader("Ultimas coletas")
        if recent_collections.empty:
            st.info("Sem linhas na tabela coletas.")
        else:
            st.dataframe(recent_collections, use_container_width=True, hide_index=True)


def render_prices_tab(settings: Settings) -> None:
    fuels = get_available_values(settings, "combustivel")
    states = get_available_values(settings, "estado")
    min_date, max_date = get_date_bounds(settings)

    if not fuels or min_date is None or max_date is None:
        st.warning("Ainda nao ha dados suficientes para montar o grafico de precos.")
        return

    col1, col2, col3 = st.columns([1.4, 1.2, 1.2])
    with col1:
        fuel = st.selectbox("Combustivel", fuels, index=0)
    with col2:
        default_states = states[:5] if len(states) > 5 else states
        selected_states = st.multiselect("Estados", states, default=default_states)
    with col3:
        date_range = st.date_input(
            "Periodo",
            value=(pd.to_datetime(min_date).date(), pd.to_datetime(max_date).date()),
            min_value=pd.to_datetime(min_date).date(),
            max_value=pd.to_datetime(max_date).date(),
        )

    start_date: Optional[pd.Timestamp] = None
    end_date: Optional[pd.Timestamp] = None
    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_date = pd.Timestamp(date_range[0])
        end_date = pd.Timestamp(date_range[1])

    history = get_price_history(settings, fuel, selected_states, start_date, end_date)

    if history.empty:
        st.info("Nenhum preco encontrado com os filtros selecionados.")
        return

    history["data_coleta"] = pd.to_datetime(history["data_coleta"])
    history = history.sort_values(["estado", "data_coleta"])

    left, right = st.columns([1.25, 0.75])
    with left:
        fig = px.line(
            history,
            x="data_coleta",
            y="preco",
            color="estado",
            markers=True,
            title=f"Evolucao de precos - {fuel}",
            labels={
                "data_coleta": "Data da coleta",
                "preco": "Preco",
                "estado": "Estado",
            },
        )
        fig.update_layout(legend_title_text="Estado", height=560)
        st.plotly_chart(fig, use_container_width=True)

    with right:
        st.subheader("Resumo do filtro")
        summary = (
            history.groupby("estado", as_index=False)
            .agg(minimo=("preco", "min"), maximo=("preco", "max"), media=("preco", "mean"))
            .sort_values("media", ascending=False)
        )
        st.dataframe(summary, use_container_width=True, hide_index=True)

    st.subheader("Dados do grafico")
    st.dataframe(history, use_container_width=True, hide_index=True)

    st.subheader("Precos na ultima coleta")
    latest = get_latest_prices(settings, fuel)
    if latest.empty:
        st.info("Sem dados para a ultima coleta.")
    else:
        st.dataframe(latest, use_container_width=True, hide_index=True)


def build_layout(settings: Settings) -> None:
    st.markdown(
        """
        <style>
        .block-container {
            padding-top: 1.4rem;
            padding-bottom: 2rem;
        }
        .stMetric {
            border: 1px solid rgba(49, 51, 63, 0.15);
            border-radius: 14px;
            padding: 0.6rem 0.75rem;
            background: rgba(255, 255, 255, 0.65);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.title("Monitor de Precos do Collector")
    st.caption("Painel para acompanhar coletas, precos por estado e o historico salvo no Postgres.")

    with st.sidebar:
        st.header("Conexao")
        st.write(f"Host: `{settings.db_host}`")
        st.write(f"Banco: `{settings.db_name}`")
        st.write(f"Usuario: `{settings.db_user}`")
        st.write(f"Porta: `{settings.db_port}`")
        st.write(f"API: `{settings.api_url}`")
        st.divider()
        if st.button("Recarregar dados"):
            st.cache_data.clear()
            st.rerun()
        st.divider()
        render_env_summary(settings)

    tabs = st.tabs(["Resumo", "Precos", "Schema"])
    with tabs[0]:
        render_summary_tab(settings)
    with tabs[1]:
        render_prices_tab(settings)
    with tabs[2]:
        render_schema_tab(settings)


def main() -> None:
    st.set_page_config(
        page_title="RecOpsLog | Monitor de Precos",
        page_icon="P",
        layout="wide",
    )

    try:
        settings = load_settings()
    except Exception as exc:
        st.error(f"Configuracao invalida: {exc}")
        st.stop()

    try:
        build_layout(settings)
    except Exception as exc:
        st.error(f"Nao foi possivel carregar o painel: {exc}")
        st.stop()


if __name__ == "__main__":
    main()
