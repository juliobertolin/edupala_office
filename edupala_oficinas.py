"""
Edupala — Formulário de Proposta de Oficina
===========================================

App em Streamlit que recebe propostas de oficinas, grava num banco de dados,
envia e-mail de confirmação ao proponente e oferece um painel de coordenação
com exportação para CSV e Excel.

Como rodar localmente:
    pip install -r requirements.txt
    streamlit run edupala_oficinas.py

Onde os dados são gravados:
    - Se existir `banco_url` nos secrets  -> usa esse banco (ex.: Postgres na nuvem).
    - Se não existir                      -> usa o arquivo SQLite local (só para testes).

Configuração em .streamlit/secrets.toml:

    banco_url   = "postgresql://usuario:senha@host/base?sslmode=require"
    admin_senha = "troque-esta-senha"

    [email]
    remetente = "oficinas@edupala.org.br"
    senha     = "senha-de-app-do-provedor"
    servidor  = "smtp.gmail.com"
    porta     = 587
    copia_coordenacao = "coordenacao@edupala.org.br"

Sem a seção [email] o app funciona normalmente: apenas não envia
o e-mail de confirmação.
"""

from __future__ import annotations

import io
import json
import smtplib
import ssl
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    Table,
    Text,
    create_engine,
    func,
    select,
)

# ===========================================================================
# ⬇⬇⬇  AJUSTE AQUI — DADOS DA SUA EDIÇÃO  ⬇⬇⬇
# ===========================================================================

EVENTO = "Edupala"                       # nome do evento
EDICAO = "2026"                          # ano/edição
PRAZO = date(2026, 9, 18)                # ano, mês, dia — último dia de envio
DIVULGACAO = "21 de setembro de 2026"     # quando sai o resultado
CONTATO = "congressoedupala@gmail.br"      # e-mail de dúvidas
PREFIXO_PROTOCOLO = "EDU"                # protocolos ficam EDU2026-OF001, EDU2026-OF002...

# Arquivo usado SOMENTE quando não há banco na nuvem configurado (testes locais)
BANCO_LOCAL = Path("propostas_oficinas.db")

EIXOS = [
    "Ensino de Ciências",
    "Tecnologias Digitais na Educação",
    "Formação de Professores",
    "Educação Inclusiva",
    "Divulgação Científica",
    "Metodologias Ativas",
    "Outro",
]

TURNOS = [
    "Segunda — manhã",
    "Segunda — tarde",
    "Terça — manhã",
    "Terça — tarde",
    "Quarta — manhã",
    "Quarta — tarde",
]

RECURSOS = [
    "Projetor",
    "Caixa de som",
    "Internet (Wi-Fi)",
    "Tomadas para notebooks",
    "Quadro branco",
    "Impressão de material",
]

TITULACOES = ["Graduando", "Graduado", "Especialista", "Mestre", "Doutor", "Outro"]

CARGAS_HORARIAS = ["2h", "3h", "4h"]

LIMITES = {"resumo": 1500, "minicurriculo": 500, "objetivos": 1000, "metodologia": 2000}

# ===========================================================================
# ⬆⬆⬆  FIM DA ÁREA DE AJUSTE — daqui para baixo não precisa mexer  ⬆⬆⬆
# ===========================================================================


CAMPOS_TEXTO = [
    "protocolo", "enviado_em", "nome", "email", "telefone", "instituicao",
    "titulacao", "minicurriculo", "lattes", "coministrantes", "titulo", "eixo",
    "resumo", "objetivos", "metodologia", "publico_alvo", "nivel",
    "prerequisitos", "carga_horaria", "referencias", "modalidade", "espaco",
    "recursos", "softwares", "materiais", "disponibilidade",
]


# ---------------------------------------------------------------------------
# Acesso seguro aos secrets
# ---------------------------------------------------------------------------


def segredo(chave: str, padrao: Any = None) -> Any:
    """Lê st.secrets sem quebrar quando o arquivo de secrets não existe."""
    try:
        return st.secrets.get(chave, padrao)
    except Exception:  # noqa: BLE001
        return padrao


