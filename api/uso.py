"""Registro de uso do conector e a página de acompanhamento (GET /mcp/<segredo>/uso).

Cada chamada de ferramenta vira um arquivo no Blob privado:
  uso/<AAAA-MM-DD>/<HHMMSS>~<ferramenta>~<ok|erro>~0~<chamadas à Meta>~<aleatório>.json
  (o 0 é um campo reservado, mantido para o formato continuar igual ao do conector com transcrição)
O nome do arquivo já traz o que o gráfico precisa (a página só lista, sem baixar cada um); o conteúdo
(perfis, hashtags, tema, garimpo, painel) só é lido para a lista de garimpos.
Horários em Brasília (UTC-3, sem horário de verão).
"""
import html
import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=-3))
DIAS_GRAFICO = 30
HASHTAGS_SEMANA = 30
MAX_GARIMPOS = 15
CACHE_META_MIN = 30        # a página consulta a Meta no máximo a cada 30 min
ESPERA_FALHA_MIN = 10      # depois de uma recusa da Meta, a página espera 10 min antes de perguntar de novo
ARQ_CACHE_META = "estado/meta.json"
LIMITE_META_HORA = 200     # app em modo de desenvolvimento: ~200 chamadas por hora (estimativa da Meta: 200 × usuários)

# Ordem fixa das séries (a cor segue a ferramenta, nunca a posição no ranking)
SERIES = [
    ("garimpar_perfis", "Garimpos de perfis"),
    ("garimpar_hashtags", "Garimpos de hashtags"),
    ("publicar_painel", "Painéis publicados"),
]
NOMES = dict(SERIES)
# ...~<áudio>~<chamadas à Meta>~<aleatório de 8>.json (registros da versão 2.1 não têm as chamadas à Meta)
RE_ARQ = re.compile(r"^uso/(\d{4}-\d{2}-\d{2})/(\d{6})~([a-z_]+)~(ok|erro)~(\d+)~(?:(\d+)~)?[0-9a-f]{8}\.json$")


def _blob():
    from vercel.blob import sync as blob
    return blob


def agora():
    return datetime.now(TZ)


# --------------------------------------------------------------------------- registro

def _texto(resultado):
    for c in (resultado or {}).get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text":
            return c.get("text") or ""
    return ""


def _lista(v, n=15):
    if isinstance(v, str):
        v = re.split(r"[,\s]+", v)
    return [str(x).strip().lstrip("@#")[:60] for x in (v or []) if str(x).strip()][:n]


def registrar(ferramenta, args, resultado, modo, inicio, meta=0):
    """Grava um evento de uso. Nunca levanta erro: o uso não pode derrubar a ferramenta."""
    try:
        if not re.fullmatch(r"[a-z_]{3,40}", ferramenta or ""):
            return
        ok = not (resultado or {}).get("isError")
        texto = _texto(resultado)
        args = args if isinstance(args, dict) else {}
        audio = 0  # campo reservado (sem transcrição nesta versão)
        meta =max(0, int(meta or 0))
        detalhes = {"quando": agora().isoformat(timespec="seconds"), "ferramenta": ferramenta, "ok": ok,
                    "ms": int((time.time() - inicio) * 1000), "modo": modo, "audio_s": audio, "chamadas_meta": meta}
        m = re.search(r"garimpo_id: ([0-9a-f]{32})", texto)
        if m:
            detalhes["garimpo_id"] = m[1]
        m = re.search(r"/painel/([0-9a-f]{32})", texto)
        if m:
            detalhes["painel"] = m[1]
        m = re.search(r"Referências no garimpo: (\d+)", texto)
        if m:
            detalhes["referencias"] = int(m[1])
        m = re.search(r"destaques: (\d+)", texto)
        if m:
            detalhes["destaques"] = int(m[1])
        if args.get("tema"):
            detalhes["tema"] = str(args["tema"])[:80]
        if args.get("perfis"):
            detalhes["perfis"] = _lista(args["perfis"])
        if args.get("hashtags"):
            detalhes["hashtags"] = _lista(args["hashtags"], 6)
        if not ok:
            detalhes["erro"] = texto[:200]
        t = agora()
        nome = (f"uso/{t:%Y-%m-%d}/{t:%H%M%S}~{ferramenta}~{'ok' if ok else 'erro'}~{audio}~{meta}~"
                f"{uuid.uuid4().hex[:8]}.json")
        _blob().put_blob(nome, json.dumps(detalhes, ensure_ascii=False).encode("utf-8"), access="private",
                         content_type="application/json")
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- leitura

