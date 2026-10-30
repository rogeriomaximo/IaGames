import math
import os
import re
import threading

import numpy as np
import pandas as pd
from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

# Sistema especialista de recomendação de jogos (Steam) exposto como API Flask.
#
# Rodar (qualquer SO):  python app.py
# Produção Linux/Mac:   gunicorn --preload -w 2 -b 0.0.0.0:5000 app:app
# Produção Windows:     pip install waitress  &&  waitress-serve --port=5000 app:app
# Dependências:         pip install flask pandas numpy

# Pasta do projeto. Por padrão é a pasta onde está este arquivo (funciona em qualquer
# máquina). Pode ser sobrescrita pela variável de ambiente BASE_DIR.
try:
    _PASTA_PADRAO = os.path.dirname(os.path.abspath(__file__))
except NameError:   # notebook/Jupyter/console: __file__ não existe -> usa a pasta atual
    _PASTA_PADRAO = os.getcwd()
BASE_DIR = os.environ.get("BASE_DIR", _PASTA_PADRAO)

# Arquivos usados (dentro de BASE_DIR):
#   Jogos/games.csv      -> CSV da Steam
#   Hardware/index.csv   -> tabela de CPUs/GPUs (colunas "Nome" e "Tier (Desempenho)")
ARQUIVO_JOGOS = os.path.join(BASE_DIR, "Jogos", "games.csv")
ARQUIVO_HARDWARE = os.path.join(BASE_DIR, "Hardware", "index.csv")

# Se na sua tabela de hardware um tier MENOR significa peça MELHOR (ex.: Tier 1 = topo),
# rode com HARDWARE_MENOR_E_MELHOR=1.
HARDWARE_MENOR_E_MELHOR = os.environ.get("HARDWARE_MENOR_E_MELHOR", "0") == "1"

# O CSV da Steam tem 40 campos por linha, mas o cabeçalho original vem com
# "Discount" e "DLC count" grudados ("DiscountDLC count"), ou seja, 39 nomes.
# Por isso o cabeçalho é ignorado e estes nomes corretos são usados no lugar.
COLUNAS_STEAM = [
    "AppID", "Name", "Release date", "Estimated owners", "Peak CCU", "Required age",
    "Price", "Discount", "DLC count", "About the game", "Supported languages",
    "Full audio languages", "Reviews", "Header image", "Website", "Support url",
    "Support email", "Windows", "Mac", "Linux", "Metacritic score", "Metacritic url",
    "User score", "Positive", "Negative", "Score rank", "Achievements", "Recommendations",
    "Notes", "Average playtime forever", "Average playtime two weeks",
    "Median playtime forever", "Median playtime two weeks", "Developers", "Publishers",
    "Categories", "Genres", "Tags", "Screenshots", "Movies",
]

# Só as colunas que o sistema realmente usa (o arquivo é grande)
COLUNAS_USADAS = [
    "AppID", "Name", "Release date", "Price", "Windows",
    "Positive", "Negative", "Categories", "Genres", "Tags",
]

# Limites padrão da recomendação por categoria (podem ser sobrescritos na requisição)
LIMITE_AVALIACAO_PADRAO = 300
LIMITE_AVALIACAO_MAX = 1000
LIMITE_EXIBICAO_PADRAO = 15
LIMITE_EXIBICAO_MAX = 50

AVISO_ESTIMATIVA = (
    "Os requisitos de CPU/GPU/RAM dos jogos são ESTIMADOS (a base da Steam não os traz)."
)

# O CSV da Steam NÃO traz requisitos de CPU/GPU/RAM. Por isso o nível exigido por
# cada jogo é ESTIMADO a partir das tags/gêneros, ano de lançamento e preço.
TAGS_LEVES = [
    "pixel graphics", "2d", "retro", "text-based", "visual novel", "card game", "puzzle",
    "casual", "turn-based", "minimalist", "hand-drawn", "point & click", "idle", "clicker",
]
TAGS_GPU_PESADA = [
    "realistic", "photorealistic", "open world", "third person", "first-person", "fps",
    "3d", "cinematic", "survival horror",
]
TAGS_CPU_PESADA = [
    "strategy", "simulation", "city builder", "grand strategy", "4x", "rts",
    "massively multiplayer", "management", "base building", "colony sim", "automation",
    "building", "sandbox", "open world",
]

# Tradução simples de termos em português para as tags/gêneros em inglês da Steam
TRADUCAO_CATEGORIAS = {
    "terror": "horror", "estratégia": "strategy", "estrategia": "strategy",
    "corrida": "racing", "esportes": "sports", "esporte": "sports",
    "aventura": "adventure", "ação": "action", "acao": "action",
    "simulação": "simulation", "simulacao": "simulation", "plataforma": "platformer",
    "quebra-cabeça": "puzzle", "quebra-cabeca": "puzzle", "mundo aberto": "open world",
    "luta": "fighting", "tiro": "shooter", "pixel": "pixel graphics",
    "cooperativo": "co-op", "multijogador": "multiplayer", "sobrevivência": "survival",
    "sobrevivencia": "survival", "fazenda": "farming", "indie": "indie", "rpg": "rpg",
}

COL_CPU_REQ = "CPU Estimada"
COL_GPU_REQ = "GPU Estimada"
COL_RAM_REQ = "RAM Estimada (GB)"
COL_NIVEL_HW = "_nivel"   # nível numérico normalizado (1 a 4) das peças de hardware

