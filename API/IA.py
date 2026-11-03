import math
import os
import re
import threading
import unicodedata

import numpy as np
import pandas as pd
from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

import lojas   # clientes das APIs da Steam, Epic Games e Microsoft Store (lojas.py)

# Sistema especialista de recomendação de jogos exposto como API Flask.
# Os jogos vêm das APIs da Steam, Epic Games e Microsoft Store (lojas.py), com foto e link.
#
# Rodar (qualquer SO):  python app.py
# Produção Linux/Mac:   gunicorn --preload -w 2 -b 0.0.0.0:5000 app:app
# Produção Windows:     pip install waitress  &&  waitress-serve --port=5000 app:app
# Dependências:         pip install flask pandas numpy   (lojas.py usa só a biblioteca padrão)

# Pasta do projeto. Por padrão é a pasta onde está este arquivo (funciona em qualquer
# máquina). Pode ser sobrescrita pela variável de ambiente BASE_DIR.
try:
    _PASTA_PADRAO = os.path.dirname(os.path.abspath(__file__))
except NameError:   # notebook/Jupyter/console: __file__ não existe -> usa a pasta atual
    _PASTA_PADRAO = os.getcwd()
BASE_DIR = os.environ.get("BASE_DIR", _PASTA_PADRAO)

# Arquivos usados (dentro de BASE_DIR):
#   Hardware/index.csv   -> tabela de CPUs/GPUs (colunas "Nome" e "Tier (Desempenho)")
ARQUIVO_HARDWARE = os.path.join(BASE_DIR, "Hardware", "index.csv")

# Se na sua tabela de hardware um tier MENOR significa peça MELHOR (ex.: Tier 1 = topo),
# rode com HARDWARE_MENOR_E_MELHOR=1.
HARDWARE_MENOR_E_MELHOR = os.environ.get("HARDWARE_MENOR_E_MELHOR", "0") == "1"

# Taxa usada só para estimar requisitos (os limiares de preço foram pensados em US$).
BRL_POR_USD = 5.5

# Limites padrão da recomendação por categoria (podem ser sobrescritos na requisição)
LIMITE_AVALIACAO_PADRAO = 30    # cada jogo exige requisições às lojas; mantenha baixo
LIMITE_AVALIACAO_MAX = 60
LIMITE_EXIBICAO_PADRAO = 15
LIMITE_EXIBICAO_MAX = 50

AVISO_ESTIMATIVA = (
    "Os requisitos de CPU/GPU/RAM são ESTIMADOS (por gêneros, ano e preço); as lojas não os fornecem de forma padronizada."
)

# As APIs das lojas NÃO trazem requisitos de CPU/GPU/RAM. Por isso o nível exigido por
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

# Gírias/abreviações de GÊNERO usadas por jogadores -> tags/gêneros da Steam (chaves sem
# acento, espaço ou hífen: "battle royale" vira "battleroyale", "co-op" vira "coop").
GIRIAS_CATEGORIAS = {
    "souls": ["souls-like"], "soulslike": ["souls-like"], "soulslikes": ["souls-like"],
    "moba": ["moba"], "br": ["battle royale"], "battleroyale": ["battle royale"],
    "zumbi": ["zombies"], "zumbis": ["zombies"], "zombie": ["zombies"],
    "porradaria": ["fighting", "beat 'em up"], "pancadaria": ["fighting", "beat 'em up"],
    "porrada": ["fighting"], "briga": ["fighting"],
    "fazendinha": ["farming"], "terrorzinho": ["horror"], "susto": ["horror"],
    "assustador": ["horror"], "medo": ["horror"],
    "metroidvania": ["metroidvania"], "gacha": ["gacha"],
    "mmo": ["massively multiplayer"], "mmorpg": ["massively multiplayer"],
    "jrpg": ["jrpg"], "cozy": ["cozy"], "relaxante": ["relaxing"], "tranquilo": ["relaxing"],
    "roguelike": ["roguelike"], "roguelite": ["roguelite"], "rogue": ["roguelike"],
    "futebol": ["soccer", "football"], "basquete": ["basketball"],
    "carro": ["racing", "driving"], "carros": ["racing", "driving"], "dirigir": ["driving"],
    "fps": ["fps"], "tps": ["third-person shooter"],
    "coop": ["co-op"], "cooperativo": ["co-op"], "pvp": ["pvp"], "pve": ["pve"],
    "construcao": ["building", "base building"], "build": ["building"],
    "crafting": ["crafting"], "anime": ["anime"], "waifu": ["anime"],
    "tatico": ["tactical"], "furtivo": ["stealth"], "stealth": ["stealth"],
    "hardcore": ["difficult"], "boomershooter": ["boomer shooter"],
    "bullethell": ["bullet hell"], "vn": ["visual novel"], "visualnovel": ["visual novel"],
    "namoro": ["dating sim"], "simulador": ["simulation"], "mundoaberto": ["open world"],
}