def eventos():
    """Eventos dos últimos DIAS_GRAFICO dias, só pelo nome do arquivo."""
    corte = (agora() - timedelta(days=DIAS_GRAFICO)).strftime("%Y-%m-%d")
    saida = []
    for b in _blob().query_blobs(prefix="uso/"):
        m = RE_ARQ.match(b.pathname or "")
        if not m or m[1] < corte:
            continue
        saida.append({"dia": m[1], "hora": m[2], "ferramenta": m[3], "ok": m[4] == "ok",
                      "audio_s": int(m[5]), "meta": int(m[6] or 0), "pathname": b.pathname})
    saida.sort(key=lambda e: (e["dia"], e["hora"]))
    return saida


def _ler(pathname):
    try:
        return json.loads(_blob().get_blob(pathname, access="private").body)
    except Exception:  # noqa: BLE001
        return None


def garimpos(evts):
    """Agrupa os eventos de garimpo e painel por garimpo_id (lê só o conteúdo desses eventos)."""
    alvo = [e for e in evts if e["ok"] and e["ferramenta"] in ("garimpar_perfis", "garimpar_hashtags",
                                                                "publicar_painel")][-120:]
    with ThreadPoolExecutor(max_workers=8) as ex:
        corpos = list(ex.map(lambda e: _ler(e["pathname"]), alvo))
    grupos = {}
    for e, d in zip(alvo, corpos):
        if not d or not d.get("garimpo_id"):
            continue
        g = grupos.setdefault(d["garimpo_id"], {"tema": None, "perfis": [], "hashtags": [], "referencias": 0,
                                                "paineis": [], "inicio": d.get("quando"), "ultimo": d.get("quando")})
        g["tema"] = g["tema"] or d.get("tema")
        for chave in ("perfis", "hashtags"):
            for x in d.get(chave) or []:
                if x not in g[chave]:
                    g[chave].append(x)
        g["referencias"] = max(g["referencias"], d.get("referencias") or 0)
        if d.get("painel"):
            g["paineis"].append({"id": d["painel"], "quando": d.get("quando"), "destaques": d.get("destaques")})
        g["ultimo"] = d.get("quando") or g["ultimo"]
    return sorted(grupos.values(), key=lambda g: g["ultimo"] or "", reverse=True)[:MAX_GARIMPOS]


CODIGOS_LIMITE = {4, 17, 32, 613, 80002}  # erros de "chamadas demais" da Meta


def _limite_dos_cabecalhos(cabecalhos, limite=None):
    """Lê X-App-Usage (limite do app, em %) e X-Business-Use-Case-Usage (limite da conta do Instagram, com o
    tempo estimado para voltar) e junta com o que já foi lido em outra chamada da mesma consulta."""
    limite = dict(limite or {})
    usos = [limite["uso_pct"]] if limite.get("uso_pct") is not None else []
    voltar = [limite["voltar_min"]] if limite.get("voltar_min") else []
    try:
        app = json.loads(cabecalhos.get("x-app-usage") or "{}")
        usos += [int(app[k]) for k in ("call_count", "total_cputime", "total_time") if isinstance(app.get(k), (int, float))]
    except (ValueError, TypeError, AttributeError):
        pass
    try:
        for itens in json.loads(cabecalhos.get("x-business-use-case-usage") or "{}").values():
            for i in itens or []:
                usos += [int(i[k]) for k in ("call_count", "total_cputime", "total_time")
                         if isinstance(i.get(k), (int, float))]
                if isinstance(i.get("estimated_time_to_regain_access"), (int, float)) and i["estimated_time_to_regain_access"] > 0:
                    voltar.append(int(i["estimated_time_to_regain_access"]))
    except (ValueError, TypeError, AttributeError):
        pass
    if usos:
        limite["uso_pct"] = max(usos)
    if voltar:
        limite["voltar_min"] = max(voltar)
    return limite


class ErroMeta(Exception):
    def __init__(self, motivo, limite, bloqueado):
        super().__init__(motivo)
        self.motivo, self.limite, self.bloqueado = motivo, limite, bloqueado