# Escala usada para converter níveis categóricos (Fraca/Média/Forte...) em números.
TIER_ORDEM = {
    "fraca": 1, "fraco": 1, "baixa": 1, "baixo": 1, "ruim": 1,
    "média": 2, "media": 2, "médio": 2, "medio": 2,
    "moderada": 2, "moderado": 2, "intermediária": 2, "intermediaria": 2,
    "razoável": 2, "razoavel": 2,
    "forte": 3, "alta": 3, "alto": 3, "boa": 3, "bom": 3,
    "muito forte": 4, "excelente": 4, "top": 4,
}

COLUNAS_SCORE = [
    "Tier (Desempenho)", "Tier(Desempenho)", "Tier", "Nível (Desempenho)",
    "Pontuação", "Pontuacao", "Nível", "Nivel", "Score", "Benchmark", "Desempenho",
]


# ----------------------------------------------------------------------
# Erros da API
# ----------------------------------------------------------------------

class ErroRequisicao(Exception):
    """Erro causado por dados inválidos na requisição (vira resposta JSON)."""

    def __init__(self, mensagem, status=400):
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.status = status


def _py(valor):
    """Converte valores do pandas/numpy em tipos nativos do Python (seguros para JSON)."""
    if valor is None or valor is pd.NA or valor is pd.NaT:
        return None
    if isinstance(valor, np.generic):
        valor = valor.item()
    if isinstance(valor, float) and math.isnan(valor):
        return None
    return valor


# ----------------------------------------------------------------------
# Carga dos dados (feita uma única vez)
# ----------------------------------------------------------------------

_DADOS = {"jogos": None, "hardware": None}
_TRAVA = threading.Lock()


def _ler_csv(caminho, **kwargs):
    """Lê um CSV tentando UTF-8 e depois Latin-1, com mensagem clara se o arquivo não existir."""
    if not os.path.exists(caminho):
        raise FileNotFoundError(
            f"Arquivo não encontrado: {caminho}. Ajuste a variável de ambiente BASE_DIR "
            f"(atual: {BASE_DIR})."
        )
    try:
        return pd.read_csv(caminho, encoding="utf-8-sig", **kwargs)
    except UnicodeDecodeError:
        return pd.read_csv(caminho, encoding="latin-1", **kwargs)


def _estimar_requisitos(dados):
    """
    Estima o nível exigido (Fraca / Média / Forte) de CPU e GPU e a RAM (GB)
    de cada jogo, a partir de tags/gêneros, ano de lançamento e preço.
    É uma aproximação: o CSV da Steam não traz os requisitos reais.
    """
    def tem(padroes):
        # \b evita falsos positivos (ex.: "rts" dentro de "sports")
        regex = "|".join(r"\b" + re.escape(p) + r"\b" for p in padroes)
        return dados["_texto"].str.contains(regex, regex=True)

    leve = tem(TAGS_LEVES)
    gpu_pesada = tem(TAGS_GPU_PESADA)
    cpu_pesada = tem(TAGS_CPU_PESADA)

    ano = dados["Release date"].astype(str).str.extract(r"(\d{4})")[0].astype(float).fillna(2015)
    preco = dados["Price"].fillna(0)
    parece_aaa = (ano >= 2018) & (preco >= 40)

    gpu_pts = 1 + (gpu_pesada & (ano >= 2012)).astype(int) + parece_aaa.astype(int)
    gpu_pts = gpu_pts.where(~(leve & ~gpu_pesada), 1)   # jogo leve e sem tag pesada -> fraca
    gpu_pts = gpu_pts.where(ano >= 2012, 1)             # jogos antigos -> fraca
    gpu_pts = gpu_pts.clip(upper=3)

    cpu_pts = 1 + cpu_pesada.astype(int) + parece_aaa.astype(int)
    cpu_pts = cpu_pts.where(ano >= 2012, 1)
    cpu_pts = cpu_pts.clip(upper=3)

    niveis = {1: "Fraca", 2: "Média", 3: "Forte"}
    dados[COL_GPU_REQ] = gpu_pts.map(niveis)
    dados[COL_CPU_REQ] = cpu_pts.map(niveis)
    dados[COL_RAM_REQ] = np.maximum(gpu_pts, cpu_pts).map({1: 4, 2: 8, 3: 16})


def _carregar_jogos():
    """Lê o CSV da Steam, corrige o cabeçalho deslocado e prepara as colunas usadas."""
    dados = _ler_csv(
        ARQUIVO_JOGOS,
        header=0,                # descarta o cabeçalho com defeito...
        names=COLUNAS_STEAM,     # ...e usa os 40 nomes corretos
        usecols=COLUNAS_USADAS,
        low_memory=False,
        on_bad_lines="skip",     # ignora linhas quebradas em vez de derrubar a carga
    )
    dados["Price"] = pd.to_numeric(dados["Price"], errors="coerce")
    dados["AppID"] = pd.to_numeric(dados["AppID"], errors="coerce")
    dados = dados.dropna(subset=["Name", "Price", "AppID"])
    dados["AppID"] = dados["AppID"].astype("int64")
    dados = dados[dados["Windows"].astype(str).str.lower() == "true"].copy()
    dados["Positive"] = pd.to_numeric(dados["Positive"], errors="coerce").fillna(0)

    # texto único (gêneros + tags + categorias) usado nas buscas e nas estimativas
    dados["_texto"] = (
        dados["Genres"].fillna("") + "," + dados["Tags"].fillna("") + "," + dados["Categories"].fillna("")
    ).str.lower()

    _estimar_requisitos(dados)
    return dados.reset_index(drop=True)


