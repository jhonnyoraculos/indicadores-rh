from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
from plotly.subplots import make_subplots
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Image as PdfImage
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import Column, Date, Float, Integer, MetaData, Table as SQLATable, create_engine, delete, insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool
import streamlit as st


BASE_DIR = Path(__file__).parent
LOGO_PATH = BASE_DIR / "logo-jr.png"
LEGACY_CSV_PATH = BASE_DIR / "dados_indicadores_rh.csv"
TABLE_NAME = "indicadores_rh"

COLUMNS = [
    "data",
    "admissoes",
    "desligamentos",
    "colaboradores",
    "horas_ausencia",
    "horas_programadas",
]


class DatabaseStorageError(RuntimeError):
    pass


METADATA = MetaData()
INDICADORES_TABLE = SQLATable(
    TABLE_NAME,
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("data", Date, nullable=False),
    Column("admissoes", Integer, nullable=False),
    Column("desligamentos", Integer, nullable=False),
    Column("colaboradores", Float, nullable=False),
    Column("horas_ausencia", Float, nullable=False),
    Column("horas_programadas", Float, nullable=False),
)
APP_STATE_TABLE = SQLATable(
    "app_state",
    METADATA,
    Column("key", Integer, primary_key=True, autoincrement=False),
    Column("value", Integer, nullable=False),
)


def read_secret(*keys: str) -> str:
    try:
        value = st.secrets
        for key in keys:
            if hasattr(value, "get"):
                value = value.get(key)
            elif isinstance(value, dict):
                value = value.get(key)
            else:
                return ""

            if value is None:
                return ""
    except Exception:
        return ""

    return str(value).strip()


def config_value(env_name: str, top_level_secret: str, nested_secret: str = "", default: str = "") -> str:
    return (
        os.environ.get(env_name, "").strip()
        or read_secret(top_level_secret)
        or (read_secret("database", nested_secret) if nested_secret else "")
        or default
    )


def configured_database_url() -> str:
    url = config_value("DATABASE_URL", "DATABASE_URL", "url")
    if not url:
        raise DatabaseStorageError(
            "Configure DATABASE_URL nos Secrets do Streamlit ou no ambiente local para usar um banco externo."
        )
    parsed = urlparse(url)
    database_name = parsed.path.lstrip("/")
    invalid_values = {"host", "usuario", "senha", "banco", ""}
    if (
        parsed.hostname in invalid_values
        or parsed.username in invalid_values
        or parsed.password in invalid_values
        or database_name in invalid_values
    ):
        raise DatabaseStorageError(
            "A DATABASE_URL ainda esta com valores de exemplo. Troque usuario, senha, host e banco pelos dados reais do seu Postgres."
        )
    return url


def database_url() -> str:
    url = configured_database_url()
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://") and not url.startswith("postgresql+"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


@st.cache_resource(show_spinner=False)
def get_engine(db_url: str) -> Engine:
    if db_url.startswith("sqlite"):
        return create_engine(db_url, future=True, connect_args={"check_same_thread": False})

    return create_engine(
        db_url,
        future=True,
        poolclass=NullPool,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": 10,
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 5,
        },
    )


def database_engine() -> Engine:
    return get_engine(database_url())


def storage_error_message(error: Exception) -> str:
    detail = str(error)
    normalized = detail.lower()

    if "ssl connection has been closed unexpectedly" in normalized or "consuming input failed" in normalized:
        return (
            "A conexao com o banco foi encerrada durante a consulta. "
            "Tente atualizar a pagina. Se continuar, o app vai precisar reconectar ao Neon."
        )
    if "failed to resolve host" in normalized:
        return "O host informado na DATABASE_URL nao foi encontrado. Revise a connection string do banco."
    if "password authentication failed" in normalized:
        return "Usuario ou senha do banco invalidos. Revise a DATABASE_URL configurada no Streamlit."
    if "connect timeout" in normalized or "timeout expired" in normalized:
        return "O banco demorou demais para responder. Tente novamente em alguns segundos."

    return f"Nao foi possivel conectar ao banco. Detalhe: {detail}"


def stop_with_storage_error(action: str, error: Exception) -> None:
    st.error(f"Nao foi possivel {action} os dados no banco. {storage_error_message(error)}")
    st.stop()


def persistence_label() -> str:
    return "no banco de dados"


def render_persistence_notice() -> None:
    st.caption("Salvamento ativo no banco de dados externo configurado em DATABASE_URL.")


def normalize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    if "colaboradores" not in df.columns:
        inicio = pd.to_numeric(df.get("colaboradores_inicio", 0), errors="coerce").fillna(0)
        fim = pd.to_numeric(df.get("colaboradores_fim", 0), errors="coerce").fillna(0)
        df["colaboradores"] = (inicio + fim) / 2

    for column in COLUMNS:
        if column not in df.columns:
            df[column] = 0

    result = df[COLUMNS].copy()
    result["data"] = pd.to_datetime(result["data"], errors="coerce").dt.date
    numeric_columns = [column for column in COLUMNS if column != "data"]
    result[numeric_columns] = result[numeric_columns].apply(pd.to_numeric, errors="coerce").fillna(0)
    return result.dropna(subset=["data"]).sort_values("data").reset_index(drop=True)


def database_records(df: pd.DataFrame) -> list[dict[str, object]]:
    return normalize_dataframe(df).to_dict(orient="records")


def ensure_database_ready() -> None:
    engine = database_engine()

    try:
        METADATA.create_all(engine)
        with engine.begin() as connection:
            migration_flag = connection.execute(
                select(APP_STATE_TABLE.c.value).where(APP_STATE_TABLE.c.key == 1)
            ).scalar_one_or_none()

            row_count = connection.execute(select(INDICADORES_TABLE.c.id)).first()
            if migration_flag is None and row_count is None and LEGACY_CSV_PATH.exists():
                legacy_df = pd.read_csv(LEGACY_CSV_PATH)
                records = database_records(legacy_df)
                if records:
                    connection.execute(insert(INDICADORES_TABLE), records)
                connection.execute(
                    insert(APP_STATE_TABLE).values(key=1, value=1)
                )
            elif migration_flag is None:
                connection.execute(insert(APP_STATE_TABLE).values(key=1, value=1))
    except (OSError, SQLAlchemyError, ValueError) as error:
        raise DatabaseStorageError(str(error)) from error


st.set_page_config(
    page_title="Indicadores RH | JR Ferragens",
    page_icon=str(LOGO_PATH) if LOGO_PATH.exists() else None,
    layout="wide",
)


def load_data() -> pd.DataFrame:
    try:
        ensure_database_ready()
        query = select(
            INDICADORES_TABLE.c.data,
            INDICADORES_TABLE.c.admissoes,
            INDICADORES_TABLE.c.desligamentos,
            INDICADORES_TABLE.c.colaboradores,
            INDICADORES_TABLE.c.horas_ausencia,
            INDICADORES_TABLE.c.horas_programadas,
        ).order_by(INDICADORES_TABLE.c.data, INDICADORES_TABLE.c.id)
        with database_engine().connect() as connection:
            df = pd.read_sql(query, connection)
    except (DatabaseStorageError, SQLAlchemyError, ValueError) as error:
        stop_with_storage_error("carregar", error)
        return pd.DataFrame(columns=COLUMNS)

    if df.empty:
        return pd.DataFrame(columns=COLUMNS)

    return normalize_dataframe(df)


def save_data(df: pd.DataFrame) -> None:
    try:
        ensure_database_ready()
        records = database_records(df)
        with database_engine().begin() as connection:
            connection.execute(delete(INDICADORES_TABLE))
            if records:
                connection.execute(insert(INDICADORES_TABLE), records)
    except (DatabaseStorageError, SQLAlchemyError, ValueError) as error:
        stop_with_storage_error("salvar", error)
        return


def format_percent(value: float) -> str:
    return f"{value:,.2f}%".replace(",", "X").replace(".", ",").replace("X", ".")


def format_number(value: float) -> str:
    if float(value).is_integer():
        return f"{int(value):,}".replace(",", ".")
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_percent_label(value: float) -> str:
    return "" if abs(float(value)) < 0.005 else format_percent(value)


def escape_pdf_text(value: object) -> str:
    text = str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def add_calculated_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()

    result = df.copy()
    result["turnover_%"] = result.apply(
        lambda row: (row["desligamentos"] / row["colaboradores"] * 100)
        if row["colaboradores"] > 0
        else 0,
        axis=1,
    )
    result["absenteismo_%"] = result.apply(
        lambda row: (row["horas_ausencia"] / row["horas_programadas"] * 100)
        if row["horas_programadas"] > 0
        else 0,
        axis=1,
    )
    return result


def overall_summary(df: pd.DataFrame) -> dict[str, float]:
    if df.empty:
        return {
            "desligamentos": 0,
            "admissoes": 0,
            "turnover": 0,
            "horas_ausencia": 0,
            "absenteismo": 0,
            "registros": 0,
        }

    sorted_df = df.sort_values("data")
    colaboradores = sorted_df["colaboradores"].mean()
    admissoes = sorted_df["admissoes"].sum()
    desligamentos = sorted_df["desligamentos"].sum()
    horas_ausencia = sorted_df["horas_ausencia"].sum()
    horas_programadas = sorted_df["horas_programadas"].sum()

    return {
        "desligamentos": desligamentos,
        "admissoes": admissoes,
        "turnover": (desligamentos / colaboradores * 100) if colaboradores > 0 else 0,
        "horas_ausencia": horas_ausencia,
        "absenteismo": (horas_ausencia / horas_programadas * 100) if horas_programadas > 0 else 0,
        "registros": len(sorted_df),
    }


def period_label(row: pd.Series, grouping: str) -> str:
    data = pd.Timestamp(row["data"])

    if grouping == "Semanal":
        iso = data.isocalendar()
        return f"S{int(iso.week):02d}/{str(int(iso.year))[-2:]}"

    if grouping == "Mensal":
        return data.strftime("%m/%Y")

    return data.strftime("%Y")