# Gírias/abreviações de NOME de jogo -> nome (ou parte do nome) usado na busca.
# Também funcionam com número colado: "gta5", "re4", "rdr2", "cs2", "bg3", "ds3", "tw3"...
GIRIAS_JOGOS = {
    "gta": "grand theft auto", "gtav": "grand theft auto v", "gtasa": "grand theft auto san andreas",
    "cs": "counter strike", "csgo": "counter strike global offensive", "css": "counter strike source",
    "re": "resident evil", "rdr": "red dead redemption", "bg": "baldur's gate",
    "ds": "dark souls", "er": "elden ring", "eldenring": "elden ring", "tw": "the witcher",
    "tlou": "the last of us", "gow": "god of war", "hk": "hollow knight", "hl": "half life",
    "l4d": "left 4 dead", "l4d2": "left 4 dead 2", "tf": "team fortress", "tf2": "team fortress 2",
    "dbd": "dead by daylight", "pubg": "pubg battlegrounds", "r6": "rainbow six siege",
    "r6s": "rainbow six siege", "cod": "call of duty", "mw": "modern warfare", "bf": "battlefield",
    "nfs": "need for speed", "sdv": "stardew valley", "stardew": "stardew valley",
    "fnaf": "five nights at freddy's", "ac": "assassin's creed", "poe": "path of exile",
    "d4": "diablo 4", "sf": "street fighter", "mk": "mortal kombat", "fh": "forza horizon",
    "ets2": "euro truck simulator 2", "ets": "euro truck simulator", "ats": "american truck simulator",
    "mhw": "monster hunter world", "mhr": "monster hunter rise", "ff": "final fantasy",
    "dmc": "devil may cry", "cp77": "cyberpunk 2077", "cyberpunk": "cyberpunk 2077",
    "ts4": "the sims 4", "sims": "the sims", "rl": "rocket league", "ow": "overwatch",
    "amongus": "among us", "fallguys": "fall guys", "gtfo": "gtfo", "nms": "no man's sky",
    "skyrim": "skyrim", "fo": "fallout", "fo4": "fallout 4", "tes": "elder scrolls",
    "darksouls": "dark souls", "mgs": "metal gear solid", "pvz": "plants vs zombies",
    "lol": "league of legends", "wow": "world of warcraft", "mine": "minecraft",
}

COL_CPU_REQ = "CPU Estimada"
COL_GPU_REQ = "GPU Estimada"
COL_RAM_REQ = "RAM Estimada (GB)"
COL_DIFICULDADE = "Dificuldade"   # Fácil / Médio / Difícil (o quanto o jogo exige da máquina)
COL_NIVEL_HW = "_nivel"   # nível numérico normalizado (1 a 4) das peças de hardware