def _coluna_score(hardware):
    """Acha, na tabela de hardware, a coluna usada como pontuação/nível/tier."""
    for c in COLUNAS_SCORE:
        if c in hardware.columns:
            return c
    return None


def _normalizar_niveis(hardware):
    """
    Cria a coluna numérica COL_NIVEL_HW (escala parecida com 1=Fraca, 2=Média, 3=Forte, 4=Muito forte),
    para que peças e jogos sejam comparáveis, qualquer que seja o formato da tabela:
      - texto (Fraca/Média/Forte...)       -> mapeado por TIER_ORDEM
      - números pequenos (todos até 4)     -> usados direto
      - números grandes (ex.: benchmark)   -> divididos em 3 faixas por percentil
    """
    coluna = _coluna_score(hardware)
    if coluna is None:
        hardware[COL_NIVEL_HW] = np.nan
        return

    bruto = hardware[coluna]
    numerico = pd.to_numeric(bruto, errors="coerce")
    n_validos = int(bruto.notna().sum())

    if n_validos and int(numerico.notna().sum()) >= 0.8 * n_validos:
        if numerico.max() <= 4:
            nivel = numerico.astype(float)
        else:
            pct = numerico.rank(pct=True)
            nivel = pd.Series(np.where(pct <= 1 / 3, 1, np.where(pct <= 2 / 3, 2, 3)), index=hardware.index)
            nivel = nivel.where(numerico.notna())
        if HARDWARE_MENOR_E_MELHOR:
            nivel = nivel.max() + nivel.min() - nivel
    else:
        nivel = bruto.astype(str).str.strip().str.lower().map(TIER_ORDEM)

    hardware[COL_NIVEL_HW] = nivel.astype(float)


def _carregar_hardware():
    hardware = _ler_csv(ARQUIVO_HARDWARE, sep=None, engine="python")
    hardware.columns = [str(c).strip() for c in hardware.columns]
    if "Nome" not in hardware.columns:
        raise RuntimeError(f"O arquivo de hardware precisa ter a coluna 'Nome'. Colunas: {list(hardware.columns)}")
    hardware = hardware.dropna(subset=["Nome"]).copy()
    hardware["Nome"] = hardware["Nome"].astype(str)
    _normalizar_niveis(hardware)
    return hardware.reset_index(drop=True)


def carregar_dados():
    """Carrega jogos e hardware na primeira chamada e reaproveita nas seguintes."""
    with _TRAVA:
        if _DADOS["jogos"] is None:
            _DADOS["jogos"] = _carregar_jogos()
        if _DADOS["hardware"] is None:
            _DADOS["hardware"] = _carregar_hardware()
    return _DADOS["jogos"], _DADOS["hardware"]


# ----------------------------------------------------------------------
# Sistema especialista
# ----------------------------------------------------------------------