# ---------------------------------------------------------------------------
# Banco de dados (Postgres na nuvem ou SQLite local)
# ---------------------------------------------------------------------------

metadata = MetaData()

propostas = Table(
    "propostas",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    *[Column(nome, Text) for nome in CAMPOS_TEXTO],
    Column("min_participantes", Integer),
    Column("max_participantes", Integer),
    Column("situacao", Text, server_default="Em análise"),
)


def url_banco() -> str:
    url = segredo("banco_url")
    if url:
        # SQLAlchemy não aceita o prefixo antigo "postgres://"
        return str(url).replace("postgres://", "postgresql://", 1)
    return f"sqlite:///{BANCO_LOCAL}"


@st.cache_resource
def motor():
    """Cria a conexão uma única vez e reaproveita entre execuções."""
    return create_engine(url_banco(), pool_pre_ping=True)


def usando_banco_na_nuvem() -> bool:
    return not url_banco().startswith("sqlite")


def criar_tabela() -> None:
    metadata.create_all(motor())


def salvar(dados: dict[str, Any]) -> str:
    """Grava a proposta e devolve o protocolo gerado a partir do id do banco."""
    registro = dict(dados)
    registro["enviado_em"] = datetime.now().strftime("%d/%m/%Y %H:%M")
    with motor().begin() as con:
        resultado = con.execute(propostas.insert().values(**registro))
        novo_id = resultado.inserted_primary_key[0]
        protocolo = f"{PREFIXO_PROTOCOLO}{EDICAO}-OF{novo_id:03d}"
        con.execute(
            propostas.update()
            .where(propostas.c.id == novo_id)
            .values(protocolo=protocolo)
        )
    dados["enviado_em"] = registro["enviado_em"]
    return protocolo


def carregar_propostas() -> pd.DataFrame:
    consulta = select(propostas).order_by(propostas.c.id.desc())
    with motor().connect() as con:
        return pd.read_sql(consulta, con)


def email_ja_enviou(email: str) -> bool:
    consulta = select(propostas.c.id).where(
        func.lower(propostas.c.email) == email.strip().lower()
    )
    with motor().connect() as con:
        return con.execute(consulta).first() is not None


# ---------------------------------------------------------------------------
# E-mail de confirmação
# ---------------------------------------------------------------------------


def enviar_confirmacao(dados: dict[str, Any], protocolo: str) -> tuple[bool, str]:
    conf = segredo("email")
    if not conf:
        return False, "Envio de e-mail não configurado."

    corpo = f"""Olá, {dados['nome']}.

Recebemos sua proposta de oficina para o {EVENTO} {EDICAO}.

Protocolo: {protocolo}
Título: {dados['titulo']}
Eixo: {dados['eixo']}
Carga horária: {dados['carga_horaria']}
Recebido em: {dados.get('enviado_em', '')}

O resultado da avaliação será divulgado até {DIVULGACAO}.
Guarde o número do protocolo para consultas.

Dúvidas: {CONTATO}

Coordenação do {EVENTO}
"""

    msg = EmailMessage()
    msg["Subject"] = f"[{EVENTO} {EDICAO}] Proposta recebida — {protocolo}"
    msg["From"] = conf["remetente"]
    msg["To"] = dados["email"]
    if conf.get("copia_coordenacao"):
        msg["Bcc"] = conf["copia_coordenacao"]
    msg.set_content(corpo)

    try:
        contexto = ssl.create_default_context()
        with smtplib.SMTP(conf["servidor"], int(conf["porta"]), timeout=20) as smtp:
            smtp.starttls(context=contexto)
            smtp.login(conf["remetente"], conf["senha"])
            smtp.send_message(msg)
        return True, "E-mail de confirmação enviado."
    except Exception as erro:  # noqa: BLE001
        return False, f"Não foi possível enviar o e-mail: {erro}"