NIVEIS_DIFICULDADE = {1: "Fácil", 2: "Médio", 3: "Difícil"}
ALIAS_DIFICULDADE = {
    "facil": "Fácil", "fácil": "Fácil", "leve": "Fácil",
    "medio": "Médio", "médio": "Médio", "media": "Médio", "média": "Médio",
    "dificil": "Difícil", "difícil": "Difícil", "pesado": "Difícil",
}

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
# Normalização de texto para buscas flexíveis
# ----------------------------------------------------------------------

def _normalizar(txt):
    """Minúsculas, sem acentos nem símbolos como ® e ™."""
    txt = unicodedata.normalize("NFKD", str(txt)).encode("ascii", "ignore").decode()
    return txt.lower()


def _compacto(txt):
    """'E5-2680 v4' -> 'e52680v4' (sem espaços, hífens ou símbolos)."""
    return re.sub(r"[^a-z0-9]", "", _normalizar(txt))


def _tokens(txt):
    """'Xeon E5-2680 v4' -> {'xeon', 'e5', '2680', 'v4'}."""
    return set(re.findall(r"[a-z0-9]+", _normalizar(txt)))


_ROMANOS = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}


def _tokens_jogo(txt):
    """Como _tokens, mas números romanos viram algarismos ('GTA V' == 'GTA 5', 'Dark Souls III' == '3')."""
    return {_ROMANOS.get(t, t) for t in re.findall(r"[a-z0-9]+", _normalizar(txt))}


def _expandir_girias(texto):
    """
    Troca gírias/abreviações de jogos pelo nome real: 'gta 5' -> 'grand theft auto 5',
    'rdr2' -> 'red dead redemption 2', 'bg3' -> "baldur's gate 3", 'csgo' -> 'counter strike global offensive'.
    """
    norm = _normalizar(texto)
    compacto = re.sub(r"[^a-z0-9]", "", norm)
    if compacto in GIRIAS_JOGOS:
        return GIRIAS_JOGOS[compacto]
    saida = []
    for p in re.findall(r"[a-z0-9]+", norm):
        if p in GIRIAS_JOGOS:
            saida.append(GIRIAS_JOGOS[p])
            continue
        m = re.fullmatch(r"([a-z]+)(\d+[a-z]?)", p)
        if m and m.group(1) in GIRIAS_JOGOS:
            saida.append(GIRIAS_JOGOS[m.group(1)] + " " + m.group(2))
            continue
        saida.append(p)
    return " ".join(saida)


# ----------------------------------------------------------------------
# Carga dos dados (feita uma única vez)
# ----------------------------------------------------------------------