class Sistema:
    """
    Uma instância por requisição: recebe as tabelas (compartilhadas) e guarda o
    hardware informado pelo usuário naquela requisição. Não há estado entre chamadas.
    """

    TIER_ORDEM = TIER_ORDEM

    def __init__(self, jogos_df, hardware_df):
        self.jogos_df = jogos_df
        self.hardware_df = hardware_df

        self.preco = None          # orçamento do usuário (US$)
        self.ram = None
        self.geracao_ram = None
        self.mhz = None
        self.cpu_consulta = None
        self.gpu_consulta = None
        self.cpu_encontrada = None
        self.gpu_encontrada = None

        # BASE DE CONHECIMENTO
        self.regras = self._criar_base_regras()

    # ------------------------------------------------------------------
    # Base de conhecimento + Motor de decisão
    # ------------------------------------------------------------------

    def _criar_base_regras(self):
        """
        Base de conhecimento: cada regra é um dicionário com
        - id / nome: identificação da regra
        - categoria: 'orcamento', 'hardware' ou 'positivo'
        - severidade: 'bloqueia' (impede rodar/comprar), 'alerta' (aviso,
          não impede) ou 'info' (observação positiva)
        - quando(fatos): condição SE
        - mensagem(fatos): explicação ENTÃO, usada na resposta
        """
        return [
            {
                "id": "R1",
                "nome": "RAM mínima geral",
                "categoria": "hardware",
                "severidade": "bloqueia",
                "quando": lambda f: f.get("ram") is not None and f["ram"] < 8,
                "mensagem": lambda f: (
                    f"Você tem {f['ram']}GB de RAM, abaixo do mínimo geral (8GB) "
                    "para jogos atuais."
                ),
            },
            {
                "id": "R2",
                "nome": "RAM abaixo do requisito do jogo",
                "categoria": "hardware",
                "severidade": "bloqueia",
                "quando": lambda f: (
                    f.get("ram") is not None
                    and f.get("ram_req") is not None
                    and not pd.isna(f["ram_req"])
                    and f["ram"] < f["ram_req"]
                ),
                "mensagem": lambda f: (
                    f"RAM insuficiente para este jogo: você tem {f['ram']}GB "
                    f"e o jogo pede {f['ram_req']}GB (estimado)."
                ),
            },
            {
                "id": "R3",
                "nome": "RAM DDR3 com frequência baixa",
                "categoria": "hardware",
                "severidade": "alerta",
                "quando": lambda f: (
                    (f.get("geracao_ram") or "").upper() == "DDR3"
                    and f.get("mhz") is not None
                    and f["mhz"] < 1600
                ),
                "mensagem": lambda f: (
                    f"Memória DDR3 a {f['mhz']}MHz está abaixo do recomendado "
                    "(1600MHz), pode prejudicar o desempenho."
                ),
            },
            {
                "id": "R4",
                "nome": "RAM DDR4 com frequência baixa",
                "categoria": "hardware",
                "severidade": "alerta",
                "quando": lambda f: (
                    (f.get("geracao_ram") or "").upper() == "DDR4"
                    and f.get("mhz") is not None
                    and f["mhz"] < 2400
                ),
                "mensagem": lambda f: (
                    f"Memória DDR4 a {f['mhz']}MHz está abaixo do recomendado "
                    "(2400MHz), pode prejudicar o desempenho."
                ),
            },
            {
                "id": "R5",
                "nome": "RAM DDR5 com frequência baixa",
                "categoria": "hardware",
                "severidade": "alerta",
                "quando": lambda f: (
                    (f.get("geracao_ram") or "").upper() == "DDR5"
                    and f.get("mhz") is not None
                    and f["mhz"] < 3200
                ),
                "mensagem": lambda f: (
                    f"Memória DDR5 a {f['mhz']}MHz está abaixo do recomendado "
                    "(3200MHz), pode prejudicar o desempenho."
                ),
            },
            {
                "id": "R6",
                "nome": "Orçamento insuficiente",
                "categoria": "orcamento",
                "severidade": "bloqueia",
                "quando": lambda f: (
                    f.get("valor_jogo") is not None
                    and f.get("preco") is not None
                    and f["valor_jogo"] > f["preco"]
                ),
                "mensagem": lambda f: (
                    f"O jogo custa US$ {f['valor_jogo']:.2f} e seu orçamento é US$ {f['preco']:.2f}."
                ),
            },
            {
                "id": "R7",
                "nome": "CPU abaixo do requisito mínimo",
                "categoria": "hardware",
                "severidade": "bloqueia",
                "quando": lambda f: f.get("cpu_ok") is False,
                "mensagem": lambda f: (
                    "Sua CPU está abaixo do requisito mínimo estimado do jogo "
                    f"(classificação: {f.get('cpu_classificacao')})."
                ),
            },
            {
                "id": "R8",
                "nome": "GPU abaixo do requisito mínimo",
                "categoria": "hardware",
                "severidade": "bloqueia",
                "quando": lambda f: f.get("gpu_ok") is False,
                "mensagem": lambda f: (
                    "Sua GPU está abaixo do requisito mínimo estimado do jogo "
                    f"(classificação: {f.get('gpu_classificacao')})."
                ),
            },
            {
                "id": "R9",
                "nome": "CPU não avaliável numericamente",
                "categoria": "hardware",
                "severidade": "alerta",
                "quando": lambda f: bool(
                    f.get("cpu_ok") is None
                    and f.get("cpu_classificacao")
                    and "Não avaliado" in str(f["cpu_classificacao"])
                ),
                "mensagem": lambda f: (
                    f"Não foi possível comparar sua CPU numericamente: "
                    f"{f['cpu_classificacao']}."
                ),
            },
            {
                "id": "R10",
                "nome": "GPU não avaliável numericamente",
                "categoria": "hardware",
                "severidade": "alerta",
                "quando": lambda f: bool(
                    f.get("gpu_ok") is None
                    and f.get("gpu_classificacao")
                    and "Não avaliado" in str(f["gpu_classificacao"])
                ),
                "mensagem": lambda f: (
                    f"Não foi possível comparar sua GPU numericamente: "
                    f"{f['gpu_classificacao']}."
                ),
            },
            {
                "id": "R11",
                "nome": "Desempenho excelente",
                "categoria": "positivo",
                "severidade": "info",
                "quando": lambda f: (
                    f.get("cpu_classificacao") == "Roda muito bem"
                    and f.get("gpu_classificacao") == "Roda muito bem"
                ),
                "mensagem": lambda f: (
                    "Sua CPU e GPU estão bem acima do requisito mínimo — "
                    "o jogo deve rodar muito bem."
                ),
            },
            {
                "id": "R12",
                "nome": "RAM exatamente no limite mínimo",
                "categoria": "hardware",
                "severidade": "alerta",
                "quando": lambda f: (
                    f.get("ram") is not None
                    and f.get("ram_req") is not None
                    and not pd.isna(f["ram_req"])
                    and f["ram"] == f["ram_req"]
                ),
                "mensagem": lambda f: (
                    "Sua RAM está exatamente no requisito mínimo estimado do jogo — "
                    "pode ser justo em momentos de pico de uso."
                ),
            },
        ]

    def listar_regras(self):
        """Lista as regras da base de conhecimento (transparência do sistema)."""
        return [
            {"id": r["id"], "nome": r["nome"], "categoria": r["categoria"], "severidade": r["severidade"]}
            for r in self.regras
        ]

    def _motor_inferencia(self, fatos):
        """
        MOTOR DE DECISÃO: percorre a base de regras e retorna a lista das regras
        que dispararam para os fatos fornecidos. Fatos ausentes são tolerados.
        """
        disparadas = []
        for regra in self.regras:
            try:
                if regra["quando"](fatos):
                    disparadas.append({
                        "id": regra["id"],
                        "nome": regra["nome"],
                        "categoria": regra["categoria"],
                        "severidade": regra["severidade"],
                        "mensagem": regra["mensagem"](fatos),
                    })
            except Exception:
                continue
        return disparadas

    # ------------------------------------------------------------------
    # Comparação de hardware
    # ------------------------------------------------------------------

    def _coluna_score(self):
        return _coluna_score(self.hardware_df)

    def _valor_ordinal(self, valor):
        """Converte um valor numérico OU categórico (Fraca/Média/Forte...) numa escala comparável."""
        if valor is None or (isinstance(valor, float) and math.isnan(valor)):
            return None
        if isinstance(valor, (int, float, np.integer, np.floating)):
            return float(valor)
        return self.TIER_ORDEM.get(str(valor).strip().lower())

    def _classificar_desempenho(self, score_usuario, score_requisito):
        """Rótulo: Não roda / Roda muito mal / Roda mal / Roda mediano / Roda bem / Roda muito bem."""
        if not score_requisito:
            return "Não foi possível avaliar"

        razao = score_usuario / score_requisito

        if razao < 0.5:
            return "Não roda"
        elif razao < 0.8:
            return "Roda muito mal"
        elif razao < 1.0:
            return "Roda mal"
        elif razao < 1.3:
            return "Roda mediano"
        elif razao < 1.8:
            return "Roda bem"
        else:
            return "Roda muito bem"

    def _comparar_score(self, requisito_nome, peca_encontrada):
        """
        Compara a peça do usuário (CPU ou GPU) com o nível exigido pelo jogo.
        Retorna (ok, score_usuario, score_requisito, classificacao);
        ok pode ser True/False/None (None = não deu pra avaliar).
        """
        if peca_encontrada is None:
            return None, None, None, "Não avaliado (hardware do usuário não identificado)"

        requisito_vazio = (
            requisito_nome is None
            or (isinstance(requisito_nome, float) and math.isnan(requisito_nome))
            or str(requisito_nome).strip() == ""
        )
        if requisito_vazio:
            return None, None, None, "Não avaliado (não foi possível estimar o requisito deste jogo)"

        if self._coluna_score() is None:
            return None, None, None, (
                "Não avaliado (a tabela de hardware não tem coluna de nível/pontuação, "
                "ex: 'Tier (Desempenho)')"
            )

        score_usuario = self._valor_ordinal(peca_encontrada.get(COL_NIVEL_HW))
        score_requisito = self._valor_ordinal(requisito_nome)

        if score_usuario is None or score_requisito is None:
            return None, None, None, (
                f"Não avaliado (não reconheci o nível '{requisito_nome}' do jogo "
                f"ou '{_py(peca_encontrada.get(self._coluna_score()))}' do seu hardware)"
            )

        ok = score_usuario >= score_requisito
        classificacao = self._classificar_desempenho(score_usuario, score_requisito)
        return ok, score_usuario, score_requisito, classificacao

    # ------------------------------------------------------------------
    # Hardware do usuário
    # ------------------------------------------------------------------

    def buscar_hardware(self, nome):
        """
        Procura peças na tabela de hardware pelo nome (parcial, sem diferenciar maiúsculas).
        Resultados com nome exato vêm primeiro, depois os de nome mais curto.
        """
        consulta = str(nome).strip()
        nomes = self.hardware_df["Nome"]
        achados = self.hardware_df[nomes.str.contains(consulta, case=False, na=False, regex=False)]
        if achados.empty:
            return achados
        exato = achados["Nome"].str.strip().str.lower() == consulta.lower()
        ordem = (~exato).astype(int) * 100000 + achados["Nome"].str.len()
        return achados.iloc[np.argsort(ordem.values, kind="stable")]

    def definir_hardware(self, hw):
        """
        Valida e registra o hardware informado na requisição. Espera:
        {"cpu": "i5-9400F", "gpu": "GTX 1650", "ram": 16, "geracao_ram": "DDR4", "mhz": 3200}
        Devolve um resumo (peças encontradas + observações sobre a RAM).
        """
        if not isinstance(hw, dict):
            raise ErroRequisicao("Envie o campo 'hardware' com cpu, gpu, ram, geracao_ram e mhz.")

        cpu = str(hw.get("cpu") or "").strip()
        gpu = str(hw.get("gpu") or "").strip()
        if not cpu or not gpu:
            raise ErroRequisicao("Informe 'cpu' e 'gpu' dentro de 'hardware'.")

        try:
            self.ram = int(hw["ram"])
        except (KeyError, TypeError, ValueError):
            raise ErroRequisicao("'ram' deve ser um número inteiro (GB) dentro de 'hardware'.")
        if self.ram <= 0:
            raise ErroRequisicao("'ram' deve ser maior que zero.")

        self.geracao_ram = (str(hw.get("geracao_ram") or "").strip().upper()) or None
        if self.geracao_ram not in (None, "DDR3", "DDR4", "DDR5"):
            raise ErroRequisicao("'geracao_ram' deve ser DDR3, DDR4 ou DDR5.")

        self.mhz = None
        if hw.get("mhz") not in (None, ""):
            try:
                self.mhz = int(hw["mhz"])
            except (TypeError, ValueError):
                raise ErroRequisicao("'mhz' deve ser um número inteiro.")

        self.cpu_consulta, self.gpu_consulta = cpu, gpu
        res_cpu = self.buscar_hardware(cpu)
        res_gpu = self.buscar_hardware(gpu)
        self.cpu_encontrada = res_cpu.iloc[0] if not res_cpu.empty else None
        self.gpu_encontrada = res_gpu.iloc[0] if not res_gpu.empty else None

        coluna_score = self._coluna_score()

        def descrever(consulta, achada):
            return {
                "consulta": consulta,
                "encontrada": _py(achada["Nome"]) if achada is not None else None,
                "nivel": _py(achada[coluna_score]) if achada is not None and coluna_score else None,
                "nivel_normalizado": _py(achada[COL_NIVEL_HW]) if achada is not None else None,
            }

        fatos_ram = {
            "ram": self.ram, "ram_req": None, "geracao_ram": self.geracao_ram, "mhz": self.mhz,
            "preco": None, "valor_jogo": None, "cpu_ok": None, "gpu_ok": None,
            "cpu_classificacao": None, "gpu_classificacao": None,
        }
        observacoes = [r["mensagem"] for r in self._motor_inferencia(fatos_ram)]

        return {
            "cpu": descrever(cpu, self.cpu_encontrada),
            "gpu": descrever(gpu, self.gpu_encontrada),
            "ram_gb": self.ram,
            "geracao_ram": self.geracao_ram,
            "mhz": self.mhz,
            "observacoes_ram": observacoes,
        }

    def exigir_pecas(self):
        """Impede avaliar jogos quando CPU/GPU não existem na tabela (evita 'Roda' falso)."""
        faltando = []
        if self.cpu_encontrada is None:
            faltando.append(f"CPU '{self.cpu_consulta}'")
        if self.gpu_encontrada is None:
            faltando.append(f"GPU '{self.gpu_consulta}'")
        if faltando:
            raise ErroRequisicao(
                "Não encontrei na tabela de hardware: " + " e ".join(faltando)
                + ". Use GET /hardware/buscar?nome=... para ver os nomes disponíveis.",
                404,
            )

    # ------------------------------------------------------------------
    # Avaliação de jogos
    # ------------------------------------------------------------------

    def _avaliar_jogo(self, jogo_info):
        """
        Avalia UM jogo (uma linha da base da Steam) contra o orçamento e o
        hardware da requisição, usando a base de regras + o motor de inferência.
        """
        valor = _py(jogo_info.get("Price"))
        cpu_req = jogo_info.get(COL_CPU_REQ)
        gpu_req = jogo_info.get(COL_GPU_REQ)
        ram_req = _py(jogo_info.get(COL_RAM_REQ))

        cpu_ok, _, _, cpu_class = self._comparar_score(cpu_req, self.cpu_encontrada)
        gpu_ok, _, _, gpu_class = self._comparar_score(gpu_req, self.gpu_encontrada)

        fatos = {
            "ram": self.ram,
            "ram_req": ram_req,
            "geracao_ram": self.geracao_ram,
            "mhz": self.mhz,
            "preco": self.preco,
            "valor_jogo": valor,
            "cpu_ok": cpu_ok,
            "gpu_ok": gpu_ok,
            "cpu_classificacao": cpu_class,
            "gpu_classificacao": gpu_class,
        }

        disparadas = self._motor_inferencia(fatos)

        orcamento_ok = not any(
            r["severidade"] == "bloqueia" and r["categoria"] == "orcamento" for r in disparadas
        )
        hardware_ok = not any(
            r["severidade"] == "bloqueia" and r["categoria"] == "hardware" for r in disparadas
        )

        if not orcamento_ok and not hardware_ok:
            status = "Fora do orçamento e PC não suporta"
        elif not orcamento_ok:
            status = "Fora do orçamento"
        elif not hardware_ok:
            status = "PC não suporta"
        else:
            status = "Roda"

        return {
            "app_id": _py(jogo_info.get("AppID")),
            "nome": _py(jogo_info.get("Name")),
            "valor": valor,
            "status": status,
            "orcamento_ok": orcamento_ok,
            "hardware_ok": hardware_ok,
            "cpu_classificacao": cpu_class,
            "gpu_classificacao": gpu_class,
            "requisitos_estimados": {
                "cpu": _py(cpu_req),
                "gpu": _py(gpu_req),
                "ram_gb": ram_req,
            },
            "motivos": [r["mensagem"] for r in disparadas if r["severidade"] == "bloqueia"],
            "explicacoes": disparadas,   # todas as regras disparadas, pro "por quê"
        }

    def _termos_busca(self, texto):
        """Devolve o termo digitado + sua tradução (se existir) para buscar nos gêneros/tags."""
        texto = texto.strip().lower()
        termos = [texto]
        if texto in TRADUCAO_CATEGORIAS:
            termos.append(TRADUCAO_CATEGORIAS[texto])
        return list(dict.fromkeys(t for t in termos if t))

    def recomendar_por_categoria(self, categoria, limite_exibicao, limite_avaliacao):
        """Opção 1: jogos da categoria que cabem no orçamento e rodam no PC."""
        termos = self._termos_busca(categoria)
        if not termos:
            raise ErroRequisicao("Informe a 'categoria' (gênero/tag, ex: terror, estratégia, RPG).")

        padrao = "|".join(re.escape(t) for t in termos)
        resultado = self.jogos_df[self.jogos_df["_texto"].str.contains(padrao, regex=True, na=False)]

        if resultado.empty:
            raise ErroRequisicao("Nenhum jogo encontrado nessa categoria.", 404)

        total = len(resultado)
        resultado = resultado.sort_values("Positive", ascending=False).head(limite_avaliacao)
        avaliacoes = [self._avaliar_jogo(j) for _, j in resultado.iterrows()]

        rodam = [a for a in avaliacoes if a["status"] == "Roda"]
        fora_orcamento = [a for a in avaliacoes if not a["orcamento_ok"]]
        nao_suportado = [a for a in avaliacoes if not a["hardware_ok"]]

        return {
            "categoria": categoria,
            "termos_buscados": termos,
            "orcamento": self.preco,
            "total_na_categoria": total,
            "avaliados": len(avaliacoes),
            "resumo": {
                "rodam": len(rodam),
                "fora_orcamento": len(fora_orcamento),
                "pc_nao_suporta": len(nao_suportado),
            },
            "rodam": rodam[:limite_exibicao],
            "fora_orcamento": fora_orcamento[:limite_exibicao],
            "pc_nao_suporta": nao_suportado[:limite_exibicao],
            "aviso": AVISO_ESTIMATIVA,
        }

    # ------------------------------------------------------------------
    # Busca de jogos
    # ------------------------------------------------------------------

    def pesquisar_jogo(self, nome, limite=5):
        """Procura jogos pelo nome (parcial), os mais bem avaliados primeiro."""
        resultado = self.jogos_df[
            self.jogos_df["Name"].astype(str).str.contains(str(nome), case=False, na=False, regex=False)
        ]
        return resultado.sort_values("Positive", ascending=False).head(limite)

    def resumo_jogo(self, jogo):
        """Dados básicos de um jogo (sem avaliar hardware)."""
        return {
            "app_id": _py(jogo["AppID"]),
            "nome": _py(jogo["Name"]),
            "preco": _py(jogo["Price"]),
            "lancamento": _py(jogo["Release date"]),
            "generos": _py(jogo["Genres"]),
            "requisitos_estimados": {
                "cpu": _py(jogo[COL_CPU_REQ]),
                "gpu": _py(jogo[COL_GPU_REQ]),
                "ram_gb": _py(jogo[COL_RAM_REQ]),
            },
        }

    def verificar_jogo(self, app_id=None, nome=None):
        """Opção 2: um jogo específico roda no PC e cabe no orçamento?"""
        outros = []
        if app_id is not None:
            achado = self.jogos_df[self.jogos_df["AppID"] == app_id]
            if achado.empty:
                raise ErroRequisicao("Jogo não encontrado na base de dados.", 404)
            jogo = achado.iloc[0]
        else:
            achados = self.pesquisar_jogo(nome)
            if achados.empty:
                raise ErroRequisicao("Jogo não encontrado na base de dados.", 404)
            jogo = achados.iloc[0]
            outros = [self.resumo_jogo(j) for _, j in achados.iloc[1:].iterrows()]

        return {
            "jogo": self.resumo_jogo(jogo),
            "avaliacao": self._avaliar_jogo(jogo),
            "outros_resultados": outros,   # outros jogos com nome parecido (use o app_id para escolher)
            "aviso": AVISO_ESTIMATIVA,
        }