def period_sort(row: pd.Series, grouping: str) -> int:
    data = pd.Timestamp(row["data"])

    if grouping == "Semanal":
        iso = data.isocalendar()
        return int(iso.year) * 100 + int(iso.week)

    if grouping == "Mensal":
        return data.year * 100 + data.month

    return data.year


def grouped_data(df: pd.DataFrame, grouping: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(
            columns=[
                "periodo",
                "admissoes",
                "desligamentos",
                "colaboradores",
                "turnover_%",
                "horas_ausencia",
                "horas_programadas",
                "absenteismo_%",
            ]
        )

    work = df.sort_values("data").copy()
    work["periodo"] = work.apply(lambda row: period_label(row, grouping), axis=1)
    work["_sort"] = work.apply(lambda row: period_sort(row, grouping), axis=1)

    rows = []
    for (_, periodo), group in work.groupby(["_sort", "periodo"], sort=True):
        first = group.iloc[0]
        colaboradores = group["colaboradores"].mean()
        admissoes = group["admissoes"].sum()
        desligamentos = group["desligamentos"].sum()
        horas_ausencia = group["horas_ausencia"].sum()
        horas_programadas = group["horas_programadas"].sum()

        rows.append(
            {
                "_sort": first["_sort"],
                "periodo": periodo,
                "admissoes": admissoes,
                "desligamentos": desligamentos,
                "colaboradores": colaboradores,
                "turnover_%": (desligamentos / colaboradores * 100)
                if colaboradores > 0
                else 0,
                "horas_ausencia": horas_ausencia,
                "horas_programadas": horas_programadas,
                "absenteismo_%": (horas_ausencia / horas_programadas * 100)
                if horas_programadas > 0
                else 0,
            }
        )

    return pd.DataFrame(rows).sort_values("_sort").drop(columns="_sort").reset_index(drop=True)


def available_years(df: pd.DataFrame) -> list[int]:
    if df.empty:
        return [date.today().year]

    years = pd.to_datetime(df["data"], errors="coerce").dropna().dt.year
    if years.empty:
        return [date.today().year]

    return sorted(years.astype(int).unique().tolist())


def filter_by_year(df: pd.DataFrame, selected_year: int | None) -> pd.DataFrame:
    if df.empty or selected_year is None:
        return df

    result = df.copy()
    result["_ano"] = pd.to_datetime(result["data"], errors="coerce").dt.year
    return result[result["_ano"] == selected_year].drop(columns="_ano")


def complete_months(grouped: pd.DataFrame, selected_year: int | None) -> pd.DataFrame:
    if selected_year is None:
        return grouped

    columns = [
        "periodo",
        "admissoes",
        "desligamentos",
        "colaboradores",
        "turnover_%",
        "horas_ausencia",
        "horas_programadas",
        "absenteismo_%",
    ]
    base = pd.DataFrame(
        [
            {
                "periodo": f"{month:02d}/{selected_year}",
                "admissoes": 0.0,
                "desligamentos": 0.0,
                "colaboradores": 0.0,
                "turnover_%": 0.0,
                "horas_ausencia": 0.0,
                "horas_programadas": 0.0,
                "absenteismo_%": 0.0,
            }
            for month in range(1, 13)
        ],
        columns=columns,
    )

    if grouped.empty:
        return base

    completed = base.set_index("periodo")
    actual = grouped[columns].set_index("periodo")
    actual = actual.astype({column: "float64" for column in columns if column != "periodo"})
    completed.loc[actual.index, actual.columns] = actual
    return completed.reset_index()


def best_week_reference(df: pd.DataFrame) -> date:
    today = date.today()
    if df.empty:
        return today

    dates = pd.to_datetime(df["data"], errors="coerce").dropna().dt.date
    if dates.empty:
        return today

    current_start, current_end = week_range(today)
    if any(current_start <= item <= current_end for item in dates):
        return today

    return max(dates)


def week_range(reference: date) -> tuple[date, date]:
    start = reference - timedelta(days=reference.weekday())
    return start, start + timedelta(days=6)


def period_summary(df: pd.DataFrame) -> dict[str, float]:
    if df.empty:
        return {
            "admissoes": 0.0,
            "desligamentos": 0.0,
            "colaboradores": 0.0,
            "turnover": 0.0,
            "rotatividade_admissional": 0.0,
            "rotatividade_demissional": 0.0,
            "rotatividade": 0.0,
            "horas_ausencia": 0.0,
            "horas_programadas": 0.0,
            "absenteismo": 0.0,
        }

    colaboradores = df["colaboradores"].mean()
    admissoes = df["admissoes"].sum()
    desligamentos = df["desligamentos"].sum()
    horas_ausencia = df["horas_ausencia"].sum()
    horas_programadas = df["horas_programadas"].sum()
    rotatividade_admissional = (admissoes / colaboradores * 100) if colaboradores > 0 else 0.0
    rotatividade_demissional = (desligamentos / colaboradores * 100) if colaboradores > 0 else 0.0

    return {
        "admissoes": admissoes,
        "desligamentos": desligamentos,
        "colaboradores": colaboradores,
        "turnover": (desligamentos / colaboradores * 100) if colaboradores > 0 else 0.0,
        "rotatividade_admissional": rotatividade_admissional,
        "rotatividade_demissional": rotatividade_demissional,
        "rotatividade": rotatividade_admissional + rotatividade_demissional,
        "horas_ausencia": horas_ausencia,
        "horas_programadas": horas_programadas,
        "absenteismo": (horas_ausencia / horas_programadas * 100) if horas_programadas > 0 else 0.0,
    }


def weekly_data(df: pd.DataFrame, reference: date) -> tuple[pd.DataFrame, dict[str, float], date, date]:
    start, end = week_range(reference)
    columns = [
        "periodo",
        "admissoes",
        "desligamentos",
        "colaboradores",
        "turnover_%",
        "horas_ausencia",
        "horas_programadas",
        "absenteismo_%",
    ]
    week_days = pd.DataFrame(
        [
            {
                "data": start + timedelta(days=offset),
                "periodo": (start + timedelta(days=offset)).strftime("%a %d/%m"),
                "admissoes": 0.0,
                "desligamentos": 0.0,
                "colaboradores": 0.0,
                "turnover_%": 0.0,
                "horas_ausencia": 0.0,
                "horas_programadas": 0.0,
                "absenteismo_%": 0.0,
            }
            for offset in range(7)
        ]
    )
    day_names = {
        "Mon": "Seg",
        "Tue": "Ter",
        "Wed": "Qua",
        "Thu": "Qui",
        "Fri": "Sex",
        "Sat": "Sáb",
        "Sun": "Dom",
    }
    week_days["periodo"] = week_days["periodo"].replace(day_names, regex=True)

    if df.empty:
        return week_days[columns], period_summary(df), start, end

    work = df.copy()
    work["data"] = pd.to_datetime(work["data"], errors="coerce").dt.date
    work = work[(work["data"] >= start) & (work["data"] <= end)].dropna(subset=["data"])

    if work.empty:
        return week_days[columns], period_summary(work), start, end

    daily = (
        work.groupby("data", as_index=False)
        .agg(
            admissoes=("admissoes", "sum"),
            desligamentos=("desligamentos", "sum"),
            colaboradores=("colaboradores", "mean"),
            horas_ausencia=("horas_ausencia", "sum"),
            horas_programadas=("horas_programadas", "sum"),
        )
        .sort_values("data")
    )
    daily["turnover_%"] = daily.apply(
        lambda row: (row["desligamentos"] / row["colaboradores"] * 100)
        if row["colaboradores"] > 0
        else 0.0,
        axis=1,
    )
    daily["absenteismo_%"] = daily.apply(
        lambda row: (row["horas_ausencia"] / row["horas_programadas"] * 100)
        if row["horas_programadas"] > 0
        else 0.0,
        axis=1,
    )

    completed = week_days.set_index("data")
    actual = daily.set_index("data")
    update_columns = [column for column in columns if column != "periodo"]
    completed.loc[actual.index, update_columns] = actual[update_columns]

    return completed.reset_index()[columns], period_summary(work), start, end


def build_indicator_chart(df: pd.DataFrame, title: str = "Comparativo dos indicadores (%)"):
    if df.empty:
        return None

    max_value = max(df["turnover_%"].max(), df["absenteismo_%"].max(), 5)
    chart = go.Figure()
    chart.add_trace(
        go.Bar(
            name="Turnover",
            x=df["periodo"],
            y=df["turnover_%"],
            text=df["turnover_%"].map(format_percent_label),
            textposition="outside",
            marker_color="#103b78",
            width=0.28,
            hovertemplate="<b>%{x}</b><br>Turnover: %{y:.2f}%<extra></extra>",
        )
    )
    chart.add_trace(
        go.Bar(
            name="Absenteísmo",
            x=df["periodo"],
            y=df["absenteismo_%"],
            text=df["absenteismo_%"].map(format_percent_label),
            textposition="outside",
            marker_color="#11723c",
            width=0.28,
            hovertemplate="<b>%{x}</b><br>Absenteísmo: %{y:.2f}%<extra></extra>",
        )
    )

    chart.update_layout(
        title=title,
        barmode="group",
        bargap=0.62 if len(df) == 1 else 0.34,
        bargroupgap=0.18,
        height=430,
        margin=dict(l=20, r=20, t=56, b=30),
        xaxis_title=None,
        yaxis_title="Percentual",
        yaxis=dict(range=[0, max_value * 1.18], ticksuffix="%", gridcolor="#dfe6f0"),
        plot_bgcolor="white",
        paper_bgcolor="white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        font=dict(color="#172033"),
    )
    return chart


def build_movement_chart(df: pd.DataFrame, title: str = "Movimentação de colaboradores"):
    if df.empty:
        return None

    max_value = max(df["admissoes"].max(), df["desligamentos"].max(), df["colaboradores"].max(), 1)
    chart = go.Figure()
    chart.add_trace(
        go.Bar(
            name="Admissões",
            x=df["periodo"],
            y=df["admissoes"],
            text=df["admissoes"].map(format_number),
            textposition="outside",
            marker_color="#c91532",
            width=0.24,
            hovertemplate="<b>%{x}</b><br>Admissões: %{y}<extra></extra>",
        )
    )
    chart.add_trace(
        go.Bar(
            name="Desligamentos",
            x=df["periodo"],
            y=df["desligamentos"],
            text=df["desligamentos"].map(format_number),
            textposition="outside",
            marker_color="#103b78",
            width=0.24,
            hovertemplate="<b>%{x}</b><br>Desligamentos: %{y}<extra></extra>",
        )
    )
    chart.add_trace(
        go.Scatter(
            name="Colaboradores",
            x=df["periodo"],
            y=df["colaboradores"],
            mode="lines+markers+text",
            text=df["colaboradores"].map(format_number),
            textposition="top center",
            line=dict(color="#11723c", width=3),
            marker=dict(size=9),
            hovertemplate="<b>%{x}</b><br>Colaboradores: %{y}<extra></extra>",
        )
    )
    chart.update_layout(
        title=title,
        barmode="group",
        bargap=0.62 if len(df) == 1 else 0.34,
        bargroupgap=0.18,
        height=390,
        margin=dict(l=20, r=20, t=56, b=30),
        xaxis_title=None,
        yaxis_title="Quantidade",
        yaxis=dict(range=[0, max_value * 1.25], gridcolor="#dfe6f0"),
        plot_bgcolor="white",
        paper_bgcolor="white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        font=dict(color="#172033"),
    )
    return chart


def risk_level(value: float) -> tuple[str, str, str]:
    if value <= 3:
        return "Baixo", "#2c8f16", "Dentro de uma faixa confortável."
    if value <= 5:
        return "Médio", "#a87905", "Acompanhar para evitar tendência de alta."
    return "Alto", "#c91532", "Pede investigação do período."


def build_rotativity_pie(summary: dict[str, float]):
    admissoes = float(summary["admissoes"])
    desligamentos = float(summary["desligamentos"])
    rotatividade = float(summary["rotatividade"])
    has_movement = (admissoes + desligamentos) > 0

    if has_movement:
        labels = ["Admissões", "Desligamentos"]
        values = [admissoes, desligamentos]
        colors = ["#5fa2d9", "#e87b36"]
        hover = "<b>%{label}</b><br>%{value} registros<br>%{percent}<extra></extra>"
    else:
        labels = ["Sem movimentação"]
        values = [1]
        colors = ["#eef1f5"]
        hover = "<b>Sem movimentação no período</b><extra></extra>"

    chart = go.Figure(
        go.Pie(
            labels=labels,
            values=values,
            hole=0.72,
            sort=False,
            direction="clockwise",
            marker=dict(colors=colors, line=dict(color="#ffffff", width=2)),
            textinfo="none",
            showlegend=False,
            hovertemplate=hover,
        )
    )
    chart.add_annotation(
        text=f"<b>{format_percent(rotatividade)}</b><br><span style='font-size:13px'>Rotatividade</span>",
        x=0.5,
        y=0.5,
        showarrow=False,
        font=dict(color="#103b78", size=24),
    )
    chart.update_layout(
        height=255,
        margin=dict(l=4, r=4, t=4, b=4),
        paper_bgcolor="white",
        plot_bgcolor="white",
    )
    return chart


def build_indicator_line_chart(df: pd.DataFrame, title: str):
    if df.empty:
        return None

    max_value = max(float(df["turnover_%"].max()), float(df["absenteismo_%"].max()), 6.0)
    upper = max(max_value * 1.2, 6.0)
    chart = go.Figure()
    chart.add_shape(type="rect", xref="paper", yref="y", x0=0, x1=1, y0=0, y1=3, fillcolor="#eaf5e5", line_width=0, layer="below")
    chart.add_shape(type="rect", xref="paper", yref="y", x0=0, x1=1, y0=3, y1=5, fillcolor="#fff2d6", line_width=0, layer="below")
    chart.add_shape(type="rect", xref="paper", yref="y", x0=0, x1=1, y0=5, y1=upper, fillcolor="#ffe8e8", line_width=0, layer="below")
    chart.add_trace(
        go.Scatter(
            name="Turnover",
            x=df["periodo"],
            y=df["turnover_%"],
            mode="lines+markers+text",
            text=df["turnover_%"].map(format_percent_label),
            textposition="top center",
            line=dict(color="#c91532", width=3),
            marker=dict(size=8),
            hovertemplate="<b>%{x}</b><br>Turnover: %{y:.2f}%<extra></extra>",
        )
    )
    chart.add_trace(
        go.Scatter(
            name="Absenteísmo",
            x=df["periodo"],
            y=df["absenteismo_%"],
            mode="lines+markers+text",
            text=df["absenteismo_%"].map(format_percent_label),
            textposition="top center",
            textfont=dict(color="#0b6b3a"),
            line=dict(color="#0b6b3a", width=4),
            marker=dict(size=10, color="#0b6b3a", line=dict(color="#ffffff", width=2)),
            hovertemplate="<b>%{x}</b><br>Absenteísmo: %{y:.2f}%<extra></extra>",
        )
    )
    chart.update_layout(
        title=title,
        height=345,
        margin=dict(l=18, r=18, t=54, b=30),
        xaxis_title=None,
        yaxis_title="Percentual",
        yaxis=dict(range=[0, upper], ticksuffix="%", gridcolor="#d7dee9"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        plot_bgcolor="white",
        paper_bgcolor="white",
        font=dict(color="#172033"),
    )
    return chart


def build_rotativity_chart(df: pd.DataFrame, title: str):
    if df.empty:
        return None

    chart = make_subplots(specs=[[{"secondary_y": True}]])
    chart.add_trace(
        go.Bar(
            name="Admissões",
            x=df["periodo"],
            y=df["admissoes"],
            marker_color="#5fa2d9",
            text=df["admissoes"].map(format_number),
            textposition="outside",
            hovertemplate="<b>%{x}</b><br>Admissões: %{y}<extra></extra>",
        ),
        secondary_y=False,
    )
    chart.add_trace(
        go.Bar(
            name="Desligamentos",
            x=df["periodo"],
            y=df["desligamentos"],
            marker_color="#e87b36",
            text=df["desligamentos"].map(format_number),
            textposition="outside",
            hovertemplate="<b>%{x}</b><br>Desligamentos: %{y}<extra></extra>",
        ),
        secondary_y=False,
    )
    chart.add_trace(
        go.Scatter(
            name="Turnover",
            x=df["periodo"],
            y=df["turnover_%"],
            mode="lines+markers+text",
            text=df["turnover_%"].map(format_percent_label),
            textposition="top center",
            line=dict(color="#9b7a08", width=3),
            marker=dict(size=8),
            hovertemplate="<b>%{x}</b><br>Turnover: %{y:.2f}%<extra></extra>",
        ),
        secondary_y=True,
    )
    max_count = max(df["admissoes"].max(), df["desligamentos"].max(), 1)
    max_rate = max(df["turnover_%"].max(), 5)
    chart.update_layout(
        title=title,
        barmode="group",
        height=360,
        margin=dict(l=18, r=18, t=54, b=30),
        xaxis_title=None,
        plot_bgcolor="white",
        paper_bgcolor="white",
        legend=dict(orientation="h", yanchor="bottom", y=-0.22, xanchor="center", x=0.5),
        font=dict(color="#172033"),
    )
    chart.update_yaxes(title_text="Quantidade", range=[0, max_count * 1.35], gridcolor="#d7dee9", secondary_y=False)
    chart.update_yaxes(title_text="Turnover", range=[0, max_rate * 1.35], ticksuffix="%", secondary_y=True)
    return chart


def render_risk_legend(value: float) -> None:
    label, color, message = risk_level(value)
    st.markdown(
        f"""
        <div class="mini-table">
            <div class="mini-row"><span>Baixo risco</span><strong>até 3%</strong></div>
            <div class="mini-row"><span>Médio risco</span><strong>3% a 5%</strong></div>
            <div class="mini-row"><span>Alto risco</span><strong>acima de 5%</strong></div>
            <div class="mini-row"><span>Leitura atual</span><strong style="color:{color}">{label}</strong></div>
        </div>
        <div class="status-pill" style="margin-top:0.75rem;border-color:{color};color:{color};">{message}</div>
        """,
        unsafe_allow_html=True,
    )


def render_risk_scale(value: float) -> None:
    label, color, message = risk_level(value)
    scale_max = max(8.0, value * 1.15)
    low_width = min(3 / scale_max * 100, 100)
    medium_width = min(2 / scale_max * 100, max(100 - low_width, 0))
    high_width = max(100 - low_width - medium_width, 0)
    marker_left = min(value / scale_max * 100, 100)

    st.markdown(
        f"""
        <div class="risk-panel">
            <h3>Risco de turnover</h3>
            <p>Turnover atual: <strong style="color:{color};">{format_percent(value)}</strong> - {label} risco. {message}</p>
            <div class="risk-scale">
                <div class="risk-segment risk-low" style="width:{low_width:.2f}%"></div>
                <div class="risk-segment risk-medium" style="width:{medium_width:.2f}%"></div>
                <div class="risk-segment risk-high" style="width:{high_width:.2f}%"></div>
                <div class="risk-marker" style="left:calc({marker_left:.2f}% - 1.5px)"></div>
            </div>
            <div class="risk-labels">
                <span>Baixo até 3%</span>
                <span>Médio 3% a 5%</span>
                <span>Alto acima de 5%</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_rotativity_legend(summary: dict[str, float]) -> None:
    st.markdown(
        f"""
        <div class="mini-table">
            <div class="mini-row"><span>Admissões</span><strong>{format_number(summary["admissoes"])}</strong></div>
            <div class="mini-row"><span>Desligamentos</span><strong>{format_number(summary["desligamentos"])}</strong></div>
            <div class="mini-row"><span>Rotatividade admissional</span><strong>{format_percent(summary["rotatividade_admissional"])}</strong></div>
            <div class="mini-row"><span>Rotatividade demissional</span><strong>{format_percent(summary["rotatividade_demissional"])}</strong></div>
            <div class="mini-row"><span>Rotatividade</span><strong>{format_percent(summary["rotatividade"])}</strong></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def active_periods(grouped: pd.DataFrame) -> pd.DataFrame:
    if grouped.empty:
        return grouped

    activity_columns = [
        "admissoes",
        "desligamentos",
        "colaboradores",
        "horas_ausencia",
        "horas_programadas",
    ]
    available = [column for column in activity_columns if column in grouped.columns]
    if not available:
        return grouped

    activity = grouped[available].fillna(0).abs().sum(axis=1)
    return grouped[activity > 0].copy()


def period_peak(grouped: pd.DataFrame, column: str) -> tuple[str, float]:
    active = active_periods(grouped)
    if active.empty or column not in active:
        return "Sem dados", 0.0

    row = active.loc[active[column].idxmax()]
    return str(row["periodo"]), float(row[column])


def turnover_trend(grouped: pd.DataFrame) -> tuple[str, str, str]:
    active = active_periods(grouped)
    if len(active) < 2:
        return "Sem comparação", "Cadastre mais períodos para enxergar tendência.", "gold"

    previous = float(active.iloc[-2]["turnover_%"])
    current = float(active.iloc[-1]["turnover_%"])
    diff = current - previous

    if abs(diff) < 0.01:
        return "Estável", f"Último período manteve {format_percent(current)}.", "green"

    if diff > 0:
        return "Subindo", f"Aumentou {format_percent(abs(diff))} em relação ao período anterior.", "red"

    return "Caindo", f"Reduziu {format_percent(abs(diff))} em relação ao período anterior.", "green"


def recommendation_text(summary: dict[str, float], grouped: pd.DataFrame) -> tuple[str, str, str]:
    risk, _, _ = risk_level(summary["turnover"])
    trend, _, _ = turnover_trend(grouped)
    saldo = summary["admissoes"] - summary["desligamentos"]

    if risk == "Alto":
        return (
            "Prioridade: investigar desligamentos",
            "O turnover está em alto risco. Revise os desligamentos do período, motivo de saída e áreas com maior concentração.",
            "red",
        )

    if summary["absenteismo"] > 3:
        return (
            "Atenção ao absenteísmo",
            "As ausências estão acima de uma faixa confortável. Verifique atestados, faltas recorrentes e setores com maior impacto.",
            "gold",
        )

    if trend == "Subindo":
        return (
            "Acompanhar tendência",
            "O turnover subiu no período mais recente. Ainda pode ser pontual, mas vale acompanhar antes que vire padrão.",
            "gold",
        )

    if saldo < 0:
        return (
            "Saldo de colaboradores negativo",
            "Há mais desligamentos do que admissões no período. Avalie se o quadro atual atende a operação.",
            "gold",
        )

    return (
        "Leitura geral saudável",
        "Os indicadores principais estão controlados. Continue alimentando os dados para acompanhar tendência e sazonalidade.",
        "green",
    )


def render_diagnostics(summary: dict[str, float], grouped: pd.DataFrame) -> None:
    risk, risk_color, risk_message = risk_level(summary["turnover"])
    worst_turnover_period, worst_turnover = period_peak(grouped, "turnover_%")
    worst_absence_period, worst_absence = period_peak(grouped, "absenteismo_%")
    trend, trend_text, trend_tone = turnover_trend(grouped)
    saldo = summary["admissoes"] - summary["desligamentos"]
    saldo_tone = "green" if saldo >= 0 else "red"
    rec_title, rec_text, rec_tone = recommendation_text(summary, grouped)
    border_color = {"red": "#c91532", "gold": "#a87905", "green": "#11723c"}.get(rec_tone, "#103b78")

    st.markdown("### Diagnóstico automático")
    st.markdown(
        f"""
        <div class="diagnostic-grid">
            <div class="diagnostic-card" style="border-top:4px solid {risk_color};">
                <div class="label">Risco atual</div>
                <div class="value" style="color:{risk_color};">{risk}</div>
                <p class="text">{risk_message}</p>
            </div>
            <div class="diagnostic-card red">
                <div class="label">Maior turnover</div>
                <div class="value">{format_percent(worst_turnover)}</div>
                <p class="text">Período: {worst_turnover_period}</p>
            </div>
            <div class="diagnostic-card gold">
                <div class="label">Maior absenteísmo</div>
                <div class="value">{format_percent(worst_absence)}</div>
                <p class="text">Período: {worst_absence_period}</p>
            </div>
            <div class="diagnostic-card {trend_tone}">
                <div class="label">Tendência</div>
                <div class="value">{trend}</div>
                <p class="text">{trend_text}</p>
            </div>
            <div class="diagnostic-card {saldo_tone}">
                <div class="label">Saldo do quadro</div>
                <div class="value">{format_number(saldo)}</div>
                <p class="text">Admissões menos desligamentos no período.</p>
            </div>
            <div class="diagnostic-card">
                <div class="label">Horas ausentes</div>
                <div class="value">{format_number(summary["horas_ausencia"])}</div>
                <p class="text">Total de horas de ausência registradas.</p>
            </div>
            <div class="diagnostic-card">
                <div class="label">Horas programadas</div>
                <div class="value">{format_number(summary["horas_programadas"])}</div>
                <p class="text">Base usada no cálculo do absenteísmo.</p>
            </div>
            <div class="diagnostic-card green">
                <div class="label">Colaboradores</div>
                <div class="value">{format_number(summary["colaboradores"])}</div>
                <p class="text">Média do total informado no período.</p>
            </div>
        </div>
        <div class="recommendation-box" style="border-left-color:{border_color};">
            <strong>{rec_title}</strong>
            <span>{rec_text}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_executive_blocks(summary: dict[str, float], df: pd.DataFrame) -> None:
    colaboradores_inicio = 0.0
    colaboradores_final = 0.0
    variacao = 0.0

    if not df.empty:
        ordered = df.sort_values("data")
        colaboradores_inicio = float(ordered.iloc[0]["colaboradores"])
        colaboradores_final = float(ordered.iloc[-1]["colaboradores"])
        variacao = colaboradores_final - colaboradores_inicio

    rot_admissional = summary["rotatividade_admissional"]
    rot_demissional = summary["rotatividade_demissional"]
    rot_total = summary["rotatividade"]

    st.markdown(
        f"""
        <div class="exec-grid">
            <div class="exec-card">
                <h3>Taxa de rotatividade</h3>
                <div class="exec-line tone-yellow"><div class="exec-label">Turnover</div><div class="exec-value">{format_percent(summary["turnover"])}</div></div>
                <div class="exec-line tone-green"><div class="exec-label">Rotatividade admissional</div><div class="exec-value">{format_percent(rot_admissional)}</div></div>
                <div class="exec-line tone-red"><div class="exec-label">Rotatividade demissional</div><div class="exec-value">{format_percent(rot_demissional)}</div></div>
                <div class="exec-line tone-yellow"><div class="exec-label">Rotatividade</div><div class="exec-value">{format_percent(rot_total)}</div></div>
            </div>
            <div class="exec-card">
                <h3>Colaboradores</h3>
                <div class="exec-line tone-blue"><div class="exec-label">Início do período</div><div class="exec-value">{format_number(colaboradores_inicio)}</div></div>
                <div class="exec-line tone-blue"><div class="exec-label">Final do período</div><div class="exec-value">{format_number(colaboradores_final)}</div></div>
                <div class="exec-line tone-yellow"><div class="exec-label">Variação</div><div class="exec-value">{format_number(variacao)}</div></div>
            </div>
            <div class="exec-card">
                <h3>Movimentação</h3>
                <div class="exec-line tone-green"><div class="exec-label">Contratações</div><div class="exec-value">{format_number(summary["admissoes"])}</div></div>
                <div class="exec-line tone-red"><div class="exec-label">Desligamentos</div><div class="exec-value">{format_number(summary["desligamentos"])}</div></div>
                <div class="exec-line tone-yellow"><div class="exec-label">Rotatividade total</div><div class="exec-value">{format_percent(rot_total)}</div></div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def format_display_table(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    result = df.copy()
    rename_map = {
        "periodo": "Período",
        "admissoes": "Admissões",
        "desligamentos": "Desligamentos",
        "colaboradores": "Colaboradores",
        "turnover_%": "Turnover",
        "horas_ausencia": "Horas de ausência",
        "horas_programadas": "Horas programadas",
        "absenteismo_%": "Absenteísmo",
    }

    for column in ["turnover_%", "absenteismo_%"]:
        if column in result:
            result[column] = result[column].map(format_percent)

    for column in ["admissoes", "desligamentos", "colaboradores", "horas_ausencia", "horas_programadas"]:
        if column in result:
            result[column] = result[column].map(format_number)

    return result.rename(columns=rename_map)


def format_history_table(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    result = add_calculated_columns(df).sort_values("data", ascending=False).copy()
    result["data"] = pd.to_datetime(result["data"]).dt.strftime("%d/%m/%Y")
    rename_map = {
        "data": "Data",
        "admissoes": "Admissões",
        "desligamentos": "Desligamentos",
        "colaboradores": "Colaboradores",
        "horas_ausencia": "Horas de ausência",
        "horas_programadas": "Horas programadas",
        "turnover_%": "Turnover",
        "absenteismo_%": "Absenteísmo",
    }

    for column in ["turnover_%", "absenteismo_%"]:
        if column in result:
            result[column] = result[column].map(format_percent)

    for column in ["admissoes", "desligamentos", "colaboradores", "horas_ausencia", "horas_programadas"]:
        if column in result:
            result[column] = result[column].map(format_number)

    return result.rename(columns=rename_map)


def pdf_table(
    data: list[list[object]],
    col_widths: list[float] | None = None,
    emphasis_cols: list[int] | None = None,
    cell_text_colors: dict[tuple[int, int], str] | None = None,
) -> Table:
    emphasis_cols = emphasis_cols or []
    cell_text_colors = cell_text_colors or {}
    header_style = ParagraphStyle(
        name="PdfTableHeader",
        fontName="Helvetica-Bold",
        fontSize=8.4,
        leading=10.5,
        textColor=colors.white,
    )
    body_style = ParagraphStyle(
        name="PdfTableBody",
        fontName="Helvetica",
        fontSize=8.2,
        leading=10.5,
        textColor=colors.HexColor("#172033"),
    )
    emphasis_style = ParagraphStyle(
        name="PdfTableEmphasis",
        parent=body_style,
        fontName="Helvetica-Bold",
        textColor=colors.HexColor("#103b78"),
    )
    custom_styles: dict[tuple[str, str], ParagraphStyle] = {}

    def style_for_cell(row_index: int, col_index: int) -> ParagraphStyle:
        base_style = header_style if row_index == 0 else emphasis_style if col_index in emphasis_cols else body_style
        color_value = cell_text_colors.get((row_index, col_index))
        if not color_value or row_index == 0:
            return base_style

        style_key = (base_style.name, color_value)
        if style_key not in custom_styles:
            custom_styles[style_key] = ParagraphStyle(
                name=f"{base_style.name}_{len(custom_styles)}",
                parent=base_style,
                textColor=colors.HexColor(color_value),
            )
        return custom_styles[style_key]

    table_data = [
        [
            Paragraph(
                escape_pdf_text(cell),
                style_for_cell(row_index, col_index),
            )
            for col_index, cell in enumerate(row)
        ]
        for row_index, row in enumerate(data)
    ]
    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#103b78")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.2),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#fbfcfe"), colors.HexColor("#f4f7fb")]),
                ("BOX", (0, 0), (-1, -1), 0.45, colors.HexColor("#d7e2ef")),
                ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.HexColor("#d7e2ef")),
                ("LINEBELOW", (0, 1), (-1, -1), 0.35, colors.HexColor("#dfe7f1")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, 0), 7),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 7),
                ("TOPPADDING", (0, 1), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 6),
            ]
        )
    )
    return table