_DADOS = {"hardware": None}
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
    Estima o nível exigido (Fraca / Média / Forte) de CPU e GPU, a RAM (GB) e a
    dificuldade (Fácil / Médio / Difícil) de cada jogo, a partir de tags/gêneros,
    ano de lançamento e preço.
    É uma aproximação: as lojas não trazem os requisitos de forma padronizada.
    """
    def tem(padroes):
        # \b evita falsos positivos (ex.: "rts" dentro de "sports")
        regex = "|".join(r"\b" + re.escape(p) + r"\b" for p in padroes)
        return dados["_texto"].str.contains(regex, regex=True)

    leve = tem(TAGS_LEVES)
    gpu_pesada = tem(TAGS_GPU_PESADA)
    cpu_pesada = tem(TAGS_CPU_PESADA)

    ano = dados["Release date"].astype(str).str.extract(r"(\d{4})")[0].astype(float).fillna(2015)
    preco = dados["Price"].fillna(0) / BRL_POR_USD   # limiares em US$
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
    dados[COL_DIFICULDADE] = np.maximum(gpu_pts, cpu_pts).map(NIVEIS_DIFICULDADE)


def _df_jogos(lista, termos_extra=()):
    """Transforma a lista de jogos das lojas (lojas.py) numa tabela com as colunas do sistema."""
    linhas = []
    extra = ",".join(termos_extra)
    for i, j in enumerate(lista):
        texto = ",".join([*(j.get("generos") or []), *(j.get("categorias") or []), extra]).lower()
        linhas.append({
            "AppID": j.get("id"), "Name": j.get("nome"), "Price": j.get("preco"),
            "Release date": j.get("lancamento") or "",
            "Genres": ", ".join(j.get("generos") or []),
            "Positive": len(lista) - i,          # preserva a ordem de relevância das lojas
            "_texto": texto,
            "Loja": j.get("loja"), "Imagem": j.get("imagem"), "Link": j.get("link"),
            "Ofertas": j.get("ofertas") or [],
        })
    colunas = ["AppID", "Name", "Price", "Release date", "Genres", "Positive", "_texto",
               "Loja", "Imagem", "Link", "Ofertas"]
    dados = pd.DataFrame(linhas, columns=colunas)
    dados["Price"] = pd.to_numeric(dados["Price"], errors="coerce")
    _estimar_requisitos(dados)
    return dados


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

    # versões normalizadas do nome, para a busca flexível por peça
    hardware["_compacto"] = hardware["Nome"].map(_compacto)
    hardware["_tokens"] = hardware["Nome"].map(_tokens)
    return hardware.reset_index(drop=True)


def carregar_dados():
    """Carrega a tabela de hardware na primeira chamada (jogos vêm ao vivo das lojas)."""
    with _TRAVA:
        if _DADOS["hardware"] is None:
            _DADOS["hardware"] = _carregar_hardware()
    return _DADOS["hardware"]


# ----------------------------------------------------------------------
# Sistema especialista
# ----------------------------------------------------------------------

class Sistema:
    """
    Uma instância por requisição: recebe as tabelas (compartilhadas) e guarda o
    hardware informado pelo usuário naquela requisição. Não há estado entre chamadas.
    """

    TIER_ORDEM = TIER_ORDEM

    def __init__(self, hardware_df):
        self.hardware_df = hardware_df
        self.lojas_status = {}     # resultado da consulta a cada loja nesta requisição

        self.preco = None          # orçamento do usuário (R$)
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
                    f"O jogo custa R$ {f['valor_jogo']:.2f} e seu orçamento é R$ {f['preco']:.2f}."
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
        Busca flexível de peças: ignora maiúsculas, acentos, hífens e espaços, e aceita
        as palavras em qualquer ordem. 'e5 2680 v4', 'e5-2680v4' e 'xeon e5 2680 v4'
        acham 'Intel Xeon E5-2680 v4'.
        Ordem: nome mais próximo do digitado, desktop antes de notebook (a menos que o
        usuário escreva 'notebook'/'laptop') e, por fim, o nome mais curto.
        """
        consulta = str(nome).strip()
        compacto = _compacto(consulta)
        tokens = _tokens(consulta)
        df = self.hardware_df
        if not compacto:
            return df.iloc[0:0]

        mascara = df["_compacto"].str.contains(re.escape(compacto), regex=True, na=False)
        mascara |= df["_tokens"].map(lambda t: tokens <= t)
        achados = df[mascara]
        if achados.empty:
            return achados

        quer_notebook = bool(tokens & {"notebook", "laptop"})
        exato = achados["_compacto"] == compacto
        termina = achados["_compacto"].str.endswith(compacto)
        eh_notebook = achados["Nome"].str.contains("notebook", case=False, na=False)

        ordem = (
            (~exato).astype(int) * 100000
            + (~termina).astype(int) * 10000
            + (eh_notebook != quer_notebook).astype(int) * 1000
            + achados["Nome"].str.len()
        )
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
        Avalia UM jogo (uma linha vinda das lojas) contra o orçamento e o
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
            "loja": _py(jogo_info.get("Loja")),
            "imagem": _py(jogo_info.get("Imagem")),
            "link": _py(jogo_info.get("Link")),
            "ofertas": jogo_info.get("Ofertas") or [],
            "dificuldade": _py(jogo_info.get(COL_DIFICULDADE)),
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
        girias = GIRIAS_CATEGORIAS.get(_compacto(texto))
        if girias:   # gíria conhecida: usa só as tags equivalentes (evita casar pedaços de palavras)
            return list(dict.fromkeys(girias))
        termos = [texto]
        if texto in TRADUCAO_CATEGORIAS:
            termos.append(TRADUCAO_CATEGORIAS[texto])
        return list(dict.fromkeys(t for t in termos if t))

    def recomendar_por_categoria(self, categoria, limite_exibicao, limite_avaliacao, dificuldade=None):
        """Opção 1: jogos da categoria que cabem no orçamento e rodam no PC (filtro opcional de dificuldade)."""
        termos = self._termos_busca(categoria)
        if not termos:
            raise ErroRequisicao("Informe a 'categoria' (gênero/tag, ex: terror, estratégia, RPG).")

        lista, self.lojas_status = lojas.buscar_por_categoria(termos, limite_avaliacao)
        resultado = _df_jogos(lista, termos)
        if dificuldade:
            resultado = resultado[resultado[COL_DIFICULDADE] == dificuldade]

        if resultado.empty:
            raise ErroRequisicao(
                "Nenhum jogo encontrado nessa categoria/dificuldade (ou as lojas não responderam: "
                + self._resumo_lojas() + ").", 404)

        total = len(resultado)
        avaliacoes = [self._avaliar_jogo(j) for _, j in resultado.iterrows()]

        rodam = [a for a in avaliacoes if a["status"] == "Roda"]
        fora_orcamento = [a for a in avaliacoes if not a["orcamento_ok"]]
        nao_suportado = [a for a in avaliacoes if not a["hardware_ok"]]

        return {
            "categoria": categoria,
            "dificuldade": dificuldade,
            "termos_buscados": termos,
            "orcamento": self.preco,
            "total_na_categoria": total,
            "lojas_status": self.lojas_status,
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

    def _resumo_lojas(self):
        return "; ".join(f"{l}: {'ok' if st.get('ok') else st.get('erro', 'falhou')}"
                         for l, st in self.lojas_status.items())

    def pesquisar_jogo(self, nome, limite=5):
        """Busca o jogo nas 3 lojas (com gírias expandidas) e devolve uma tabela já com requisitos estimados."""
        for consulta in dict.fromkeys([_expandir_girias(nome), str(nome)]):
            if not _compacto(consulta):
                continue
            lista, self.lojas_status = lojas.buscar_por_nome(consulta, limite)
            if lista:
                return _df_jogos(lista).head(limite)
        return _df_jogos([])

    def resumo_jogo(self, jogo):
        """Dados básicos de um jogo (sem avaliar hardware), com foto e link das lojas."""
        return {
            "app_id": _py(jogo["AppID"]),
            "nome": _py(jogo["Name"]),
            "preco": _py(jogo["Price"]),
            "lancamento": _py(jogo["Release date"]),
            "generos": _py(jogo["Genres"]),
            "dificuldade": _py(jogo[COL_DIFICULDADE]),
            "loja": _py(jogo["Loja"]),
            "imagem": _py(jogo["Imagem"]),
            "link": _py(jogo["Link"]),
            "ofertas": jogo["Ofertas"] if isinstance(jogo["Ofertas"], list) else [],
            "requisitos_estimados": {
                "cpu": _py(jogo[COL_CPU_REQ]),
                "gpu": _py(jogo[COL_GPU_REQ]),
                "ram_gb": _py(jogo[COL_RAM_REQ]),
            },
        }

    def verificar_jogo(self, app_id=None, nome=None):
        """Opção 2: um jogo específico roda no PC e cabe no orçamento?"""
        outros = []
        if app_id is not None:   # app_id = ID do jogo na Steam
            det = lojas.steam_detalhes(app_id)
            if not det:
                raise ErroRequisicao("Jogo não encontrado na Steam.", 404)
            det["ofertas"] = [{"loja": "Steam", "preco": det["preco"], "link": det["link"], "imagem": det["imagem"]}]
            achados = _df_jogos([det])
        else:
            achados = self.pesquisar_jogo(nome)
            if achados.empty:
                raise ErroRequisicao(
                    "Jogo não encontrado nas lojas (" + (self._resumo_lojas() or "sem resposta") + ").", 404)
        jogo = achados.iloc[0]
        outros = [self.resumo_jogo(j) for _, j in achados.iloc[1:].iterrows()]

        return {
            "jogo": self.resumo_jogo(jogo),
            "avaliacao": self._avaliar_jogo(jogo),
            "outros_resultados": outros,
            "lojas_status": self.lojas_status,
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
        raise ErroRequisicao("'orcamento' deve ser um número (em R$).")
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


def _dificuldade(corpo):
    """Lê 'dificuldade' (fácil/médio/difícil, opcional) e devolve o rótulo oficial ou None."""
    bruto = str(corpo.get("dificuldade") or "").strip().lower()
    if not bruto:
        return None
    rotulo = ALIAS_DIFICULDADE.get(bruto)
    if rotulo is None:
        raise ErroRequisicao("'dificuldade' deve ser fácil, médio ou difícil.")
    return rotulo


def _sistema():
    return Sistema(carregar_dados())


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
    hardware = carregar_dados()
    return jsonify({"status": "ok", "fonte_dos_jogos": list(lojas.LOJAS), "pecas_de_hardware": len(hardware)})


@app.get("/lojas/status")
def lojas_status():
    """Testa as 3 lojas com uma busca simples. Abra no navegador para ver qual API está respondendo."""
    return jsonify({"lojas": lojas.testar()})


@app.get("/regras")
def regras():
    """Lista as regras da base de conhecimento."""
    return jsonify({"regras": _sistema().listar_regras()})


@app.get("/hardware/buscar")
def hardware_buscar():
    """Autocomplete de CPU/GPU: GET /hardware/buscar?nome=e5 2680 v4"""
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
    return jsonify({
        "resultados": [sistema.resumo_jogo(j) for _, j in achados.iterrows()],
        "lojas_status": sistema.lojas_status,
    })


@app.post("/jogos/recomendar")
def jogos_recomendar():
    """
    Opção 1: jogos de uma categoria que cabem no orçamento e rodam no PC.
    Corpo: {
      "categoria": "terror", "orcamento": 30,
      "hardware": {"cpu": "i5-9400F", "gpu": "GTX 1650", "ram": 16, "geracao_ram": "DDR4", "mhz": 3200},
      "dificuldade": "fácil",  (opcional: fácil, médio ou difícil)
      "limite": 15,        (opcional)
      "avaliar_top": 30    (opcional: quantos jogos da categoria são buscados nas lojas)
    }
    """
    corpo = _json_do_corpo()
    categoria = str(corpo.get("categoria") or "").strip()
    if not categoria:
        raise ErroRequisicao("Informe a 'categoria' (gênero/tag, ex: terror, estratégia, RPG).")
    dificuldade = _dificuldade(corpo)

    sistema = _sistema()
    sistema.preco = _orcamento(corpo)
    hardware = sistema.definir_hardware(corpo.get("hardware"))
    sistema.exigir_pecas()

    resposta = sistema.recomendar_por_categoria(
        categoria,
        _inteiro(corpo, "limite", LIMITE_EXIBICAO_PADRAO, LIMITE_EXIBICAO_MAX),
        _inteiro(corpo, "avaliar_top", LIMITE_AVALIACAO_PADRAO, LIMITE_AVALIACAO_MAX),
        dificuldade,
    )
    resposta["hardware"] = hardware
    return jsonify(resposta)


@app.post("/jogos/verificar")
def jogos_verificar():
    """
    Opção 2: um jogo específico roda no PC e cabe no orçamento?
    Corpo: {
      "nome": "witcher 3"  (ou "app_id": 292030, ID da Steam),
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