# ----------------------------------------------------------------------
# API Flask
# ----------------------------------------------------------------------

app = Flask(__name__)
try:
    app.json.ensure_ascii = False   # acentos legíveis no JSON
except AttributeError:
    pass

CORS_ORIGEM = os.environ.get("CORS_ORIGIN", "*")


@app.after_request
def _cors(resposta):
    """CORS simples, para o front-end (outra porta/origem) conseguir chamar a API."""
    resposta.headers["Access-Control-Allow-Origin"] = CORS_ORIGEM
    resposta.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resposta.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resposta


def _json_do_corpo():
    corpo = request.get_json(silent=True)
    if not isinstance(corpo, dict):
        raise ErroRequisicao("Envie um corpo JSON (Content-Type: application/json).")
    return corpo


def _orcamento(corpo):
    try:
        valor = float(corpo["orcamento"])
    except (KeyError, TypeError, ValueError):
        raise ErroRequisicao("'orcamento' deve ser um número (em US$).")
    if math.isnan(valor) or math.isinf(valor):
        raise ErroRequisicao("'orcamento' deve ser um número válido.")
    if valor < 0:
        raise ErroRequisicao("'orcamento' não pode ser negativo.")
    return valor


def _inteiro(corpo, campo, padrao, maximo):
    if corpo.get(campo) in (None, ""):
        return padrao
    try:
        valor = int(corpo[campo])
    except (TypeError, ValueError):
        raise ErroRequisicao(f"'{campo}' deve ser um número inteiro.")
    if valor < 1:
        raise ErroRequisicao(f"'{campo}' deve ser maior que zero.")
    return min(valor, maximo)