def pdf_chart_image(figure: go.Figure | None, width_cm: float = 18.0) -> PdfImage | None:
    if figure is None:
        return None

    try:
        image_bytes = pio.to_image(figure, format="png", scale=2)
    except Exception:
        return None

    buffer = BytesIO(image_bytes)
    width_points = width_cm * cm
    if figure.layout.width and figure.layout.height:
        height_points = width_points * (figure.layout.height / figure.layout.width)
    else:
        height_points = width_points * 0.6
    return PdfImage(buffer, width=width_points, height=height_points)


def prepare_pdf_figure(figure: go.Figure | None, chart_type: str) -> go.Figure | None:
    if figure is None:
        return None

    prepared = go.Figure(figure)

    if chart_type == "indicator":
        prepared.update_layout(
            width=1150,
            height=620,
            margin=dict(l=88, r=40, t=78, b=92),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            font=dict(size=20, color="#172033"),
        )
        prepared.update_xaxes(tickangle=0, automargin=True, tickfont=dict(size=18))
        prepared.update_yaxes(
            automargin=True,
            title_standoff=16,
            tickfont=dict(size=16),
            title_font=dict(size=18),
        )
    elif chart_type == "rotativity":
        prepared.update_layout(
            width=1150,
            height=620,
            margin=dict(l=88, r=72, t=78, b=110),
            legend=dict(orientation="h", yanchor="top", y=-0.14, xanchor="center", x=0.5),
            font=dict(size=20, color="#172033"),
        )
        prepared.update_xaxes(tickangle=-28, automargin=True, tickfont=dict(size=18))
        prepared.update_yaxes(
            automargin=True,
            title_standoff=16,
            tickfont=dict(size=16),
            title_font=dict(size=18),
            secondary_y=False,
        )
        prepared.update_yaxes(
            automargin=True,
            title_standoff=16,
            tickfont=dict(size=16),
            title_font=dict(size=18),
            secondary_y=True,
        )

    return prepared


