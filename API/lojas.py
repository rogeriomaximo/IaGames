"""
Clientes das lojas Steam, Epic Games e Microsoft Store.

Substitui o antigo games.csv: os jogos agora vêm de requisições às APIs públicas
das lojas. Cada jogo é devolvido num formato único:

    {"loja": "Steam" | "Epic Games" | "Microsoft Store",
     "id": "...", "nome": "...", "preco": 59.9 (R$; 0 = grátis; None = desconhecido),
     "imagem": "https://...", "link": "https://...",
     "lancamento": "2015", "generos": ["Ação", ...]}

ATENÇÃO: são APIs públicas NÃO oficiais/não documentadas; podem mudar sem aviso.
Cada loja é consultada de forma independente: se uma falhar (timeout, bloqueio,
mudança no formato), as outras continuam funcionando e o erro aparece em "status".
Use GET /lojas/status na API para testar as três de uma vez.

Só usa a biblioteca padrão (urllib), sem dependências extras.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait

PAIS = "BR"
IDIOMA = "pt-BR"

# ---- Ajustes de desempenho (pensados para servidor com pouca RAM/CPU) ----
TIMEOUT = 6               # segundos máximos de cada requisição HTTP a uma loja
PRAZO_BUSCA = 12          # segundos máximos de uma busca inteira (todas as lojas juntas)
CACHE_SEGUNDOS = 30 * 60  # um resultado fica "fresco" por 30 min
CACHE_PARCIAL_SEGUNDOS = 60   # busca em que alguma loja falhou: guarda só por 1 min
CACHE_VELHO_SEGUNDOS = 24 * 3600  # se TODAS as lojas falharem, serve o último resultado bom (até 24 h)
CACHE_MAX_ITENS = 300     # limite de itens guardados (evita crescer sem parar na memória)
MAX_RESPOSTA_BYTES = 2_000_000    # ignora respostas gigantes (poupa RAM)
FALHAS_PARA_PAUSAR = 2    # após 2 falhas seguidas de uma loja...
PAUSA_LOJA = 60           # ...ela é "pulada" por 60 s, para não atrasar todas as buscas
MAX_THREADS = 4           # threads de trabalho (reaproveitadas; poucas = menos RAM)
USER_AGENT = "Mozilla/5.0 (compatible; PlaySpecAI/1.0)"

LOJAS = ("Steam", "Epic Games", "Microsoft Store")

# Tags da Steam (ID usado em /search/results?tags=...) por termo em inglês
STEAM_TAGS = {
    "action": 19, "adventure": 21, "rpg": 122, "strategy": 9, "simulation": 599,
    "sports": 701, "racing": 699, "horror": 1667, "puzzle": 1664, "indie": 492,
    "fps": 1663, "shooter": 1774, "survival": 1662, "open world": 1695,
    "multiplayer": 3859, "co-op": 1685, "fighting": 1743, "platformer": 1625,
    "roguelike": 1716, "roguelite": 3959, "souls-like": 29482, "metroidvania": 1628,
    "zombies": 1659, "farming": 87918, "visual novel": 3799, "pixel graphics": 3964,
    "tactical": 1708, "stealth": 1687, "anime": 4085, "building": 1643,
    "base building": 7332, "crafting": 1702, "battle royale": 176981,
    "massively multiplayer": 128, "soccer": 1679, "football": 1254, "basketball": 1746,
    "driving": 1644, "moba": 1718, "jrpg": 4434, "cozy": 4726, "relaxing": 1654,
    "third-person shooter": 3814, "bullet hell": 4885, "boomer shooter": 1022703,
    "difficult": 4026, "dating sim": 9551, "pvp": 1775, "pve": 1776,
    "card game": 1666, "city builder": 4328, "sandbox": 3810, "casual": 597,
}


# ----------------------------------------------------------------------
# HTTP + cache
# ----------------------------------------------------------------------

class _Cache:
    """Cache em memória com limite de tamanho (LRU) e idade máxima definida na leitura."""

    def __init__(self, maximo):
        self.maximo = maximo
        self._d = OrderedDict()
        self._trava = threading.Lock()
        self.acertos = 0
        self.erros = 0

    def ler(self, chave, idade_max):
        with self._trava:
            item = self._d.get(chave)
            if item and time.time() - item[0] < idade_max:
                self._d.move_to_end(chave)
                self.acertos += 1
                return item[1]
            self.erros += 1
            return None

    def gravar(self, chave, valor):
        with self._trava:
            self._d[chave] = (time.time(), valor)
            self._d.move_to_end(chave)
            while len(self._d) > self.maximo:
                self._d.popitem(last=False)

    def limpar(self):
        with self._trava:
            self._d.clear()

    def info(self):
        with self._trava:
            return {"itens": len(self._d), "maximo": self.maximo, "acertos": self.acertos, "falhas": self.erros}


_cache = _Cache(CACHE_MAX_ITENS)
_travas_chave = {}            # uma trava por consulta: pedidos iguais e simultâneos esperam o primeiro
_travas_chave_lock = threading.Lock()

# threads reaproveitadas (em vez de criar/destruir a cada pesquisa)
_POOL_LOJAS = ThreadPoolExecutor(max_workers=MAX_THREADS, thread_name_prefix="loja")
_POOL_DETALHES = ThreadPoolExecutor(max_workers=MAX_THREADS, thread_name_prefix="det")

# controle de lojas que andam falhando
_falhas = {}                  # loja -> falhas seguidas
_pausada_ate = {}             # loja -> instante (time.time) até quando fica pulada
_estado_lock = threading.Lock()


def _http(url, dados=None, cabecalhos=None):
    """GET (ou POST JSON, se 'dados') -> objeto JSON. Levanta exceção em qualquer falha."""
    h = {"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Language": "pt-BR,pt;q=0.9"}
    if cabecalhos:
        h.update(cabecalhos)
    corpo = None
    if dados is not None:
        corpo = json.dumps(dados).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=corpo, headers=h)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        bruto = r.read(MAX_RESPOSTA_BYTES + 1)
    if len(bruto) > MAX_RESPOSTA_BYTES:
        raise ValueError("resposta grande demais")
    return json.loads(bruto.decode("utf-8", errors="replace"))


def _com_cache(chave, funcao):
    """Cache de uma chamada HTTP: usa o guardado se estiver fresco; senão chama (uma vez só por chave)."""
    valor = _cache.ler(chave, CACHE_SEGUNDOS)
    if valor is not None:
        return valor
    with _travas_chave_lock:
        trava = _travas_chave.setdefault(chave, threading.Lock())
    with trava:                       # pedidos iguais ao mesmo tempo: só o primeiro vai à loja
        valor = _cache.ler(chave, CACHE_SEGUNDOS)
        if valor is not None:
            return valor
        try:
            valor = funcao()
        except Exception:
            antigo = _cache.ler(chave, CACHE_VELHO_SEGUNDOS)   # loja fora do ar: usa o último resultado bom
            if antigo is not None:
                return antigo
            raise
        finally:
            with _travas_chave_lock:
                _travas_chave.pop(chave, None)
    _cache.gravar(chave, valor)
    return valor


def _mapear(funcao, itens, prazo=PRAZO_BUSCA):
    """Aplica 'funcao' aos itens nas threads reaproveitadas; devolve só o que terminou dentro do prazo."""
    futuros = [_POOL_DETALHES.submit(_seguro(funcao), i) for i in itens]
    feitos, _ = wait(futuros, timeout=prazo)
    for f in futuros:
        if f not in feitos:
            f.cancel()
    return [f.result() for f in futuros if f in feitos]


def _url(base, **params):
    return base + "?" + urllib.parse.urlencode(params)


def _preco(valor, divisor=1.0):
    try:
        return round(float(valor) / divisor, 2)
    except (TypeError, ValueError):
        return None


def _https(u):
    if not u:
        return None
    u = str(u)
    return "https:" + u if u.startswith("//") else u


# ----------------------------------------------------------------------
# Steam
# ----------------------------------------------------------------------

def _steam_imagem(app_id):
    return f"https://cdn.akamai.steamstatic.com/steam/apps/{app_id}/header.jpg"


def _steam_link(app_id):
    return f"https://store.steampowered.com/app/{app_id}/"


def steam_detalhes(app_id):
    """appdetails de um jogo (gêneros, lançamento, preço em R$, imagem)."""
    def buscar():
        j = _http(_url("https://store.steampowered.com/api/appdetails",
                       appids=app_id, cc=PAIS.lower(), l="english"))
        item = j.get(str(app_id)) or {}
        if not item.get("success"):
            return None
        d = item["data"]
        if d.get("type") not in (None, "game"):
            return None
        if d.get("is_free"):
            preco = 0.0
        else:
            preco = _preco((d.get("price_overview") or {}).get("final"), 100)
        return {
            "loja": "Steam", "id": str(app_id), "nome": d.get("name"),
            "preco": preco,
            "imagem": d.get("header_image") or _steam_imagem(app_id),
            "link": _steam_link(app_id),
            "lancamento": (d.get("release_date") or {}).get("date"),
            "generos": [g.get("description") for g in d.get("genres") or [] if g.get("description")],
            "categorias": [c.get("description") for c in d.get("categories") or [] if c.get("description")],
            "windows": (d.get("platforms") or {}).get("windows", True),
        }
    return _com_cache(("steam_det", str(app_id)), buscar)


def _steam_enriquecer(ids):
    resultados = _mapear(steam_detalhes, ids)
    return [r for r in resultados if r and r.get("nome") and r.get("windows", True)]


def _seguro(f):
    def interno(*a):
        try:
            return f(*a)
        except Exception:
            return None
    return interno


def steam_por_nome(termo, limite=6):
    j = _com_cache(("steam_busca", termo.lower(), limite), lambda: _http(
        _url("https://store.steampowered.com/api/storesearch/",
             term=termo, l="portuguese", cc=PAIS.lower())))
    ids = [it["id"] for it in (j.get("items") or []) if it.get("id")][:limite]
    return _steam_enriquecer(ids)


def steam_por_tags(termos, limite=20):
    """Mais bem avaliados da Steam para as tags (termos em inglês)."""
    tag_ids = [str(STEAM_TAGS[t]) for t in termos if t in STEAM_TAGS]
    if not tag_ids:
        return None   # sem tag conhecida: o chamador usa busca por texto
    j = _com_cache(("steam_tags", tuple(tag_ids), limite), lambda: _http(
        _url("https://store.steampowered.com/search/results/",
             query="", start=0, count=limite, tags=",".join(tag_ids), sort_by="Reviews_DESC",
             category1=998, os="win", cc=PAIS.lower(), l="portuguese", json=1)))
    ids = []
    for it in j.get("items") or []:
        m = re.search(r"/apps/(\d+)/", it.get("logo") or "")
        if m:
            ids.append(m.group(1))
    return _steam_enriquecer(ids[:limite])


# ----------------------------------------------------------------------
# Epic Games
# ----------------------------------------------------------------------

_EPIC_QUERY = """
query searchStoreQuery($allowCountries: String, $country: String!, $keywords: String,
                       $locale: String, $count: Int) {
  Catalog {
    searchStore(allowCountries: $allowCountries, country: $country, keywords: $keywords,
                locale: $locale, count: $count, category: "games/edition/base|bundles/games|editors",
                sortBy: "relevancy", sortDir: "DESC") {
      elements {
        title id namespace effectiveDate productSlug urlSlug
        keyImages { type url }
        tags { id name }
        catalogNs { mappings(pageType: "productHome") { pageSlug pageType } }
        offerMappings { pageSlug pageType }
        price(country: $country) { totalPrice { discountPrice originalPrice currencyInfo { decimals } } }
      }
    }
  }
}
"""


def _epic_item(e):
    slug = None
    for lista in ((e.get("catalogNs") or {}).get("mappings"), e.get("offerMappings")):
        for m in lista or []:
            if m.get("pageSlug"):
                slug = m["pageSlug"]
                break
        if slug:
            break
    slug = slug or e.get("productSlug") or e.get("urlSlug")
    if not slug:
        return None
    slug = slug.split("/")[0] if slug.endswith("/home") else slug
    imagens = {i.get("type"): i.get("url") for i in e.get("keyImages") or []}
    imagem = next((imagens[t] for t in ("OfferImageWide", "DieselStoreFrontWide", "Thumbnail",
                                        "OfferImageTall") if imagens.get(t)), None)
    tp = ((e.get("price") or {}).get("totalPrice") or {})
    dec = (tp.get("currencyInfo") or {}).get("decimals", 2)
    preco = _preco(tp.get("discountPrice"), 10 ** dec) if tp else None
    return {
        "loja": "Epic Games", "id": e.get("id"), "nome": e.get("title"), "preco": preco,
        "imagem": imagem, "link": f"https://store.epicgames.com/{IDIOMA}/p/{slug}",
        "lancamento": (e.get("effectiveDate") or "")[:4] or None,
        "generos": [t["name"] for t in e.get("tags") or [] if t.get("name")],
        "categorias": [], "windows": True,
    }


def epic_busca(termo, limite=6):
    def buscar():
        j = _http("https://store.epicgames.com/graphql",
                  {"query": _EPIC_QUERY,
                   "variables": {"country": PAIS, "allowCountries": PAIS, "locale": IDIOMA,
                                 "keywords": termo, "count": limite}},
                  {"Origin": "https://store.epicgames.com", "Referer": "https://store.epicgames.com/"})
        elementos = (((j.get("data") or {}).get("Catalog") or {}).get("searchStore") or {}).get("elements") or []
        return [x for x in (_epic_item(e) for e in elementos) if x and x.get("nome")]
    return _com_cache(("epic", termo.lower(), limite), buscar)[:limite]


# ----------------------------------------------------------------------
# Microsoft Store
# ----------------------------------------------------------------------

def _ms_detalhes(big_id):
    def buscar():
        j = _http(_url("https://displaycatalog.mp.microsoft.com/v7.0/products",
                       bigIds=big_id, market=PAIS, languages=IDIOMA,
                       **{"MS-CV": "DGU1mcuYo0WMMp+F.1"}))
        produtos = j.get("Products") or []
        if not produtos:
            return None
        p = produtos[0]
        loc = (p.get("LocalizedProperties") or [{}])[0]
        imagens = loc.get("Images") or []
        imagem = None
        for tipo in ("BoxArt", "Poster", "Tile", "Logo"):
            imagem = next((i.get("Uri") for i in imagens if i.get("ImagePurpose") == tipo), None)
            if imagem:
                break
        imagem = imagem or (imagens[0].get("Uri") if imagens else None)
        preco = None
        try:
            av = p["DisplaySkuAvailabilities"][0]["Availabilities"][0]
            preco = _preco(av["OrderManagementData"]["Price"]["ListPrice"])
        except (KeyError, IndexError, TypeError):
            pass
        props = p.get("Properties") or {}
        data = (p.get("MarketProperties") or [{}])[0].get("OriginalReleaseDate") or ""
        return {
            "loja": "Microsoft Store", "id": big_id, "nome": loc.get("ProductTitle"),
            "preco": preco, "imagem": _https(imagem),
            "link": f"https://apps.microsoft.com/detail/{big_id}?hl={IDIOMA}&gl={PAIS}",
            "lancamento": data[:4] or None,
            "generos": [props["Category"]] if props.get("Category") else [],
            "categorias": [], "windows": True,
            "_tipo": props.get("Category") or "",
        }
    return _com_cache(("ms_det", big_id), buscar)


def microsoft_busca(termo, limite=6):
    def sugerir():
        return _http(_url("https://www.microsoft.com/msstoreapiprod/api/autosuggest",
                          market=IDIOMA.lower(), clientId="7F27B536-CF6B-4C65-8638-A0F8CBDFCA65",
                          sources="Iris-Products,DCatAll-Products", filter="ExcludeDCatAll",
                          counts="0,0,10", query=termo))
    j = _com_cache(("ms_busca", termo.lower()), sugerir)
    ids = []
    for conjunto in j.get("ResultSets") or []:
        for s in conjunto.get("Suggests") or []:
            if s.get("Source") == "Microsoft-Terms":
                continue
            for meta in s.get("Metas") or []:
                if meta.get("Key") == "BigCatalogId" and meta.get("Value") not in ids:
                    ids.append(meta["Value"])
    det = _mapear(_ms_detalhes, ids[:limite * 2])
    jogos = [d for d in det
             if d and d.get("nome") and (not d.get("_tipo") or "game" in d["_tipo"].lower())]
    return jogos[:limite]


# ----------------------------------------------------------------------
# Interface única
# ----------------------------------------------------------------------

def _chave(nome):
    return re.sub(r"[^a-z0-9]+", "", (nome or "").lower())


def _registrar(loja, ok):
    with _estado_lock:
        if ok:
            _falhas[loja] = 0
            _pausada_ate.pop(loja, None)
        else:
            _falhas[loja] = _falhas.get(loja, 0) + 1
            if _falhas[loja] >= FALHAS_PARA_PAUSAR:
                _pausada_ate[loja] = time.time() + PAUSA_LOJA


def _pausada(loja):
    with _estado_lock:
        ate = _pausada_ate.get(loja)
        if ate and time.time() < ate:
            return int(ate - time.time()) + 1
        _pausada_ate.pop(loja, None)
        return 0


def _rodar(tarefas, usar_pausa=True):
    """
    tarefas: {loja: callable}. Roda as lojas em paralelo com PRAZO_BUSCA no total.
    Loja lenta ou fora do ar não segura as outras: vira um erro no 'status' e a busca segue.
    """
    jogos, status, futuros = [], {}, {}
    inicio = time.time()

    def uma(fn):
        t0 = time.time()
        res = fn() or []
        return res, int((time.time() - t0) * 1000)

    for loja, fn in tarefas.items():
        espera = _pausada(loja) if usar_pausa else 0
        if espera:
            status[loja] = {"ok": False, "erro": f"pausada por falhas recentes (volta em ~{espera}s)"}
        else:
            futuros[loja] = _POOL_LOJAS.submit(uma, fn)

    wait(list(futuros.values()), timeout=PRAZO_BUSCA)
    for loja, f in futuros.items():
        if not f.done():
            f.cancel()
            status[loja] = {"ok": False, "erro": f"tempo esgotado (> {PRAZO_BUSCA}s)"}
            _registrar(loja, False)
            continue
        try:
            res, ms = f.result()
            status[loja] = {"ok": True, "resultados": len(res), "ms": ms}
            jogos.extend(res)
            _registrar(loja, True)
        except urllib.error.HTTPError as e:
            status[loja] = {"ok": False, "erro": f"HTTP {e.code}"}
            _registrar(loja, False)
        except Exception as e:   # bloqueio de rede, timeout, JSON inesperado...
            status[loja] = {"ok": False, "erro": f"{type(e).__name__}: {e}"[:200]}
            _registrar(loja, False)
    return jogos, status


def _buscar_com_cache(chave, executar):
    """
    Cache da busca inteira (já agrupada):
      - fresco (30 min)            -> devolve na hora, sem tocar nas lojas;
      - tudo ok                    -> guarda 30 min;
      - só algumas lojas falharam  -> guarda 1 min (tenta de novo logo);
      - todas falharam/sem nada    -> devolve o último resultado bom (até 24 h), se houver.
    """
    hit = _cache.ler(("busca", chave), CACHE_SEGUNDOS)
    if hit is not None and hit[2]:
        return _marcar_cache(hit[0], hit[1])
    parcial = _cache.ler(("busca", chave), CACHE_PARCIAL_SEGUNDOS)
    if parcial is not None:
        return _marcar_cache(parcial[0], parcial[1])
    with _travas_chave_lock:
        trava = _travas_chave.setdefault(("busca", chave), threading.Lock())
    with trava:
        hit = _cache.ler(("busca", chave), CACHE_SEGUNDOS)
        if hit is not None and (hit[2] or _cache.ler(("busca", chave), CACHE_PARCIAL_SEGUNDOS) is not None):
            return _marcar_cache(hit[0], hit[1])
        jogos, status = executar()
        completo = bool(status) and all(v.get("ok") for v in status.values())
        if jogos or completo:          # inclusive "nenhum resultado" confirmado por todas as lojas
            _cache.gravar(("busca", chave), (jogos, status, completo))
        if jogos:
            _cache.gravar(("bom", chave), (jogos, status, completo))
        if not jogos:
            antigo = _cache.ler(("bom", chave), CACHE_VELHO_SEGUNDOS)
            if antigo is not None and not completo:
                return _marcar_cache(antigo[0], antigo[1], antigo=True)
    return jogos, status


def _marcar_cache(jogos, status, antigo=False):
    novo = {l: dict(v, cache=("antigo" if antigo else True)) for l, v in status.items()}
    return jogos, novo


def agrupar(jogos):
    """
    Junta o mesmo jogo vindo de lojas diferentes. Cada grupo vira um jogo com
    'ofertas' (uma por loja). O principal é a oferta mais barata (com foto, de preferência).
    """
    grupos = {}
    for j in jogos:
        grupos.setdefault(_chave(j["nome"]), []).append(j)
    saida = []
    for ofertas in grupos.values():
        ofertas.sort(key=lambda o: (o["preco"] is None, o["preco"] or 0))
        principal = dict(ofertas[0])
        if not principal.get("imagem"):
            principal["imagem"] = next((o["imagem"] for o in ofertas if o.get("imagem")), None)
        for campo in ("lancamento", "generos", "categorias"):
            if not principal.get(campo):
                principal[campo] = next((o[campo] for o in ofertas if o.get(campo)), principal.get(campo))
        principal["ofertas"] = [
            {"loja": o["loja"], "preco": o["preco"], "link": o["link"], "imagem": o.get("imagem")}
            for o in ofertas
        ]
        saida.append(principal)
    return saida


def _executar_nome(termo, limite, lojas, usar_pausa=True):
    tarefas = {}
    if "Steam" in lojas:
        tarefas["Steam"] = lambda: steam_por_nome(termo, limite)
    if "Epic Games" in lojas:
        tarefas["Epic Games"] = lambda: epic_busca(termo, limite)
    if "Microsoft Store" in lojas:
        tarefas["Microsoft Store"] = lambda: microsoft_busca(termo, limite)
    jogos, status = _rodar(tarefas, usar_pausa)
    return agrupar(jogos), status


def buscar_por_nome(termo, limite=6, lojas=LOJAS):
    chave = ("nome", _chave(termo), limite, tuple(lojas))
    return _buscar_com_cache(chave, lambda: _executar_nome(termo, limite, lojas))


def buscar_por_categoria(termos_en, limite=20, lojas=LOJAS):
    """termos_en: tags/gêneros em inglês (ex.: ['horror']). Steam usa tag; Epic/Microsoft, texto."""
    texto = termos_en[0] if termos_en else ""

    def executar():
        tarefas = {}

        def steam():
            r = steam_por_tags(termos_en, limite)
            return r if r is not None else steam_por_nome(texto, limite)

        if "Steam" in lojas:
            tarefas["Steam"] = steam
        if "Epic Games" in lojas:
            tarefas["Epic Games"] = lambda: epic_busca(texto, max(limite // 2, 6))
        if "Microsoft Store" in lojas:
            tarefas["Microsoft Store"] = lambda: microsoft_busca(texto, max(limite // 2, 6))
        jogos, status = _rodar(tarefas)
        return agrupar(jogos), status

    return _buscar_com_cache(("cat", tuple(termos_en), limite, tuple(lojas)), executar)


def info_cache():
    """Números do cache e lojas pausadas (aparece em /saude)."""
    with _estado_lock:
        pausadas = {l: int(t - time.time()) for l, t in _pausada_ate.items() if t > time.time()}
    return {**_cache.info(), "lojas_pausadas_s": pausadas}


def limpar_cache():
    _cache.limpar()
    with _estado_lock:
        _falhas.clear()
        _pausada_ate.clear()


def testar():
    """Diagnóstico (GET /lojas/status): consulta as 3 lojas de verdade, sem cache e sem pausa."""
    limpar_cache()
    _, status = _executar_nome("minecraft", 2, LOJAS, usar_pausa=False)
    return status