def _sistema():
    jogos, hardware = carregar_dados()
    return Sistema(jogos, hardware)


@app.errorhandler(ErroRequisicao)
def _erro_requisicao(e):
    return jsonify({"erro": e.mensagem}), e.status


@app.errorhandler(FileNotFoundError)
def _erro_arquivo(e):
    app.logger.error("%s", e)
    return jsonify({"erro": str(e)}), 503


@app.errorhandler(HTTPException)
def _erro_http(e):
    return jsonify({"erro": e.description}), e.code


@app.errorhandler(Exception)
def _erro_inesperado(e):
    app.logger.exception("Erro inesperado")
    return jsonify({"erro": "Erro interno do servidor."}), 500


@app.get("/saude")
def saude():
    """Confere se a API está no ar e quantos jogos foram carregados."""
    jogos, hardware = carregar_dados()
    return jsonify({"status": "ok", "jogos": len(jogos), "pecas_de_hardware": len(hardware)})


@app.get("/regras")
def regras():
    """Lista as regras da base de conhecimento."""
    return jsonify({"regras": _sistema().listar_regras()})


@app.get("/hardware/buscar")
def hardware_buscar():
    """Autocomplete de CPU/GPU: GET /hardware/buscar?nome=1650"""
    nome = (request.args.get("nome") or "").strip()
    if not nome:
        raise ErroRequisicao("Informe o parâmetro 'nome'.")
    sistema = _sistema()
    coluna_score = sistema._coluna_score()
    achados = sistema.buscar_hardware(nome).head(20)
    return jsonify({
        "resultados": [
            {"nome": _py(p["Nome"]), "nivel": _py(p[coluna_score]) if coluna_score else None}
            for _, p in achados.iterrows()
        ]
    })