def build_pdf_report(
    summary: dict[str, float],
    grouped: pd.DataFrame,
    selected_df: pd.DataFrame,
    analysis_type: str,
    period_text: str,
) -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=1.2 * cm,
        leftMargin=1.2 * cm,
        topMargin=1.0 * cm,
        bottomMargin=1.0 * cm,
        title="Relatório de Indicadores de RH",
    )
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="ReportTitle",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=18,
            leading=22,
            textColor=colors.HexColor("#103b78"),
            spaceAfter=4,
        )
    )
    styles.add(
        ParagraphStyle(
            name="ReportSubtitle",
            parent=styles["Normal"],
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#667085"),
        )
    )
    styles.add(
        ParagraphStyle(
            name="SectionTitle",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=14,
            textColor=colors.HexColor("#172033"),
            spaceBefore=12,
            spaceAfter=6,
        )
    )
    styles.add(
        ParagraphStyle(
            name="BodySmall",
            parent=styles["Normal"],
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#172033"),
        )
    )

    elements = []
    header_text = Paragraph(
        "<b>Indicadores de RH</b><br/>JR Ferragens &amp; Madeiras<br/>"
        f"<font color='#667085'>Relatório gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}</font>",
        styles["ReportSubtitle"],
    )
    if LOGO_PATH.exists():
        logo = PdfImage(str(LOGO_PATH), width=1.35 * cm, height=1.35 * cm)
        header = Table([[logo, header_text]], colWidths=[1.7 * cm, 15.2 * cm])
    else:
        header = Table([[header_text]], colWidths=[16.9 * cm])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    elements.append(header)
    elements.append(Spacer(1, 0.28 * cm))
    elements.append(Paragraph("Relatório de Indicadores de RH", styles["ReportTitle"]))
    elements.append(Paragraph(f"{escape_pdf_text(analysis_type)} - {escape_pdf_text(period_text)}", styles["ReportSubtitle"]))

    risk, _, risk_message = risk_level(summary["turnover"])
    risk_color = {"Baixo": "#2c8f16", "Médio": "#a87905", "Alto": "#c91532"}.get(risk, "#103b78")
    trend, trend_text, trend_color = turnover_trend(grouped)
    worst_turnover_period, worst_turnover = period_peak(grouped, "turnover_%")
    worst_absence_period, worst_absence = period_peak(grouped, "absenteismo_%")
    rec_title, rec_text, rec_color = recommendation_text(summary, grouped)
    saldo = summary["admissoes"] - summary["desligamentos"]
    positive_color = "#2c8f16"
    negative_color = "#c91532"
    neutral_color = "#103b78"

    def signed_color(value: float) -> str:
        if value > 0:
            return positive_color
        if value < 0:
            return negative_color
        return neutral_color

    elements.append(Paragraph("Resumo executivo", styles["SectionTitle"]))
    elements.append(
        pdf_table(
            [
                ["Indicador", "Resultado", "Leitura"],
                ["Turnover", format_percent(summary["turnover"]), f"{risk} risco"],
                ["Absenteísmo", format_percent(summary["absenteismo"]), "Horas ausentes / horas programadas"],
                ["Rotatividade", format_percent(summary["rotatividade"]), "Admissões + desligamentos sobre colaboradores"],
                ["Admissões", format_number(summary["admissoes"]), "Entradas no período"],
                ["Desligamentos", format_number(summary["desligamentos"]), "Saídas no período"],
                ["Saldo do quadro", format_number(saldo), "Admissões menos desligamentos"],
                ["Colaboradores", format_number(summary["colaboradores"]), "Média do total informado"],
            ],
            col_widths=[5.2 * cm, 4.0 * cm, 7.2 * cm],
            emphasis_cols=[1],
            cell_text_colors={
                (1, 1): risk_color,
                (6, 1): signed_color(saldo),
            },
        )
    )

    elements.append(Paragraph("Diagnóstico automático", styles["SectionTitle"]))
    elements.append(
        pdf_table(
            [
                ["Item", "Resultado", "Observação"],
                ["Risco atual", risk, risk_message],
                ["Maior turnover", format_percent(worst_turnover), f"Período: {worst_turnover_period}"],
                ["Maior absenteísmo", format_percent(worst_absence), f"Período: {worst_absence_period}"],
                ["Tendência", trend, trend_text],
                ["Recomendação", rec_title, rec_text],
            ],
            col_widths=[4.0 * cm, 4.2 * cm, 8.2 * cm],
            emphasis_cols=[1],
            cell_text_colors={
                (1, 1): risk_color,
                (4, 1): {"green": positive_color, "red": negative_color, "gold": "#a87905"}.get(trend_color, neutral_color),
                (5, 1): {"green": positive_color, "red": negative_color, "gold": "#a87905"}.get(rec_color, neutral_color),
            },
        )
    )

    colaboradores_inicio = 0.0
    colaboradores_final = 0.0
    if not selected_df.empty:
        ordered = selected_df.sort_values("data")
        colaboradores_inicio = float(ordered.iloc[0]["colaboradores"])
        colaboradores_final = float(ordered.iloc[-1]["colaboradores"])
    variacao = colaboradores_final - colaboradores_inicio

    elements.append(Paragraph("Taxas e movimentação", styles["SectionTitle"]))
    elements.append(
        pdf_table(
            [
                ["Grupo", "Indicador", "Valor"],
                ["Taxa de rotatividade", "Turnover", format_percent(summary["turnover"])],
                ["Taxa de rotatividade", "Rotatividade admissional", format_percent(summary["rotatividade_admissional"])],
                ["Taxa de rotatividade", "Rotatividade demissional", format_percent(summary["rotatividade_demissional"])],
                ["Taxa de rotatividade", "Rotatividade", format_percent(summary["rotatividade"])],
                ["Colaboradores", "Início do período", format_number(colaboradores_inicio)],
                ["Colaboradores", "Final do período", format_number(colaboradores_final)],
                ["Colaboradores", "Variação", format_number(variacao)],
                ["Movimentação", "Contratações", format_number(summary["admissoes"])],
                ["Movimentação", "Desligamentos", format_number(summary["desligamentos"])],
            ],
            col_widths=[5.0 * cm, 7.4 * cm, 4.0 * cm],
            emphasis_cols=[2],
            cell_text_colors={
                (1, 2): risk_color,
                (7, 2): signed_color(variacao),
            },
        )
    )

    line_title = (
        "Indicadores por dia da semana"
        if analysis_type == "Semanal"
        else f"Indicadores mes a mes - {period_text.split()[-1]}"
        if analysis_type == "Mensal"
        else "Indicadores ano a ano"
    )
    rotativity_title = (
        "Admissoes, desligamentos e turnover da semana"
        if analysis_type == "Semanal"
        else f"Taxa de rotatividade - {period_text.split()[-1]}"
        if analysis_type == "Mensal"
        else "Taxa de rotatividade anual"
    )
    chart_images = [
        pdf_chart_image(prepare_pdf_figure(build_indicator_line_chart(grouped, line_title), "indicator")),
        pdf_chart_image(prepare_pdf_figure(build_rotativity_chart(grouped, rotativity_title), "rotativity")),
    ]
    chart_images = [chart for chart in chart_images if chart is not None]

    elements.append(PageBreak())
    elements.append(Paragraph("Graficos do periodo", styles["SectionTitle"]))
    if chart_images:
        for chart_image in chart_images:
            elements.append(chart_image)
            elements.append(Spacer(1, 0.18 * cm))
    else:
        elements.append(
            Paragraph(
                "Nao foi possivel incorporar os graficos ao PDF neste ambiente.",
                styles["BodySmall"],
            )
        )

    display = format_display_table(grouped)
    if not display.empty:
        elements.append(Paragraph("Tabela usada nos gráficos", styles["SectionTitle"]))
        table_rows = [display.columns.tolist()]
        table_rows.extend(display.head(18).values.tolist())
        elements.append(
            pdf_table(
                table_rows,
                col_widths=[2.2 * cm, 2.0 * cm, 2.3 * cm, 2.4 * cm, 2.0 * cm, 2.5 * cm, 2.5 * cm, 2.2 * cm],
            )
        )
        if len(display) > 18:
            elements.append(Spacer(1, 0.15 * cm))
            elements.append(Paragraph(f"Observação: tabela limitada aos 18 primeiros períodos de {len(display)}.", styles["BodySmall"]))

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