def _graph_com_cabecalhos(g, caminho, params, tok, ver):
    """Como garimpo.graph_get, mas devolve também os cabeçalhos (inclusive quando a Meta recusa)."""
    import urllib.error
    import urllib.parse
    import urllib.request
    g.registrar_segredo(tok)
    consulta = dict({k: v for k, v in (params or {}).items() if v is not None}, access_token=tok)
    url = f"{g.GRAPH_BASE}/{ver or g.GRAPH_VERSAO_PADRAO}/{caminho.lstrip('/')}?{urllib.parse.urlencode(consulta)}"
    req = urllib.request.Request(url, headers={"User-Agent": g.UA_GARIMPO})
    try:
        with urllib.request.urlopen(req, timeout=g.GRAPH_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace")), r.headers
    except urllib.error.HTTPError as e:
        try:
            corpo = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            corpo = ""
        erro = g._erro_da_resposta(corpo, e.code)
        erro.cabecalhos = e.headers
        raise erro from None


def _consultar_meta():
    """2 chamadas: validade do token (debug_token) e hashtags que contaram na semana, lendo os cabeçalhos de
    limite das duas. Levanta ErroMeta com o motivo e a situação do limite."""
    import app_garimpo  # noqa: F401 - prepara o caminho do garimpo.py e a config em /tmp
    import garimpo as g
    cfg = g.config_efetiva()
    if not g.api_pronta(cfg):
        raise ErroMeta("API da Meta não configurada no servidor", {}, False)
    tok, ver, uid = cfg["token"], cfg.get("versao_api"), cfg["conta_padrao"]
    limite = {}
    try:
        d, cab = _graph_com_cabecalhos(g, g.EP_DEBUG_TOKEN, {"input_token": tok}, tok, ver)
        limite = _limite_dos_cabecalhos(cab, limite)
        d = (d or {}).get("data") or {}
        r = {"token_valido": bool(d.get("is_valid"))}
        if d.get("expires_at"):
            expira = datetime.fromtimestamp(int(d["expires_at"]), TZ)
            r["token_expira_em"] = expira.isoformat(timespec="seconds")
            r["dias_para_expirar"] = (expira - agora()).days
        h, cab = _graph_com_cabecalhos(g, g.EP_HASHTAGS_RECENTES.format(ig_user_id=uid),
                                       {"limit": HASHTAGS_SEMANA}, tok, ver)
        limite = _limite_dos_cabecalhos(cab, limite)
        r["hashtags_7d_usadas"] = len((h or {}).get("data") or [])
    except g.ErroGraph as e:
        limite = _limite_dos_cabecalhos(getattr(e, "cabecalhos", None) or {}, limite)
        bloqueado = e.codigo in CODIGOS_LIMITE
        raise ErroMeta(g.traduzir_erro_graph(e), dict(limite, bloqueado=bloqueado,
                                                      visto_em=agora().isoformat(timespec="seconds")),
                       bloqueado) from None
    except Exception as e:  # noqa: BLE001 - rede etc.
        raise ErroMeta(f"não consegui falar com a Meta ({type(e).__name__})", limite, False) from None
    r["limite"] = dict(limite, bloqueado=False, visto_em=agora().isoformat(timespec="seconds"))
    return r


def _idade(iso):
    try:
        return agora() - datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return timedelta(days=999)


def _com_falha(c):
    f = c["falha"]
    r = dict(c, limite=f.get("limite") or {})
    if c.get("consultado_em"):
        r["aviso"] = (f"A Meta não respondeu às {_data_br(f.get('em'), True)[-5:]} ({f.get('motivo')}). "
                      f"Mostrando os dados de {_data_br(c['consultado_em'], True)}.")
    else:
        r["erro"] = f.get("motivo")
    return r


def situacao_meta():
    """Validade do token, hashtags da semana e situação do limite da Meta.
    Com resposta boa, guarda por CACHE_META_MIN minutos; depois de uma recusa, espera ESPERA_FALHA_MIN minutos
    antes de perguntar de novo (cada pergunta durante o bloqueio pode esticar a espera)."""
    cache = _ler(ARQ_CACHE_META) or {}
    if cache.get("falha"):
        if _idade(cache["falha"].get("em")) < timedelta(minutes=ESPERA_FALHA_MIN):
            return _com_falha(cache)
    elif cache.get("consultado_em") and _idade(cache["consultado_em"]) < timedelta(minutes=CACHE_META_MIN):
        return cache
    try:
        r = _consultar_meta()
        r["consultado_em"] = agora().isoformat(timespec="seconds")
        novo = r
    except ErroMeta as e:
        novo = {k: v for k, v in cache.items() if k != "falha"}
        novo["falha"] = {"em": agora().isoformat(timespec="seconds"), "motivo": e.motivo, "limite": e.limite}
    try:
        _blob().put_blob(ARQ_CACHE_META, json.dumps(novo).encode("utf-8"), access="private",
                         content_type="application/json", allow_overwrite=True)
    except Exception:  # noqa: BLE001
        pass
    return _com_falha(novo) if novo.get("falha") else novo


# --------------------------------------------------------------------------- página

def _e(v):
    return html.escape(str(v if v is not None else ""), quote=True)


def _num(n):
    return f"{int(n):,}".replace(",", ".")


def _data_br(iso, hora=False):
    try:
        d = datetime.fromisoformat(iso).astimezone(TZ)
    except (TypeError, ValueError):
        return ""
    return d.strftime("%d/%m %H:%M" if hora else "%d/%m/%Y")


def _passo(maximo):
    for p in (1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000):
        if maximo / p <= 5:
            return p
    return 1000


def _grafico(dias, contagem):
    """Barras empilhadas, SVG puro. dias: lista de 'AAAA-MM-DD'; contagem[dia][ferramenta] = n."""
    largura, altura = 720, 290
    esq, dir_, topo, base = 40, 8, 14, 40
    area_l, area_a = largura - esq - dir_, altura - topo - base
    totais = [sum(contagem.get(d, {}).get(f, 0) for f, _ in SERIES) for d in dias]
    maximo = max(totais + [1])
    passo = _passo(maximo)
    teto = max(passo, -(-maximo // passo) * passo)
    fatia = area_l / len(dias)
    barra = max(4.0, min(16.0, fatia * 0.62))
    y = lambda v: topo + area_a - (v / teto) * area_a  # noqa: E731
    partes = []
    for v in range(0, teto + 1, passo):
        partes.append(f'<line class="grade" x1="{esq}" x2="{largura - dir_}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
                      f'<text class="eixo" x="{esq - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{v}</text>')
    partes.append(f'<line class="base" x1="{esq}" x2="{largura - dir_}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>')
    for i, d in enumerate(dias):
        cx = esq + fatia * i + fatia / 2
        x0 = cx - barra / 2
        cont = contagem.get(d, {})
        presentes = [(f, cont.get(f, 0)) for f, _ in SERIES if cont.get(f, 0)]
        acumulado = 0
        for k, (f, n) in enumerate(presentes):
            y_base, y_topo = y(acumulado), y(acumulado + n)
            acumulado += n
            gap = 2 if k < len(presentes) - 1 else 0  # 2px de respiro entre segmentos
            h = max(0.0, y_base - y_topo - gap)
            yt = y_topo + gap
            classe = f"s{[s for s, _ in SERIES].index(f) + 1}"
            if k == len(presentes) - 1 and h > 4:  # ponta arredondada (4px) só no topo da pilha
                r = min(4.0, barra / 2)
                partes.append(f'<path class="{classe}" d="M{x0:.1f},{y_base:.1f}V{yt + r:.1f}'
                              f'Q{x0:.1f},{yt:.1f} {x0 + r:.1f},{yt:.1f}H{x0 + barra - r:.1f}'
                              f'Q{x0 + barra:.1f},{yt:.1f} {x0 + barra:.1f},{yt + r:.1f}V{y_base:.1f}Z"/>')
            else:
                partes.append(f'<rect class="{classe}" x="{x0:.1f}" y="{yt:.1f}" width="{barra:.1f}" height="{h:.1f}"/>')
        dd = f"{d[8:10]}/{d[5:7]}"
        dica = f"{dd}: " + (", ".join(f"{NOMES[f]} {n}" for f, n in presentes) if presentes else "sem uso")
        partes.append(f'<rect class="alvo" x="{esq + fatia * i:.1f}" y="{topo}" width="{fatia:.1f}" '
                      f'height="{area_a}"><title>{_e(dica)}</title></rect>')
        if i == len(dias) - 1:  # último rótulo alinhado à borda direita para não ser cortado
            partes.append(f'<text class="eixo" x="{largura - dir_}" y="{altura - 12}" text-anchor="end">{dd}</text>')
        elif i % 5 == 4 and i < len(dias) - 3:
            partes.append(f'<text class="eixo" x="{cx:.1f}" y="{altura - 12}" text-anchor="middle">{dd}</text>')
    return (f'<svg class="grafico" viewBox="0 0 {largura} {altura}" role="img" '
            f'aria-label="Usos por dia nos últimos {len(dias)} dias, por ferramenta">{"".join(partes)}</svg>')


CSS = """
:root{color-scheme:light;--plano:#f9f9f7;--sup:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--mudo:#898781;
--grade:#e1e0d9;--base:#c3c2b7;--borda:rgba(11,11,11,.10);--link:#256abf;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--bom:#006300;--alerta:#b26b00;--critico:#d03b3b}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--plano:#0d0d0d;--sup:#1a1a19;
--ink:#f0efec;--ink2:#c3c2b7;--grade:#2c2c2a;--base:#383835;--borda:rgba(255,255,255,.10);--link:#86b6ef;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--bom:#0ca30c;--alerta:#fab219;--critico:#e66767}}
:root[data-theme="dark"]{color-scheme:dark;--plano:#0d0d0d;--sup:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;
--grade:#2c2c2a;--base:#383835;--borda:rgba(255,255,255,.10);--link:#86b6ef;--s1:#3987e5;--s2:#d95926;
--s3:#199e70;--s4:#c98500;--bom:#0ca30c;--alerta:#fab219;--critico:#e66767}
*{box-sizing:border-box}
body{margin:0;background:var(--plano);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:1.5rem;margin:0 0 4px}h2{font-size:1.05rem;margin:0 0 12px}
.sub{color:var(--ink2);margin:0 0 20px}
.cartao{background:var(--sup);border:1px solid var(--borda);border-radius:12px;padding:16px;margin-bottom:16px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:16px}
.kpi{background:var(--sup);border:1px solid var(--borda);border-radius:12px;padding:14px 16px}
.kpi .rot{color:var(--ink2);font-size:.85rem}.kpi .val{font-size:1.75rem;font-weight:650;line-height:1.2}
.kpi .det{color:var(--mudo);font-size:.82rem}
.medidor{height:6px;border-radius:3px;background:var(--grade);margin-top:8px;overflow:hidden}
.medidor span{display:block;height:100%;border-radius:3px;background:var(--s1)}
.status{font-size:.85rem;font-weight:600}.status.bom{color:var(--bom)}.status.alerta{color:var(--alerta)}
.status.critico{color:var(--critico)}
.legenda{display:flex;flex-wrap:wrap;gap:6px 16px;margin:0 0 8px;padding:0;list-style:none;color:var(--ink2);font-size:.85rem}
.legenda li{display:flex;align-items:center;gap:6px}.legenda i{width:10px;height:10px;border-radius:2px;display:inline-block}
.grafico{width:100%;height:auto;display:block}
.grafico .grade{stroke:var(--grade);stroke-width:1}.grafico .base{stroke:var(--base);stroke-width:1}
.grafico .eixo{fill:var(--mudo);font-size:12px;font-variant-numeric:tabular-nums}
@media (max-width:640px){.grafico .eixo{font-size:24px}}
.grafico .alvo{fill:transparent}.grafico .alvo:hover{fill:var(--ink);fill-opacity:.06}
.s1{fill:var(--s1);background:var(--s1)}.s2{fill:var(--s2);background:var(--s2)}
.s3{fill:var(--s3);background:var(--s3)}.s4{fill:var(--s4);background:var(--s4)}
details{margin-top:10px}summary{cursor:pointer;color:var(--ink2);font-size:.88rem;width:fit-content}
.tabela{overflow-x:auto;margin-top:8px}
table{border-collapse:collapse;font-size:.85rem;font-variant-numeric:tabular-nums;min-width:100%}
th,td{padding:4px 8px;text-align:right;border-bottom:1px solid var(--grade);white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{color:var(--ink2);font-weight:600}
.garimpo{padding:12px 0;border-top:1px solid var(--grade)}.garimpo:first-of-type{border-top:0;padding-top:0}
.garimpo h3{font-size:1rem;margin:0 0 2px}.garimpo .meta{color:var(--mudo);font-size:.82rem;margin:0 0 6px}
.garimpo p{margin:2px 0;color:var(--ink2);font-size:.9rem;overflow-wrap:anywhere}
.paineis{display:flex;flex-wrap:wrap;gap:6px 12px;margin-top:6px}
a{color:var(--link)}.vazio{color:var(--mudo)}
.aviso{margin:-4px 0 16px;padding:10px 14px;border-radius:10px;border:1px solid var(--borda);background:var(--sup);
color:var(--ink2);font-size:.88rem}
footer{color:var(--mudo);font-size:.8rem;margin-top:8px}
"""


def _kpi(rotulo, valor, detalhe="", medidor=None, status=None):
    m = (f'<div class="medidor" aria-hidden="true"><span style="width:{max(0, min(100, medidor)):.0f}%"></span></div>'
         if medidor is not None else "")
    s = f'<div class="status {status[0]}">{_e(status[1])}</div>' if status else ""
    d = f'<div class="det">{detalhe}</div>' if detalhe else ""
    return f'<div class="kpi"><div class="rot">{_e(rotulo)}</div><div class="val">{valor}</div>{s}{d}{m}</div>'


def pagina(host):
    hoje = agora()
    dias = [(hoje - timedelta(days=DIAS_GRAFICO - 1 - i)).strftime("%Y-%m-%d") for i in range(DIAS_GRAFICO)]
    evts = eventos()
    contagem = {}
    for e in evts:
        if e["ferramenta"] in NOMES:
            c = contagem.setdefault(e["dia"], {})
            c[e["ferramenta"]] = c.get(e["ferramenta"], 0) + 1
    dia_hoje = dias[-1]
    usos_hoje = sum(contagem.get(dia_hoje, {}).values())
    total_30 = sum(sum(c.values()) for c in contagem.values())
    meta = situacao_meta()
    lista = garimpos(evts)

    if meta.get("hashtags_7d_usadas") is not None:
        n = meta["hashtags_7d_usadas"]
        k_hash = _kpi("Hashtags novas na semana", f"{n} de {HASHTAGS_SEMANA}",
                      "limite da Meta, janela de 7 dias", medidor=100 * n / HASHTAGS_SEMANA)
    else:
        k_hash = _kpi("Hashtags novas na semana", "—", _e(meta.get("erro") or "não deu para consultar agora"))
    dias_token = meta.get("dias_para_expirar")
    if meta.get("token_valido") is False:
        k_token = _kpi("Token da Meta", "Vencido", "renove: token novo no Vercel e publique de novo",
                       status=("critico", "✖ Garimpo parado"))
    elif dias_token is not None:
        estado = (("critico", "✖ Renove agora") if dias_token <= 3 else ("alerta", "▲ Renove esta semana")
                  if dias_token <= 10 else ("bom", "✔ Em dia"))
        k_token = _kpi("Token da Meta vence em", f"{dias_token} dias",
                       f"em {_data_br(meta.get('token_expira_em'))}", status=estado)
    else:
        k_token = _kpi("Token da Meta", "—", _e(meta.get("erro") or "sem data de validade informada"))
    uma_hora = hoje - timedelta(hours=1)
    meta_hora = sum(e["meta"] for e in evts
                    if datetime.strptime(e["dia"] + e["hora"], "%Y-%m-%d%H%M%S").replace(tzinfo=TZ) >= uma_hora)
    perto = meta_hora >= LIMITE_META_HORA * 0.75
    k_meta = _kpi("Chamadas à Meta na última hora", f"{_num(meta_hora)}",
                  f"limite aproximado de {LIMITE_META_HORA} por hora; ao passar, os garimpos param por até 1 hora",
                  medidor=100 * meta_hora / LIMITE_META_HORA,
                  status=("alerta", "▲ Perto do limite: espere antes de garimpar mais") if perto else None)
    lim = meta.get("limite") or {}
    pct = lim.get("uso_pct")
    visto = f" · verificado às {_data_br(lim['visto_em'], True)[-5:]}" if lim.get("visto_em") else ""
    uso_txt = f" · uso do limite: {pct}%" if pct is not None else ""
    if lim.get("bloqueado"):
        quando = (f"volta em cerca de {lim['voltar_min']} min" if lim.get("voltar_min")
                  else "a Meta não informa a hora; tente de novo em 15 a 30 min, uma vez só")
        k_sit = _kpi("Situação da Meta", "Bloqueada", quando + uso_txt + visto, medidor=pct,
                     status=("critico", "✖ Garimpos parados"))
    elif lim:
        perto_lim = pct is not None and pct >= 75
        k_sit = _kpi("Situação da Meta", "Liberada", (f"uso do limite: {pct}%" if pct is not None
                                                     else "uso do limite não informado") + visto,
                     medidor=pct, status=("alerta", "▲ Perto do limite") if perto_lim else ("bom", "✔ Pode garimpar"))
    else:
        k_sit = _kpi("Situação da Meta", "—", _e(meta.get("erro") or "ainda não verificada"))
    kpis = (_kpi("Usos hoje", _num(usos_hoje), f"{_num(total_30)} nos últimos {DIAS_GRAFICO} dias")
            + k_sit + k_meta + k_hash + k_token)
    aviso = f'<p class="aviso">{_e(meta["aviso"])}</p>' if meta.get("aviso") else ""
    ultima = (f" (última consulta: {_e(_data_br(meta['consultado_em'], True))})"
              if meta.get("consultado_em") else "")

    legenda = "".join(f'<li><i class="s{i + 1}"></i>{_e(nome)}</li>' for i, (_, nome) in enumerate(SERIES))
    linhas = "".join(
        "<tr><td>%s</td>%s<td>%d</td></tr>" % (
            f"{d[8:10]}/{d[5:7]}", "".join(f"<td>{contagem.get(d, {}).get(f, 0)}</td>" for f, _ in SERIES),
            sum(contagem.get(d, {}).values()))
        for d in reversed(dias) if contagem.get(d))
    tabela = (f'<details><summary>Ver em tabela</summary><div class="tabela"><table><thead><tr><th>Dia</th>'
              + "".join(f"<th>{_e(n)}</th>" for _, n in SERIES) + f"<th>Total</th></tr></thead><tbody>{linhas}"
              "</tbody></table></div></details>") if linhas else ""
    grafico = _grafico(dias, contagem) if total_30 else '<p class="vazio">Nenhum uso registrado ainda.</p>'

    itens = []
    for g in lista:
        perfis = ", ".join("@" + p for p in g["perfis"]) or "—"
        tags = ", ".join("#" + t for t in g["hashtags"])
        paineis = "".join(
            f'<a href="https://{_e(host)}/painel/{_e(p["id"])}" target="_blank" rel="noopener noreferrer">'
            f'Painel de {_e(_data_br(p["quando"], True))}</a>' for p in reversed(g["paineis"]))
        itens.append(
            '<div class="garimpo">'
            f'<h3>{_e(g["tema"] or "Garimpo sem tema")}</h3>'
            f'<div class="meta">{_e(_data_br(g["inicio"], True))} · {_num(g["referencias"])} referências</div>'
            f'<p>Perfis: {_e(perfis)}</p>' + (f'<p>Hashtags: {_e(tags)}</p>' if tags else "")
            + (f'<div class="paineis">{paineis}</div>' if paineis else '<p class="vazio">Sem painel publicado</p>')
            + "</div>")
    garimpos_html = "".join(itens) or '<p class="vazio">Nenhum garimpo registrado ainda.</p>'

    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Uso do conector</title>
<meta name="robots" content="noindex,nofollow"><style>{CSS}</style></head><body><main>
<h1>Uso do garimpo de Instagram</h1>
<p class="sub">Garimpo de referências · últimos {DIAS_GRAFICO} dias · horário de Brasília</p>
<section class="kpis">{kpis}</section>{aviso}
<section class="cartao"><h2>Usos por dia</h2><ul class="legenda">{legenda}</ul>{grafico}{tabela}</section>
<section class="cartao"><h2>Garimpos e painéis</h2>{garimpos_html}</section>
<footer>Atualizado em {hoje:%d/%m/%Y às %H:%M}. Token e hashtags vêm da Meta e são atualizados no máximo a cada
{CACHE_META_MIN} min{ultima}.</footer>
</main></body></html>""".encode("utf-8")