@app.post("/hardware/avaliar")
def hardware_avaliar():
    """
    Confere as peças informadas e as observações sobre a RAM.
    Corpo: {"hardware": {"cpu": "...", "gpu": "...", "ram": 16, "geracao_ram": "DDR4", "mhz": 3200}}
    """
    corpo = _json_do_corpo()
    sistema = _sistema()
    return jsonify(sistema.definir_hardware(corpo.get("hardware")))


@app.get("/jogos/buscar")
def jogos_buscar():
    """Busca jogos pelo nome: GET /jogos/buscar?nome=witcher&limite=5"""
    nome = (request.args.get("nome") or "").strip()
    if not nome:
        raise ErroRequisicao("Informe o parâmetro 'nome'.")
    try:
        limite = min(max(int(request.args.get("limite", 5)), 1), 20)
    except ValueError:
        raise ErroRequisicao("'limite' deve ser um número inteiro.")
    sistema = _sistema()
    achados = sistema.pesquisar_jogo(nome, limite)
    return jsonify({"resultados": [sistema.resumo_jogo(j) for _, j in achados.iterrows()]})


@app.post("/jogos/recomendar")
def jogos_recomendar():
    """
    Opção 1: jogos de uma categoria que cabem no orçamento e rodam no PC.
    Corpo: {
      "categoria": "terror", "orcamento": 30,
      "hardware": {"cpu": "i5-9400F", "gpu": "GTX 1650", "ram": 16, "geracao_ram": "DDR4", "mhz": 3200},
      "limite": 15,        (opcional)
      "avaliar_top": 300   (opcional: quantos jogos mais bem avaliados da categoria são analisados)
    }
    """
    corpo = _json_do_corpo()
    categoria = str(corpo.get("categoria") or "").strip()
    if not categoria:
        raise ErroRequisicao("Informe a 'categoria' (gênero/tag, ex: terror, estratégia, RPG).")

    sistema = _sistema()
    sistema.preco = _orcamento(corpo)
    hardware = sistema.definir_hardware(corpo.get("hardware"))
    sistema.exigir_pecas()

    resposta = sistema.recomendar_por_categoria(
        categoria,
        _inteiro(corpo, "limite", LIMITE_EXIBICAO_PADRAO, LIMITE_EXIBICAO_MAX),
        _inteiro(corpo, "avaliar_top", LIMITE_AVALIACAO_PADRAO, LIMITE_AVALIACAO_MAX),
    )
    resposta["hardware"] = hardware
    return jsonify(resposta)