def render_header() -> None:
    left, right = st.columns([0.82, 0.18])

    with left:
        st.markdown(
            """
            <div class="title-block">
                <p>JR Ferragens & Madeiras</p>
                <h1>Indicadores de RH</h1>
                <p class="subtitle">Acompanhe turnover, absenteísmo e movimentação de colaboradores em uma visão simples.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with right:
        if LOGO_PATH.exists():
            st.image(str(LOGO_PATH), width=92)


def render_metric_card(label: str, value: str, caption: str, tone: str = "blue") -> None:
    st.markdown(
        f"""
        <div class="metric-card {tone}">
            <div class="label">{label}</div>
            <div class="value">{value}</div>
            <div class="caption">{caption}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_insight(summary: dict[str, float]) -> None:
    turnover = summary["turnover"]
    absenteismo = summary["absenteismo"]

    if turnover == 0 and absenteismo == 0:
        title = "Sem sinal de alerta nos dados cadastrados"
        text = "Os indicadores aparecem zerados porque ainda não existem saídas ou ausências no período analisado."
    elif turnover > 5 or absenteismo > 3:
        title = "Ponto de atenção"
        text = "Há indicador acima de um nível comum de acompanhamento. Confira os lançamentos e veja se existe alguma causa concentrada no período."
    else:
        title = "Leitura geral estável"
        text = "Os indicadores estão em uma faixa de acompanhamento. Continue alimentando os dados para enxergar tendência."

    st.markdown(
        f"""
        <div class="insight-box">
            <h3>{title}</h3>
            <p>{text}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def inject_styles() -> None:
    st.markdown(
        """
        <style>
            :root {
                --jr-red: #c91532;
                --jr-blue: #103b78;
                --jr-green: #11723c;
                --ink: #172033;
                --muted: #667085;
                --line: #d9e2ee;
                --page: #ffffff;
                --card: #ffffff;
            }

            .stApp {
                background: var(--page);
            }

            .main .block-container {
                padding-top: 1.2rem;
                padding-bottom: 3rem;
                max-width: 1500px;
            }

            [data-testid="stHeader"] {
                background: rgba(255, 255, 255, 0.92);
            }

            .title-block p {
                color: var(--jr-red);
                font-size: 0.78rem;
                font-weight: 800;
                letter-spacing: 0;
                margin: 0 0 0.2rem;
                text-transform: uppercase;
            }

            .title-block h1 {
                color: var(--jr-blue);
                font-size: clamp(2rem, 5vw, 3.4rem);
                line-height: 1;
                margin: 0 0 0.35rem;
            }

            .title-block .subtitle {
                color: var(--muted);
                font-size: 1rem;
                max-width: 780px;
                margin: 0;
            }

            .hero-card {
                background: linear-gradient(135deg, #ffffff 0%, #eef5ff 58%, #fff1f4 100%);
                border: 1px solid var(--line);
                border-radius: 12px;
                padding: 1.3rem 1.4rem;
                box-shadow: 0 18px 45px rgba(18, 35, 61, 0.08);
                margin-bottom: 1rem;
            }

            .dashboard-shell {
                background: #ffffff;
                border: 1px solid var(--line);
                border-radius: 14px;
                padding: 1.15rem 1.25rem;
                box-shadow: 0 18px 45px rgba(18, 35, 61, 0.08);
                margin-bottom: 1rem;
            }

            .panel-title {
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 1rem;
                margin-bottom: 0.7rem;
            }

            .panel-title h2 {
                color: var(--ink);
                font-size: 1.45rem;
                margin: 0;
            }

            .panel-title span {
                color: var(--muted);
                font-size: 0.9rem;
            }

            .mini-table {
                margin-top: 0.55rem;
                border-top: 1px solid #edf1f6;
            }

            .mini-row {
                display: flex;
                justify-content: space-between;
                border-bottom: 1px solid #edf1f6;
                padding: 0.54rem 0;
                color: var(--ink);
                font-size: 0.88rem;
            }

            .mini-row strong {
                color: var(--jr-blue);
            }

            .status-pill {
                display: inline-flex;
                align-items: center;
                justify-content: center;
                min-height: 34px;
                padding: 0 0.8rem;
                border-radius: 999px;
                border: 1px solid var(--line);
                color: var(--ink);
                background: #ffffff;
                font-weight: 800;
                font-size: 0.86rem;
            }

            .risk-panel {
                background: #ffffff;
                border: 1px solid var(--line);
                border-radius: 10px;
                padding: 1rem;
                box-shadow: 0 10px 26px rgba(18, 35, 61, 0.05);
                margin: 0.85rem 0 1rem;
            }

            .risk-panel h3 {
                color: var(--ink);
                font-size: 1rem;
                margin: 0 0 0.25rem;
            }

            .risk-panel p {
                color: var(--muted);
                font-size: 0.9rem;
                margin: 0 0 0.85rem;
            }

            .risk-scale {
                position: relative;
                display: flex;
                height: 18px;
                overflow: visible;
                border-radius: 999px;
                background: #edf1f6;
                box-shadow: inset 0 0 0 1px rgba(23, 32, 51, 0.08);
            }

            .risk-segment {
                height: 18px;
            }

            .risk-low {
                background: #2c8f16;
                border-radius: 999px 0 0 999px;
            }

            .risk-medium {
                background: #a87905;
            }

            .risk-high {
                background: #c91532;
                border-radius: 0 999px 999px 0;
            }

            .risk-marker {
                position: absolute;
                top: -7px;
                width: 3px;
                height: 32px;
                border-radius: 999px;
                background: #172033;
                box-shadow: 0 0 0 3px #ffffff, 0 6px 12px rgba(18, 35, 61, 0.2);
            }

            .risk-labels {
                display: grid;
                grid-template-columns: repeat(3, 1fr);
                gap: 0.5rem;
                color: var(--muted);
                font-size: 0.78rem;
                font-weight: 800;
                margin-top: 0.65rem;
            }

            .risk-labels span:nth-child(2) {
                text-align: center;
            }

            .risk-labels span:last-child {
                text-align: right;
            }

            .diagnostic-grid {
                display: grid;
                grid-template-columns: repeat(4, minmax(0, 1fr));
                gap: 1rem;
                margin: 0.9rem 0 1.1rem;
            }

            .diagnostic-card {
                background: #ffffff;
                border: 1px solid var(--line);
                border-radius: 10px;
                padding: 1rem;
                box-shadow: 0 10px 26px rgba(18, 35, 61, 0.05);
                min-height: 132px;
            }

            .diagnostic-card .label {
                color: var(--muted);
                font-size: 0.76rem;
                font-weight: 900;
                text-transform: uppercase;
                margin-bottom: 0.45rem;
            }

            .diagnostic-card .value {
                color: var(--jr-blue);
                font-size: 1.55rem;
                font-weight: 900;
                line-height: 1;
                margin-bottom: 0.45rem;
            }

            .diagnostic-card .text {
                color: var(--muted);
                font-size: 0.86rem;
                line-height: 1.35;
                margin: 0;
            }

            .diagnostic-card.red .value {
                color: var(--jr-red);
            }

            .diagnostic-card.green .value {
                color: var(--jr-green);
            }

            .diagnostic-card.gold .value {
                color: #a87905;
            }

            .recommendation-box {
                background: #ffffff;
                border: 1px solid var(--line);
                border-left: 6px solid var(--jr-red);
                border-radius: 10px;
                color: var(--ink);
                padding: 0.95rem 1.1rem;
                margin: 0 0 1.1rem;
                box-shadow: 0 10px 26px rgba(18, 35, 61, 0.05);
            }

            .recommendation-box strong {
                display: block;
                margin-bottom: 0.25rem;
            }

            .recommendation-box span {
                color: var(--muted);
                line-height: 1.4;
            }

            .exec-grid {
                display: grid;
                grid-template-columns: repeat(3, minmax(0, 1fr));
                gap: 1rem;
                margin: 0.85rem 0 1.1rem;
            }

            .exec-card {
                background: #ffffff;
                border: 1px solid var(--line);
                border-radius: 10px;
                overflow: hidden;
                box-shadow: 0 10px 26px rgba(18, 35, 61, 0.05);
            }

            .exec-card h3 {
                background: #f3f5f8;
                border-bottom: 1px solid var(--line);
                color: #3a4354;
                font-size: 0.92rem;
                letter-spacing: 0;
                margin: 0;
                padding: 0.85rem 1rem;
                text-align: center;
                text-transform: uppercase;
            }

            .exec-line {
                display: grid;
                grid-template-columns: 1fr 110px;
                min-height: 54px;
                border-bottom: 1px solid var(--line);
            }

            .exec-line:last-child {
                border-bottom: 0;
            }

            .exec-label {
                align-items: center;
                background: #f8fafc;
                color: #3a4354;
                display: flex;
                font-size: 0.84rem;
                font-weight: 800;
                justify-content: center;
                padding: 0.65rem;
                text-align: center;
                text-transform: uppercase;
            }

            .exec-value {
                align-items: center;
                color: var(--ink);
                display: flex;
                font-size: 1rem;
                font-weight: 900;
                justify-content: center;
                padding: 0.65rem;
            }

            .tone-blue .exec-label { background: #eef5ff; }
            .tone-green .exec-label { background: #edf8ef; }
            .tone-yellow .exec-label { background: #fff5d6; }
            .tone-red .exec-label { background: #ffe8df; }

            .metric-card {
                background: var(--card);
                border: 1px solid var(--line);
                border-radius: 10px;
                padding: 1rem 1.05rem;
                min-height: 138px;
                box-shadow: 0 12px 30px rgba(18, 35, 61, 0.06);
            }

            .metric-card .label {
                color: var(--muted);
                font-size: 0.78rem;
                font-weight: 800;
                margin-bottom: 0.45rem;
                text-transform: uppercase;
            }

            .metric-card .value {
                color: var(--jr-blue);
                font-size: clamp(1.7rem, 3vw, 2.55rem);
                font-weight: 900;
                line-height: 1;
                margin-bottom: 0.55rem;
            }

            .metric-card .caption {
                color: var(--muted);
                font-size: 0.86rem;
                line-height: 1.25;
            }

            .metric-card.green .value {
                color: var(--jr-green);
            }

            .metric-card.red .value {
                color: var(--jr-red);
            }

            .insight-box {
                background: #ffffff;
                border: 1px solid var(--line);
                border-left: 6px solid var(--jr-blue);
                border-radius: 10px;
                padding: 1rem 1.15rem;
                margin: 1rem 0 1.15rem;
                box-shadow: 0 10px 26px rgba(18, 35, 61, 0.05);
            }

            .insight-box h3 {
                color: var(--ink);
                font-size: 1.05rem;
                margin: 0 0 0.35rem;
            }

            .insight-box p {
                color: var(--muted);
                margin: 0;
                line-height: 1.45;
            }

            .formula-grid {
                display: grid;
                grid-template-columns: repeat(2, minmax(0, 1fr));
                gap: 1rem;
                margin: 1rem 0 0.35rem;
            }

            .formula-card {
                background: #ffffff;
                border: 1px solid var(--line);
                border-radius: 10px;
                padding: 1rem;
            }

            .formula-card strong {
                color: var(--jr-blue);
                display: block;
                margin-bottom: 0.35rem;
            }

            .formula-card span {
                color: var(--muted);
            }

            .section-note {
                color: var(--muted);
                margin-top: -0.35rem;
                margin-bottom: 1rem;
            }

            div[data-testid="stTabs"] button {
                font-weight: 800;
            }

            div[data-testid="stRadio"] label {
                font-weight: 700;
            }

            div[data-testid="stMetric"] {
                background: #ffffff;
                border: 1px solid #dce3ee;
                border-radius: 8px;
                padding: 1rem;
                box-shadow: 0 14px 34px rgba(18, 35, 61, 0.08);
            }

            div[data-testid="stMetricValue"] {
                color: var(--jr-blue);
            }

            .stButton > button,
            .stDownloadButton > button,
            div[data-testid="stFormSubmitButton"] button {
                border-radius: 7px;
                font-weight: 800;
            }

            div[data-testid="stForm"] {
                border: 1px solid #dce3ee;
                border-radius: 8px;
                padding: 1.1rem;
                background: #ffffff;
            }

            @media (max-width: 800px) {
                .formula-grid {
                    grid-template-columns: 1fr;
                }

                .exec-grid {
                    grid-template-columns: 1fr;
                }

                .diagnostic-grid {
                    grid-template-columns: 1fr;
                }
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_form(df: pd.DataFrame) -> pd.DataFrame:
    default_colaboradores = 0
    if not df.empty:
        default_colaboradores = int(df.sort_values("data").iloc[-1]["colaboradores"])

    st.markdown("### Novo lançamento")
    st.markdown(
        '<p class="section-note">Cadastre uma linha por dia, semana ou mês. O painel agrupa automaticamente conforme a visão escolhida.</p>',
        unsafe_allow_html=True,
    )
    render_persistence_notice()

    with st.form("novo_registro", clear_on_submit=True):
        col1, col2, col3 = st.columns(3)
        col4, col5, col6 = st.columns(3)

        with col1:
            data = st.date_input("Data do lançamento", value=date.today(), format="DD/MM/YYYY")
        with col2:
            admissoes = st.number_input("Admissões", min_value=0, step=1, help="Quantas pessoas entraram no período.")
        with col3:
            desligamentos = st.number_input("Desligamentos", min_value=0, step=1, help="Quantas pessoas saíram no período.")
        with col4:
            colaboradores = st.number_input(
                "Total de colaboradores",
                min_value=0,
                value=default_colaboradores,
                step=1,
                help="Informe o total atual de colaboradores usado como base do cálculo.",
            )
        with col5:
            horas_ausencia = st.number_input(
                "Horas de ausência",
                min_value=0.0,
                step=0.5,
                help="Faltas, atrasos, atestados e saídas antecipadas em horas.",
            )
        with col6:
            horas_programadas = st.number_input(
                "Horas programadas",
                min_value=0.0,
                step=0.5,
                help="Total de horas que deveriam ser trabalhadas no período.",
            )

        submitted = st.form_submit_button("Salvar lançamento", type="primary", width="stretch")

    if submitted:
        if colaboradores <= 0:
            st.error("Informe o total de colaboradores para calcular o turnover.")
            return df

        if horas_programadas <= 0:
            st.error("Informe horas programadas para calcular o absenteísmo.")
            return df

        new_row = pd.DataFrame(
            [
                {
                    "data": data,
                    "admissoes": admissoes,
                    "desligamentos": desligamentos,
                    "colaboradores": colaboradores,
                    "horas_ausencia": horas_ausencia,
                    "horas_programadas": horas_programadas,
                }
            ]
        )
        updated = pd.concat([df, new_row], ignore_index=True)
        save_data(updated)
        st.success(f"Registro salvo {persistence_label()}. Gráficos atualizados.")
        st.rerun()

    return df


def render_fill_guide() -> None:
    st.markdown(
        """
        <div class="formula-grid">
            <div class="formula-card">
                <strong>Turnover</strong>
                <span>Desligamentos / Total de colaboradores x 100</span>
            </div>
            <div class="formula-card">
                <strong>Absenteísmo</strong>
                <span>Horas de ausência / Horas programadas x 100</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.info(
        "Para acompanhar por mês, lance uma data dentro daquele mês. "
        "Para acompanhar por semana, lance os dados nos dias reais da semana."
    )


def render_summary(df: pd.DataFrame) -> None:
    summary = overall_summary(df)
    st.markdown("### Visão geral")
    st.markdown('<p class="section-note">Resumo calculado com todos os lançamentos cadastrados.</p>', unsafe_allow_html=True)
    metric1, metric2, metric3, metric4, metric5 = st.columns(5)

    with metric1:
        render_metric_card(
            "Turnover geral",
            format_percent(summary["turnover"]),
            "Saídas em relação ao total médio de colaboradores.",
            "blue",
        )
    with metric2:
        render_metric_card(
            "Absenteísmo geral",
            format_percent(summary["absenteismo"]),
            "Horas ausentes em relação às horas programadas.",
            "green",
        )
    with metric3:
        render_metric_card("Admissões", format_number(summary["admissoes"]), "Entradas registradas.", "red")
    with metric4:
        render_metric_card("Desligamentos", format_number(summary["desligamentos"]), "Saídas registradas.", "blue")
    with metric5:
        render_metric_card("Registros", format_number(summary["registros"]), "Linhas salvas no histórico.", "green")

    render_insight(summary)


def render_week_status(summary: dict[str, float], start: date, end: date) -> None:
    st.markdown(f"**Semana: {start.strftime('%d/%m/%Y')} a {end.strftime('%d/%m/%Y')}**")
    metric1, metric2, metric3, metric4, metric5 = st.columns(5)
    with metric1:
        render_metric_card("Turnover", format_percent(summary["turnover"]), "Resultado da semana.", "blue")
    with metric2:
        render_metric_card("Absenteísmo", format_percent(summary["absenteismo"]), "Resultado da semana.", "green")
    with metric3:
        render_metric_card("Admissões", format_number(summary["admissoes"]), "Entradas na semana.", "red")
    with metric4:
        render_metric_card("Desligamentos", format_number(summary["desligamentos"]), "Saídas na semana.", "blue")
    with metric5:
        render_metric_card("Colaboradores", format_number(summary["colaboradores"]), "Média informada.", "green")

    if summary["horas_programadas"] == 0 and summary["admissoes"] == 0 and summary["desligamentos"] == 0:
        st.info("Ainda não há lançamentos para essa semana.")


def render_charts(df: pd.DataFrame) -> None:
    st.markdown("### Análise dos gráficos")
    st.markdown(
        '<p class="section-note">Escolha a visão que você quer acompanhar. A semanal mostra a semana selecionada; a mensal mostra o ano inteiro.</p>',
        unsafe_allow_html=True,
    )
    controls1, controls2 = st.columns([0.72, 0.28])
    with controls1:
        grouping = st.radio(
            "Agrupamento",
            options=["Semanal", "Mensal", "Anual"],
            index=1,
            horizontal=True,
        )
    with controls2:
        selected_year = None
        week_reference = None
        if grouping == "Semanal":
            week_reference = st.date_input(
                "Semana de referência",
                value=best_week_reference(df),
                format="DD/MM/YYYY",
            )
        elif grouping == "Mensal":
            years = available_years(df)
            selected_year = st.selectbox(
                "Ano do gráfico",
                options=years,
                index=len(years) - 1,
            )

    if df.empty:
        st.info("Cadastre dados para gerar os gráficos.")
        return

    if grouping == "Semanal":
        grouped, week_summary, week_start, week_end = weekly_data(df, week_reference or date.today())
        render_week_status(week_summary, week_start, week_end)
        indicator_title = "Indicadores por dia da semana"
        movement_title = "Movimentação da semana"
    else:
        chart_df = filter_by_year(df, selected_year) if grouping == "Mensal" else df
        grouped = grouped_data(chart_df, grouping or "Mensal")
        indicator_title = "Indicadores mês a mês" if grouping == "Mensal" else "Indicadores ano a ano"
        movement_title = "Movimentação mês a mês" if grouping == "Mensal" else "Movimentação ano a ano"

    if grouping == "Mensal":
        grouped = complete_months(grouped, selected_year)

    if grouped.empty:
        st.info("Cadastre dados para gerar os gráficos.")
        return

    if grouped["turnover_%"].max() > 100:
        st.warning(
            "O turnover passou de 100%. Confira se o campo 'Total de colaboradores' "
            "está com a base total de funcionários do período, e não apenas com as admissões."
        )

    indicator_chart = build_indicator_chart(grouped, indicator_title)
    if indicator_chart is not None:
        st.plotly_chart(indicator_chart, width="stretch", config={"displayModeBar": False})

    movement_chart = build_movement_chart(grouped, movement_title)
    if movement_chart is not None:
        st.plotly_chart(movement_chart, width="stretch", config={"displayModeBar": False})

    with st.expander("Ver tabela usada nos gráficos", expanded=False):
        st.dataframe(format_display_table(grouped), width="stretch", hide_index=True)


def render_dashboard(df: pd.DataFrame) -> None:
    st.markdown("### Rotatividade")
    st.markdown(
        '<p class="section-note">Painel executivo inspirado em relatório gerencial: indicador principal, evolução e movimentação.</p>',
        unsafe_allow_html=True,
    )

    controls1, controls2 = st.columns([0.72, 0.28])
    with controls1:
        grouping = st.radio(
            "Tipo de análise",
            options=["Semanal", "Mensal", "Anual"],
            index=1,
            horizontal=True,
        )

    with controls2:
        selected_year = None
        week_reference = None
        if grouping == "Semanal":
            week_reference = st.date_input(
                "Semana de referência",
                value=best_week_reference(df),
                format="DD/MM/YYYY",
            )
        elif grouping == "Mensal":
            years = available_years(df)
            selected_year = st.selectbox("Ano de análise", options=years, index=len(years) - 1)

    if df.empty:
        st.info("Cadastre dados para visualizar o painel.")
        return

    if grouping == "Semanal":
        reference = week_reference or date.today()
        grouped, summary, start, end = weekly_data(df, reference)
        selected_df = df.copy()
        selected_df["data"] = pd.to_datetime(selected_df["data"], errors="coerce").dt.date
        selected_df = selected_df[(selected_df["data"] >= start) & (selected_df["data"] <= end)]
        period_text = f"Dados referentes de {start.strftime('%d/%m/%Y')} a {end.strftime('%d/%m/%Y')}"
        line_title = "Indicadores por dia da semana"
        rotativity_title = "Admissões, desligamentos e turnover da semana"
    elif grouping == "Mensal":
        selected_df = filter_by_year(df, selected_year)
        grouped = complete_months(grouped_data(selected_df, "Mensal"), selected_year)
        summary = period_summary(selected_df)
        period_text = f"Dados referentes ao ano de {selected_year}"
        line_title = f"Indicadores mês a mês - {selected_year}"
        rotativity_title = f"Taxa de rotatividade - {selected_year}"
    else:
        selected_df = df
        grouped = grouped_data(selected_df, "Anual")
        summary = period_summary(selected_df)
        period_text = "Dados agrupados por ano"
        line_title = "Indicadores ano a ano"
        rotativity_title = "Taxa de rotatividade anual"

    st.markdown(
        f"""
        <div class="panel-title">
            <div>
                <h2>Resumo executivo</h2>
                <span>{period_text}</span>
            </div>
            <div class="status-pill">Rotatividade {format_percent(summary["rotatividade"])}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    pdf_col1, pdf_col2 = st.columns([0.78, 0.22])
    with pdf_col2:
        st.download_button(
            "Baixar PDF",
            data=build_pdf_report(summary, grouped, selected_df, grouping, period_text),
            file_name=f"relatorio_indicadores_rh_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
            mime="application/pdf",
            width="stretch",
        )

    left, right = st.columns([0.24, 0.76])
    with left:
        st.markdown("**Rotatividade do período**")
        st.plotly_chart(build_rotativity_pie(summary), width="stretch", config={"displayModeBar": False})
        render_rotativity_legend(summary)

    with right:
        line_chart = build_indicator_line_chart(grouped, line_title)
        if line_chart is not None:
            st.plotly_chart(line_chart, width="stretch", config={"displayModeBar": False})
        render_risk_scale(summary["turnover"])

    render_diagnostics(summary, grouped)
    render_executive_blocks(summary, selected_df)

    rotativity_chart = build_rotativity_chart(grouped, rotativity_title)
    if rotativity_chart is not None:
        st.plotly_chart(rotativity_chart, width="stretch", config={"displayModeBar": False})

    with st.expander("Ver tabela usada nos gráficos", expanded=False):
        st.dataframe(format_display_table(grouped), width="stretch", hide_index=True)


def render_history(df: pd.DataFrame) -> None:
    st.markdown("### Dados cadastrados")
    st.markdown(
        '<p class="section-note">Consulte, baixe ou edite os lançamentos que alimentam o painel.</p>',
        unsafe_allow_html=True,
    )
    render_persistence_notice()

    if df.empty:
        st.info("Nenhum lançamento cadastrado.")
        return

    st.dataframe(format_history_table(df), width="stretch", hide_index=True)

    with st.expander("Editar lançamentos", expanded=False):
        st.caption("Altere os valores na tabela abaixo e clique em salvar.")
        edited = st.data_editor(
            df.sort_values("data", ascending=False).reset_index(drop=True),
            width="stretch",
            hide_index=True,
            num_rows="dynamic",
            column_config={
                "data": st.column_config.DateColumn("Data", format="DD/MM/YYYY", required=True),
                "admissoes": st.column_config.NumberColumn("Admissões", min_value=0, step=1),
                "desligamentos": st.column_config.NumberColumn("Desligamentos", min_value=0, step=1),
                "colaboradores": st.column_config.NumberColumn("Colaboradores", min_value=0, step=1),
                "horas_ausencia": st.column_config.NumberColumn("Horas de ausência", min_value=0.0, step=0.5),
                "horas_programadas": st.column_config.NumberColumn("Horas programadas", min_value=0.0, step=0.5),
            },
            key="editor_lancamentos",
        )

        if st.button("Salvar alterações", type="primary", width="stretch"):
            edited = edited[COLUMNS].copy()
            edited["data"] = pd.to_datetime(edited["data"], errors="coerce").dt.date
            numeric_columns = [column for column in COLUMNS if column != "data"]
            edited[numeric_columns] = edited[numeric_columns].apply(pd.to_numeric, errors="coerce").fillna(0)
            edited = edited.dropna(subset=["data"])
            save_data(edited)
            st.success(f"Alterações salvas {persistence_label()}.")
            st.rerun()

    csv = format_history_table(df).to_csv(index=False, sep=";").encode("utf-8-sig")
    action1, action2 = st.columns([0.22, 0.78])
    with action1:
        st.download_button(
            "Baixar CSV",
            data=csv,
            file_name="indicadores_rh_jr.csv",
            mime="text/csv",
            width="stretch",
        )
    with action2:
        if st.button("Apagar todos os registros", type="secondary"):
            save_data(pd.DataFrame(columns=COLUMNS))
            st.rerun()


inject_styles()
render_header()

dataframe = load_data()

tab_dashboard, tab_entry, tab_data = st.tabs(["Painel", "Lançamento", "Dados"])

with tab_dashboard:
    render_dashboard(dataframe)

with tab_entry:
    dataframe = render_form(dataframe)
    render_fill_guide()

with tab_data:
    render_history(dataframe)