# ---------------------------------------------------------------------------
# Validação
# ---------------------------------------------------------------------------


def validar(d: dict[str, Any], aceites: list[bool]) -> list[str]:
    erros: list[str] = []

    obrigatorios = {
        "nome": "Nome completo",
        "email": "E-mail",
        "telefone": "Telefone",
        "instituicao": "Instituição",
        "minicurriculo": "Minicurrículo",
        "titulo": "Título da oficina",
        "resumo": "Resumo",
        "objetivos": "Objetivos de aprendizagem",
        "metodologia": "Metodologia e roteiro",
        "publico_alvo": "Público-alvo",
        "prerequisitos": "Pré-requisitos",
        "materiais": "Materiais de consumo",
    }
    for campo, rotulo in obrigatorios.items():
        if not str(d.get(campo, "")).strip():
            erros.append(f"Preencha o campo **{rotulo}**.")

    email = d.get("email", "")
    if email and ("@" not in email or "." not in email.split("@")[-1]):
        erros.append("O e-mail informado não parece válido.")

    for campo, limite in LIMITES.items():
        if len(str(d.get(campo, ""))) > limite:
            erros.append(f"O campo **{campo}** excede {limite} caracteres.")

    if d["min_participantes"] > d["max_participantes"]:
        erros.append("O número mínimo de participantes não pode superar o máximo.")

    if not d.get("disponibilidade"):
        erros.append("Marque ao menos um turno de disponibilidade.")

    if not all(aceites):
        erros.append("É preciso marcar as três declarações finais.")

    return erros


# ---------------------------------------------------------------------------
# Interface — formulário
# ---------------------------------------------------------------------------