@app.post("/jogos/verificar")
def jogos_verificar():
    """
    Opção 2: um jogo específico roda no PC e cabe no orçamento?
    Corpo: {
      "nome": "witcher 3"  (ou "app_id": 292030),
      "orcamento": 50,
      "hardware": {"cpu": "...", "gpu": "...", "ram": 16, "geracao_ram": "DDR4", "mhz": 3200}
    }
    """
    corpo = _json_do_corpo()

    app_id = None
    if corpo.get("app_id") not in (None, ""):
        try:
            app_id = int(corpo["app_id"])
        except (TypeError, ValueError):
            raise ErroRequisicao("'app_id' deve ser um número inteiro.")
    nome = str(corpo.get("nome") or "").strip()
    if app_id is None and not nome:
        raise ErroRequisicao("Informe 'nome' ou 'app_id' do jogo.")

    sistema = _sistema()
    sistema.preco = _orcamento(corpo)
    hardware = sistema.definir_hardware(corpo.get("hardware"))
    sistema.exigir_pecas()

    resposta = sistema.verificar_jogo(app_id=app_id, nome=nome)
    resposta["hardware"] = hardware
    return jsonify(resposta)


# Pré-carrega os dados na importação (útil com gunicorn --preload / waitress).
# Se falhar, a API sobe mesmo assim e tenta de novo na primeira requisição.
try:
    carregar_dados()
except Exception as _e:
    app.logger.error("Falha ao pré-carregar os dados: %s", _e)


if __name__ == "__main__":
    carregar_dados()   # mostra o erro claramente no terminal se algum arquivo estiver faltando
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False)