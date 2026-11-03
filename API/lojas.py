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
from concurrent.futures import ThreadPoolExecutor

PAIS = "BR"
IDIOMA = "pt-BR"
TIMEOUT = 8            # segundos por requisição
CACHE_SEGUNDOS = 15 * 60
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

_cache = {}
_trava = threading.Lock()


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
        return json.loads(r.read().decode("utf-8", errors="replace"))


def _com_cache(chave, funcao):
    agora = time.time()
    with _trava:
        item = _cache.get(chave)
        if item and agora - item[0] < CACHE_SEGUNDOS:
            return item[1]
    valor = funcao()          # fora da trava: não bloqueia outras requisições
    with _trava:
        _cache[chave] = (agora, valor)
    return valor


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
    with ThreadPoolExecutor(max_workers=8) as ex:
        resultados = list(ex.map(_seguro(steam_detalhes), ids))
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
    with ThreadPoolExecutor(max_workers=6) as ex:
        det = list(ex.map(_seguro(_ms_detalhes), ids[:limite * 2]))
    jogos = [d for d in det
             if d and d.get("nome") and (not d.get("_tipo") or "game" in d["_tipo"].lower())]
    return jogos[:limite]


# ----------------------------------------------------------------------
# Interface única
# ----------------------------------------------------------------------

def _chave(nome):
    return re.sub(r"[^a-z0-9]+", "", (nome or "").lower())


def _rodar(tarefas):
    """tarefas: {loja: callable}. Roda em paralelo; devolve (jogos, status por loja)."""
    jogos, status = [], {}

    def uma(item):
        loja, fn = item
        inicio = time.time()
        try:
            res = fn() or []
            return loja, res, {"ok": True, "resultados": len(res), "ms": int((time.time() - inicio) * 1000)}
        except urllib.error.HTTPError as e:
            return loja, [], {"ok": False, "erro": f"HTTP {e.code}"}
        except Exception as e:   # bloqueio de rede, timeout, JSON inesperado...
            return loja, [], {"ok": False, "erro": f"{type(e).__name__}: {e}"[:200]}

    with ThreadPoolExecutor(max_workers=len(tarefas) or 1) as ex:
        for loja, res, st in ex.map(uma, tarefas.items()):
            jogos.extend(res)
            status[loja] = st
    return jogos, status


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


def buscar_por_nome(termo, limite=6, lojas=LOJAS):
    tarefas = {}
    if "Steam" in lojas:
        tarefas["Steam"] = lambda: steam_por_nome(termo, limite)
    if "Epic Games" in lojas:
        tarefas["Epic Games"] = lambda: epic_busca(termo, limite)
    if "Microsoft Store" in lojas:
        tarefas["Microsoft Store"] = lambda: microsoft_busca(termo, limite)
    jogos, status = _rodar(tarefas)
    return agrupar(jogos), status


def buscar_por_categoria(termos_en, limite=20, lojas=LOJAS):
    """termos_en: tags/gêneros em inglês (ex.: ['horror']). Steam usa tag; Epic/Microsoft, texto."""
    texto = termos_en[0] if termos_en else ""
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


def testar():
    """Diagnóstico: uma busca simples em cada loja (usado por GET /lojas/status)."""
    _, status = buscar_por_nome("minecraft", 2)
    return status