def tela_formulario() -> None:
    st.title(f"Proposta de oficina — {EVENTO} {EDICAO}")

    if date.today() > PRAZO:
        st.error(
            f"O prazo de submissão encerrou em {PRAZO.strftime('%d/%m/%Y')}. "
            f"Dúvidas: {CONTATO}"
        )
        return

    st.caption(
        f"Envios até {PRAZO.strftime('%d/%m/%Y')} · resultado até {DIVULGACAO} · "
        f"contato: {CONTATO}"
    )
    st.info(
        "As oficinas do Edupala são atividades práticas e participativas. "
        "As propostas são avaliadas quanto à pertinência temática, clareza "
        "metodológica e viabilidade de infraestrutura."
    )

    with st.form("proposta", clear_on_submit=False):
        st.subheader("1. Identificação do proponente")
        c1, c2 = st.columns(2)
        nome = c1.text_input("Nome completo *")
        email = c2.text_input("E-mail *")
        telefone = c1.text_input("Telefone / WhatsApp *")
        instituicao = c2.text_input("Instituição ou vínculo profissional *")
        titulacao = c1.selectbox("Maior titulação *", TITULACOES)
        lattes = c2.text_input("Link do Lattes ou ORCID")
        minicurriculo = st.text_area(
            "Minicurrículo *",
            max_chars=LIMITES["minicurriculo"],
            height=110,
            help="Até 500 caracteres. Será usado na divulgação, caso aprovada.",
        )
        coministrantes = st.text_area(
            "Coministrantes",
            height=80,
            help="Um por linha: nome, e-mail e instituição. Deixe em branco se não houver.",
        )

        st.subheader("2. Dados da oficina")
        titulo = st.text_input("Título da oficina *")
        c3, c4 = st.columns(2)
        eixo = c3.selectbox("Eixo temático *", EIXOS)
        nivel = c4.selectbox("Nível *", ["Introdutório", "Intermediário", "Avançado"])
        resumo = st.text_area("Resumo *", max_chars=LIMITES["resumo"], height=160)
        objetivos = st.text_area(
            "Objetivos de aprendizagem *",
            max_chars=LIMITES["objetivos"],
            height=120,
            help="O que o participante será capaz de fazer ao final da oficina.",
        )
        metodologia = st.text_area(
            "Metodologia e roteiro das atividades *",
            max_chars=LIMITES["metodologia"],
            height=180,
            help="Descreva a sequência das atividades e a divisão do tempo.",
        )
        publico_alvo = st.text_input(
            "Público-alvo *",
            placeholder="Ex.: professores da educação básica, licenciandos",
        )
        prerequisitos = st.text_input(
            "Pré-requisitos dos participantes *",
            placeholder="Escreva 'Nenhum' se não houver",
        )
        c5, c6, c7 = st.columns(3)
        carga_horaria = c5.selectbox("Carga horária *", CARGAS_HORARIAS)
        min_part = c6.number_input("Mínimo de participantes *", 1, 200, 10)
        max_part = c7.number_input("Máximo de participantes *", 1, 200, 30)
        referencias = st.text_area("Referências bibliográficas", height=80)

        st.subheader("3. Infraestrutura e logística")
        c8, c9 = st.columns(2)
        modalidade = c8.selectbox("Modalidade *", ["Presencial", "Online", "Híbrida"])
        espaco = c9.selectbox(
            "Tipo de espaço *",
            [
                "Sala comum",
                "Sala com mesas móveis",
                "Laboratório de informática",
                "Espaço aberto",
                "Outro",
            ],
        )
        recursos = st.multiselect("Recursos necessários", RECURSOS)
        softwares = st.text_input("Softwares que precisam estar instalados nas máquinas")
        materiais = st.text_area(
            "Materiais de consumo *",
            height=90,
            help="Especifique quantidades e quem fornece (proponente ou organização). "
            "Escreva 'Nenhum' se não houver.",
        )
        disponibilidade = st.multiselect(
            "Disponibilidade de data e turno *",
            TURNOS,
            help="Marque todas as opções possíveis para facilitar a montagem da grade.",
        )

        st.subheader("4. Declarações finais")
        a1 = st.checkbox(
            "Declaro que as informações são verdadeiras e me comprometo a "
            "ministrar a oficina caso aprovada."
        )
        a2 = st.checkbox(
            "Autorizo o uso de imagem e a divulgação do material nos canais do evento."
        )
        a3 = st.checkbox(
            "Estou ciente de que a aprovação depende da disponibilidade de infraestrutura."
        )

        enviar = st.form_submit_button("Enviar proposta", type="primary")

    if not enviar:
        return

    dados = {
        "nome": nome.strip(),
        "email": email.strip(),
        "telefone": telefone.strip(),
        "instituicao": instituicao.strip(),
        "titulacao": titulacao,
        "minicurriculo": minicurriculo.strip(),
        "lattes": lattes.strip(),
        "coministrantes": coministrantes.strip(),
        "titulo": titulo.strip(),
        "eixo": eixo,
        "resumo": resumo.strip(),
        "objetivos": objetivos.strip(),
        "metodologia": metodologia.strip(),
        "publico_alvo": publico_alvo.strip(),
        "nivel": nivel,
        "prerequisitos": prerequisitos.strip(),
        "carga_horaria": carga_horaria,
        "min_participantes": int(min_part),
        "max_participantes": int(max_part),
        "referencias": referencias.strip(),
        "modalidade": modalidade,
        "espaco": espaco,
        "recursos": json.dumps(recursos, ensure_ascii=False),
        "softwares": softwares.strip(),
        "materiais": materiais.strip(),
        "disponibilidade": json.dumps(disponibilidade, ensure_ascii=False),
        "situacao": "Em análise",
    }

    erros = validar({**dados, "disponibilidade": disponibilidade}, [a1, a2, a3])

    if erros:
        st.error("Corrija os pontos abaixo antes de enviar:")
        for e in erros:
            st.markdown(f"- {e}")
        return

    try:
        if email_ja_enviou(dados["email"]):
            st.warning(
                "Já existe uma proposta registrada com este e-mail. "
                f"Se precisar substituí-la, escreva para {CONTATO}."
            )
            return
        protocolo = salvar(dados)
    except Exception as erro:  # noqa: BLE001
        st.error(
            "Não foi possível gravar a proposta no banco de dados. "
            f"Copie esta mensagem e envie para {CONTATO}:\n\n`{erro}`"
        )
        return

    st.success(f"Proposta registrada. Protocolo: **{protocolo}**")
    st.balloons()

    ok, mensagem = enviar_confirmacao(dados, protocolo)
    st.caption(mensagem if ok else f"Aviso: {mensagem} Anote seu protocolo.")


# ---------------------------------------------------------------------------
# Interface — painel da coordenação
# ---------------------------------------------------------------------------


def para_excel(df: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Propostas")
    return buffer.getvalue()


def tela_coordenacao() -> None:
    st.title("Painel da coordenação")

    senha_real = segredo("admin_senha")
    if not senha_real:
        st.warning(
            "Defina `admin_senha` em .streamlit/secrets.toml para proteger o painel."
        )
        senha_real = "edupala"

    if st.session_state.get("autenticado") is not True:
        senha = st.text_input("Senha", type="password")
        if st.button("Entrar"):
            if senha == senha_real:
                st.session_state["autenticado"] = True
                st.rerun()
            else:
                st.error("Senha incorreta.")
        return

    if usando_banco_na_nuvem():
        st.caption("Armazenamento: banco de dados externo (dados persistentes).")
    else:
        st.warning(
            f"Armazenamento: arquivo local `{BANCO_LOCAL}`. Adequado apenas para testes — "
            "num servidor em nuvem esses dados podem ser apagados a cada reinício."
        )

    df = carregar_propostas()
    if df.empty:
        st.info("Nenhuma proposta recebida até o momento.")
        return

    c1, c2, c3 = st.columns(3)
    c1.metric("Propostas", len(df))
    c2.metric("Eixos distintos", df["eixo"].nunique())
    c3.metric("Vagas máximas somadas", int(df["max_participantes"].sum()))

    st.bar_chart(df["eixo"].value_counts())

    filtro = st.multiselect("Filtrar por eixo", sorted(df["eixo"].dropna().unique()))
    visao = df[df["eixo"].isin(filtro)] if filtro else df

    st.dataframe(
        visao[
            ["protocolo", "titulo", "nome", "eixo", "carga_horaria", "modalidade", "situacao"]
        ],
        use_container_width=True,
        hide_index=True,
    )

    escolha = st.selectbox("Ver proposta completa", visao["protocolo"])
    linha = visao[visao["protocolo"] == escolha].iloc[0]
    with st.expander(f"{linha['protocolo']} — {linha['titulo']}", expanded=True):
        for campo in [
            "nome", "email", "telefone", "instituicao", "titulacao", "minicurriculo",
            "coministrantes", "eixo", "nivel", "resumo", "objetivos", "metodologia",
            "publico_alvo", "prerequisitos", "carga_horaria", "referencias",
            "modalidade", "espaco", "recursos", "softwares", "materiais",
            "disponibilidade", "enviado_em",
        ]:
            valor = linha[campo]
            if valor:
                st.markdown(f"**{campo.replace('_', ' ').capitalize()}:** {valor}")

    base_nome = f"propostas_oficinas_{EVENTO.lower()}_{EDICAO}"
    d1, d2 = st.columns(2)
    d1.download_button(
        "Baixar todas as propostas (CSV)",
        df.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"{base_nome}.csv",
        mime="text/csv",
    )
    d2.download_button(
        "Baixar todas as propostas (Excel)",
        para_excel(df),
        file_name=f"{base_nome}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(
        page_title=f"Oficinas — {EVENTO} {EDICAO}", page_icon="🔬", layout="centered"
    )
    criar_tabela()

    pagina = st.sidebar.radio("Navegação", ["Enviar proposta", "Coordenação"])
    if pagina == "Enviar proposta":
        tela_formulario()
    else:
        tela_coordenacao()


if __name__ == "__main__":
    main()
