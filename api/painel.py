#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""painel.py - gera o painel HTML autocontido do garimpo de referencias do Instagram.

Uso:
    python painel.py <pasta-do-garimpo>        # imprime PAINEL=<caminho do painel.html>

Como biblioteca:
    from painel import gerar_painel
    caminho = gerar_painel(pasta)

So biblioteca padrao (Python 3.9+). Tudo que vem de referencias.json / analise.json e
tratado como NAO CONFIAVEL: todo texto passa por html.escape, paleta so aceita hex estrito
e links so aceitam http(s) em instagram.com.
"""
import base64
import datetime
import html
import json
import math
import os
import re
import sys
from urllib.parse import urlparse

LIMITE_BYTES = 12 * 1024 * 1024
MAX_SLIDES_DESTAQUE = 5
MAX_BYTES_IMAGEM = 3 * 1024 * 1024

FORMATOS = {"carrossel": "Carrossel", "reels": "Reels", "imagem": "Imagem", "video": "Vídeo"}
ORIGENS_BUSCA = {"perfil": "Perfil", "hashtag": "Hashtag", "link": "Busca na web"}
HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
DATA_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
        ".gif": "image/gif"}


# ----------------------------------------------------------------- utilitarios de dados
def _d(v):
    return v if isinstance(v, dict) else {}


def _l(v):
    return v if isinstance(v, list) else []


def _s(v):
    """Texto limpo ou '' (None, dict, lista e bool viram vazio)."""
    if v is None or isinstance(v, (dict, list, bool)):
        return ""
    return str(v).strip()


def _n(v):
    """Numero finito ou None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v if math.isfinite(v) else None
    try:
        f = float(str(v).strip().replace(",", "."))
        return f if math.isfinite(f) else None
    except ValueError:
        return None


def _e(v):
    return html.escape(_s(v), quote=True)


def fmt_int(v):
    n = _n(v)
    if n is None:
        return "—"
    return "{:,}".format(int(round(n))).replace(",", ".")


def fmt_pct(ratio):
    n = _n(ratio)
    if n is None:
        return "—"
    p = n * 100
    casas = 2 if abs(p) < 1 else 1
    return ("{:.%df}" % casas).format(p).replace(".", ",") + "%"


def fmt_x(v):
    n = _n(v)
    if n is None:
        return "—"
    return "{:.1f}".format(n).replace(".", ",") + "×"


def fmt_data(v):
    m = DATA_RE.match(_s(v))
    if not m:
        return ""
    return "%s/%s/%s" % (m.group(3), m.group(2), m.group(1))


def data_num(v):
    m = DATA_RE.match(_s(v))
    return int(m.group(1) + m.group(2) + m.group(3)) if m else -1


LINK_RE = re.compile(r"https://(?:www\.)?instagram\.com/(?:p|reel|tv)/[A-Za-z0-9_-]{1,64}/?")
IMG_RE = re.compile(r"midia/(?:mini/)?[A-Za-z0-9_.-]+")


def link_seguro(v):
    """Devolve a URL apenas se for um post/reel/tv em https://instagram.com (regex estrita)."""
    url = _s(v)
    return url if LINK_RE.fullmatch(url) else ""


def id_seguro(v):
    return re.sub(r"[^A-Za-z0-9_-]", "_", _s(v))[:80]


def nota_int(v):
    n = _n(v)
    return 0 if n is None else max(0, min(100, int(round(n))))


def nota_opt(v):
    """Nota 0-100 ou None (sem nota)."""
    n = _n(v)
    return None if n is None else max(0, min(100, int(round(n))))


def _chave_nota(r):
    n = nota_opt(r.get("nota"))
    return (n is None, -(n or 0), -(_n(r.get("interacoes")) or 0))


def _carregar_json(caminho, obrigatorio):
    if not os.path.isfile(caminho):
        if obrigatorio:
            raise FileNotFoundError("não encontrei %s" % caminho)
        return {}
    try:
        with open(caminho, "r", encoding="utf-8-sig") as f:
            dados = json.load(f)
    except (OSError, ValueError) as exc:
        if obrigatorio:
            raise ValueError("não consegui ler %s: %s" % (caminho, exc))
        return {}
    return dados if isinstance(dados, dict) else {}


def _carregar_analise(caminho):
    """(dados, erro). erro e texto quando o arquivo existe mas nao da para usar."""
    if not os.path.isfile(caminho):
        return {}, None
    try:
        with open(caminho, "r", encoding="utf-8-sig") as f:
            dados = json.load(f)
    except (OSError, ValueError) as exc:
        return {}, " ".join(str(exc).split())[:140] or exc.__class__.__name__
    if not isinstance(dados, dict):
        return {}, "o conteúdo precisa ser um objeto JSON"
    return dados, None


# ----------------------------------------------------------------- imagens
class _Imagens(object):
    def __init__(self, pasta):
        self.base = os.path.realpath(pasta)
        self.cache = {}

    def uri(self, rel):
        rel = _s(rel).replace("\\", "/")
        if not rel or ".." in rel or not IMG_RE.fullmatch(rel):
            return ""
        if rel in self.cache:
            return self.cache[rel]
        uri = ""
        try:
            caminho = os.path.realpath(os.path.join(self.base, rel))
            ext = os.path.splitext(caminho)[1].lower()
            dentro = caminho.startswith(self.base + os.sep)
            if dentro and ext in MIME and os.path.isfile(caminho) \
                    and os.path.getsize(caminho) <= MAX_BYTES_IMAGEM:
                with open(caminho, "rb") as f:
                    uri = "data:%s;base64,%s" % (MIME[ext], base64.b64encode(f.read()).decode("ascii"))
        except (OSError, ValueError):
            uri = ""
        self.cache[rel] = uri
        return uri


# ----------------------------------------------------------------- markdown minimo
def _inline(t):
    t = html.escape(t, quote=True)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)


def _md(texto):
    linhas = _s(texto).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out, par, lista = [], [], []

    def fecha_par():
        if par:
            out.append("<p>%s</p>" % "<br>".join(_inline(x) for x in par))
            del par[:]

    def fecha_lista():
        if lista:
            out.append("<ul>%s</ul>" % "".join("<li>%s</li>" % _inline(x) for x in lista))
            del lista[:]

    for ln in linhas:
        ln = ln.rstrip()
        m_h = re.match(r"^\s*(#{1,6})\s+(.*)$", ln)
        m_b = re.match(r"^\s*[-*•]\s+(.*)$", ln)
        if not ln.strip():
            fecha_par()
            fecha_lista()
        elif m_h:
            fecha_par()
            fecha_lista()
            nivel = "h3" if len(m_h.group(1)) <= 2 else "h4"
            out.append("<%s>%s</%s>" % (nivel, _inline(m_h.group(2)), nivel))
        elif m_b:
            fecha_par()
            lista.append(m_b.group(1))
        else:
            fecha_lista()
            par.append(ln.strip())
    fecha_par()
    fecha_lista()
    return "\n".join(out)


def _paras(texto):
    blocos = [b for b in re.split(r"\n\s*\n", _s(texto).replace("\r\n", "\n")) if b.strip()]
    return "".join("<p>%s</p>" % "<br>".join(html.escape(x.strip(), quote=True)
                                              for x in b.split("\n")) for b in blocos)


# ----------------------------------------------------------------- pedacos de HTML
def _img(imagens, rel, alt, permitido=True, cls=""):
    uri = imagens.uri(rel) if permitido else ""
    c = (' class="%s"' % cls) if cls else ""
    if uri:
        return '<img%s src="%s" alt="%s" loading="lazy" decoding="async">' % (c, uri, html.escape(alt, quote=True))
    return '<div class="ph%s" role="img" aria-label="%s"><span>sem imagem</span></div>' % (
        (" " + cls) if cls else "", html.escape(alt + " (imagem indisponível)", quote=True))


def _badge_formato(r):
    f = _s(r.get("formato")).lower()
    if not f:
        return ""
    return '<span class="badge">%s</span>' % html.escape(FORMATOS.get(f, f.capitalize()), quote=True)


def _perfil_txt(r):
    p = _s(r.get("perfil")).lstrip("@")
    return "@" + p if p else ""


def _chip(rotulo, valor, extra=""):
    return '<li class="chip%s"><span>%s</span><b>%s</b></li>' % (extra, html.escape(rotulo), valor)


def _chips_metricas(r, completo):
    cur, com = _n(r.get("curtidas")), _n(r.get("comentarios"))
    chips = []
    if cur is None and com is not None:
        chips.append(_chip("Curtidas", "ocultas", " muted"))
    else:
        chips.append(_chip("Curtidas", fmt_int(cur)))
    chips.append(_chip("Comentários", fmt_int(com)))
    if _n(r.get("visualizacoes")) is not None or (completo and _s(r.get("formato")) in ("reels", "video")):
        chips.append(_chip("Visualizações", fmt_int(r.get("visualizacoes"))))
    chips.append(_chip("Engajamento", fmt_pct(r.get("taxa_engajamento"))))
    am = _n(r.get("acima_da_media"))
    if am is not None:
        txt = ("%s acima da média do perfil" % fmt_x(am)) if am >= 1 else ("%s da média do perfil" % fmt_x(am))
        chips.append('<li class="chip hl"><b>%s</b></li>' % html.escape(txt))
    return '<ul class="chips" aria-label="Métricas">%s</ul>' % "".join(chips)


def _nota_html(r):
    n = nota_opt(r.get("nota"))
    if n is None:
        return ('<div class="nota sem" role="img" aria-label="Sem nota (curtidas ocultas)"><strong>—</strong>'
                '<small>sem nota (curtidas ocultas)</small></div>')
    return ('<div class="nota" role="img" aria-label="Nota %d de 100"><strong>%d</strong><small>/100</small>'
            '<i style="width:%d%%"></i></div>' % (n, n, n))


def _campo(rotulo, valor_html):
    if not valor_html:
        return ""
    return "<div><dt>%s</dt><dd>%s</dd></div>" % (html.escape(rotulo), valor_html)


def _paleta(visual):
    nomes = []
    for c in _l(visual.get("paleta")):
        c = _s(c)
        if HEX_RE.match(c):
            nomes.append(c)
    if not nomes:
        return ""
    itens = "".join(
        '<li><span class="sw" style="background:%s"></span><code>%s</code></li>' % (c, c.upper())
        for c in nomes[:12])
    return '<ul class="paleta" aria-label="Paleta de cores">%s</ul>' % itens


def _card_destaque(r, a, imagens, nivel):
    rid = id_seguro(r.get("id"))
    perfil = _perfil_txt(r)
    quem = perfil or "Perfil não informado"
    minis = [m for m in _l(r.get("mini")) if _s(m)]
    capa = _img(imagens, minis[0] if minis else "", "Capa do post de %s" % quem, nivel < 3, "capa-img")
    strip = ""
    if nivel < 1 and len(minis) > 1:
        itens = "".join(
            "<li>%s</li>" % _img(imagens, m, "Slide %d do post de %s" % (i + 2, quem))
            for i, m in enumerate(minis[1:1 + MAX_SLIDES_DESTAQUE]))
        strip = '<ul class="strip" aria-label="Slides">%s</ul>' % itens

    visual = _d(a.get("visual"))
    vis_campos = "".join([
        _campo("Tipografia", _e(visual.get("tipografia"))),
        _campo("Layout", _e(visual.get("layout"))),
        _campo("Densidade de texto", _e(visual.get("densidade_texto"))),
    ])
    paleta = _paleta(visual)
    vis_html = ""
    if paleta or vis_campos:
        vis_html = '<section class="visual"><h4>Visual</h4>%s<dl class="campos">%s</dl></section>' % (paleta, vis_campos)

    gancho = _e(a.get("gancho"))
    campos = "".join([
        _campo("Tema", _e(a.get("tema"))),
        _campo("Formato de conteúdo", _e(a.get("formato_conteudo"))),
        _campo("Estrutura", _e(a.get("estrutura"))),
        _campo("CTA", _e(a.get("cta"))),
    ])
    duo = ""
    pq, ca = _paras(a.get("por_que_funciona")), _paras(a.get("como_adaptar"))
    if pq:
        duo += '<section class="why"><h4>Por que funciona</h4>%s</section>' % pq
    if ca:
        duo += '<section class="how"><h4>Como adaptar</h4>%s</section>' % ca
    tags = [t for t in (_s(x) for x in _l(a.get("tags"))) if t]
    tags_html = ('<ul class="tags" aria-label="Tags">%s</ul>' %
                 "".join("<li>%s</li>" % html.escape(t, quote=True) for t in tags[:20])) if tags else ""
    legenda = _s(r.get("legenda"))
    leg_html = ('<details class="legenda"><summary>Legenda original</summary><p>%s</p></details>'
                % html.escape(legenda, quote=True)) if legenda else ""
    # Fala transcrita do reels (opcional, vinda da análise): fica fechada para não alongar o card
    fala = _s(a.get("transcricao"))
    fala_html = ('<details class="legenda fala"><summary>Fala do reels</summary>%s</details>'
                 % _paras(fala)) if fala else ""
    leg_html = fala_html + leg_html
    url = link_seguro(r.get("link"))
    abrir = ('<a class="abrir" href="%s" target="_blank" rel="noopener noreferrer">Abrir no Instagram</a>'
             % html.escape(url, quote=True)) if url else ""
    data = fmt_data(r.get("data"))
    data_html = '<time>%s</time>' % data if data else ""
    objetivos = [o for o in (_s(x) for x in _l(a.get("objetivos"))) if o][:7]
    obj_html = ('<ul class="obj" aria-label="Objetivos">%s</ul>'
                % "".join("<li>%s</li>" % html.escape(o, quote=True) for o in objetivos)) if objetivos else ""
    if a.get("replicavel") is False:
        obj_html += '<p class="nao-rep">Não é um formato replicável: serve como tema em alta.</p>'

    return (
        '<article class="dest" id="ref-%s">'
        '<div class="dest-media"><figure class="capa">%s</figure>%s</div>'
        '<div class="dest-body">'
        '<header class="dest-head"><div class="quem"><span class="perfil">%s</span>%s%s</div>%s</header>'
        '%s%s%s<dl class="campos">%s</dl>%s%s%s%s<div class="rodape">%s</div>'
        '</div></article>'
    ) % (
        rid, capa, strip,
        html.escape(quem, quote=True), _badge_formato(r), data_html, _nota_html(r),
        ('<p class="gancho">%s</p>' % gancho) if gancho else "",
        _chips_metricas(r, True),
        obj_html, campos, duo, vis_html, tags_html, leg_html, abrir,
    )


def _item_grade(r, destaque, imagens, nivel):
    perfil = _perfil_txt(r)
    quem = perfil or "Perfil não informado"
    minis = [m for m in _l(r.get("mini")) if _s(m)]
    thumb = _img(imagens, minis[0] if minis else "", "Capa do post de %s" % quem, nivel < 2, "thumb-img")
    cur, com = _n(r.get("curtidas")), _n(r.get("comentarios"))
    inter = _n(r.get("interacoes"))
    taxa = _n(r.get("taxa_engajamento"))
    am = _n(r.get("acima_da_media"))
    origens = [o for o in (_s(x).replace("|", "/") for x in _l(r.get("origens"))) if o]
    nota = nota_opt(r.get("nota"))
    oculta = (cur is None and com is not None)
    inter_txt = fmt_int(inter) + (" <em>(curtidas ocultas)</em>" if oculta else "")
    data = fmt_data(r.get("data"))
    url = link_seguro(r.get("link"))
    abrir = ('<a href="%s" target="_blank" rel="noopener noreferrer">Abrir no Instagram</a>'
             % html.escape(url, quote=True)) if url else ""
    legenda = " ".join(_s(r.get("legenda")).split())
    if len(legenda) > 140:
        legenda = legenda[:139].rstrip() + "…"
    dest_tag = '<span class="badge dest-tag">Destaque</span>' if destaque else ""
    am_txt = fmt_x(am) if am is not None else "—"
    return (
        '<li class="item" data-origens="%s" data-formato="%s" data-destaque="%d" '
        'data-nota="%d" data-inter="%s" data-taxa="%s" data-data="%d">'
        '<div class="thumb">%s<span class="nota-mini" aria-label="%s" title="%s">%s</span></div>'
        '<div class="ib"><p class="l1"><b>%s</b>%s%s</p>'
        '%s'
        '<dl class="mini-dl"><div><dt>Interações</dt><dd>%s</dd></div>'
        '<div><dt>Engajamento</dt><dd>%s</dd></div>'
        '<div><dt>vs. média</dt><dd>%s</dd></div>'
        '<div><dt>Data</dt><dd>%s</dd></div></dl>'
        '%s<p class="orig">%s</p><p class="lk">%s</p></div></li>'
    ) % (
        html.escape("|".join(origens), quote=True), html.escape(_s(r.get("formato")).lower(), quote=True),
        1 if destaque else 0, nota if nota is not None else -1,
        int(inter) if inter is not None else -1,
        ("%.6f" % taxa) if taxa is not None else "-1", data_num(r.get("data")),
        thumb, ("Nota %d de 100" % nota) if nota is not None else "Sem nota (curtidas ocultas)",
        ("Nota %d de 100" % nota) if nota is not None else "Sem nota (curtidas ocultas)",
        str(nota) if nota is not None else "—",
        html.escape(quem, quote=True), _badge_formato(r), dest_tag,
        ('<p class="leg">%s</p>' % html.escape(legenda, quote=True)) if legenda else "",
        inter_txt, fmt_pct(taxa), am_txt, data or "—",
        ('<p class="sem-nota">sem nota (curtidas ocultas)</p>' if nota is None else ""),
        html.escape(" · ".join(origens), quote=True), abrir,
    )


# ----------------------------------------------------------------- pagina
CSS = r"""
:root {
  /* Layout: mural editorial. Cabeçalho calmo, destaques em cartões largos (imagem à esquerda, texto à direita), grade compacta de candidatos. */
  --bg: #EEF1EE;
  --surface: #FFFFFF;
  --surface-2: #E4E9E5;
  --fg: #17201C;
  --muted: #4F5C56;
  --line: #CDD5D0;
  --accent: #0A6A57;
  --accent-soft: #D5EBE4;
  --on-accent: #FFFFFF;
  --warn: #8A4A00;
  --ph: #D9DFDB;
  --font-display: "Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua", Georgia, serif;
  --font-body: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --font-mono: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #101513; --surface: #181F1C; --surface-2: #212A26; --fg: #E6ECE8; --muted: #A2B0A9;
    --line: #2D3833; --accent: #6FD3B8; --accent-soft: #1C3A32; --on-accent: #08130F; --warn: #F0B15C;
    --ph: #26302C; color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #101513; --surface: #181F1C; --surface-2: #212A26; --fg: #E6ECE8; --muted: #A2B0A9;
  --line: #2D3833; --accent: #6FD3B8; --accent-soft: #1C3A32; --on-accent: #08130F; --warn: #F0B15C;
  --ph: #26302C; color-scheme: dark;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font-family: var(--font-body); font-size: 15px; line-height: 1.55;
  padding-block: 28px 64px;
  padding-left: max(16px, env(safe-area-inset-left, 0px));
  padding-right: max(16px, env(safe-area-inset-right, 0px));
}
[hidden] { display: none !important; }
.wrap { max-width: 1180px; margin: 0 auto; display: flex; flex-direction: column; gap: 56px; }
img { max-width: 100%; display: block; }
a { color: var(--accent); }
a:focus-visible, summary:focus-visible, select:focus-visible, input:focus-visible {
  outline: 3px solid var(--accent); outline-offset: 2px; border-radius: 4px;
}
h1, h2, h3, h4 { text-wrap: balance; margin: 0; }
h2 { font-family: var(--font-display); font-weight: 600; font-size: clamp(1.4rem, 3.4vw, 1.9rem); letter-spacing: -0.01em; }
h4 { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.09em; color: var(--muted); font-weight: 700; margin-bottom: 6px; }
p { margin: 0; }
code { font-family: var(--font-mono); font-size: 0.85em; }
.sec-head { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 6px 16px; margin-bottom: 18px; }
.sec-head p { color: var(--muted); font-size: 0.9rem; }

/* cabeçalho */
.topo { display: flex; flex-direction: column; gap: 18px; padding-bottom: 28px; border-bottom: 1px solid var(--line); }
.eyebrow { font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.14em; color: var(--accent); font-weight: 700; }
h1 { font-family: var(--font-display); font-weight: 600; font-size: clamp(1.9rem, 6vw, 3.3rem); line-height: 1.08; letter-spacing: -0.02em; overflow-wrap: anywhere; }
.data-ger { color: var(--muted); font-size: 0.9rem; }
.contagens { display: flex; flex-wrap: wrap; gap: 8px 28px; margin: 0; padding: 0; list-style: none; }
.contagens li { display: flex; align-items: baseline; gap: 7px; }
.contagens b { font-family: var(--font-display); font-size: 1.5rem; font-weight: 600; font-variant-numeric: tabular-nums; }
.contagens span { color: var(--muted); font-size: 0.85rem; }
.aviso { color: var(--warn); font-size: 0.85rem; }
.buscas { font-size: 0.85rem; color: var(--muted); }
.buscas summary { cursor: pointer; font-weight: 600; color: var(--fg); width: fit-content; }
.buscas ul { list-style: none; margin: 10px 0 0; padding: 0; display: flex; flex-direction: column; gap: 5px; }
.buscas li { min-width: 0; overflow-wrap: anywhere; }
.buscas .tipo { text-transform: uppercase; letter-spacing: 0.08em; font-size: 0.68rem; font-weight: 700; color: var(--accent); margin-right: 6px; }
.buscas .erro { color: var(--warn); }

/* padrões */
.padroes { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: clamp(18px, 3vw, 32px); }
.md { max-width: 74ch; display: flex; flex-direction: column; gap: 10px; overflow-wrap: anywhere; }
.md h3 { font-family: var(--font-display); font-size: 1.15rem; margin-top: 8px; }
.md h4 { margin-top: 6px; }
.md ul { margin: 0; padding-left: 1.2em; display: flex; flex-direction: column; gap: 4px; }

/* destaques */
.destaques { display: flex; flex-direction: column; gap: 28px; }
.dest { display: grid; grid-template-columns: minmax(0, 1fr); gap: 20px; background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: clamp(14px, 2.5vw, 26px); }
@media (min-width: 860px) { .capa { max-width: none; } .dest { grid-template-columns: minmax(0, 5fr) minmax(0, 7fr); gap: 32px; } .dest-media { position: sticky; top: 16px; align-self: start; } }
.dest-media { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.capa { margin: 0; aspect-ratio: 4 / 5; width: 100%; max-width: 380px; border-radius: 8px; overflow: hidden; background: var(--ph); }
.capa img, .capa .ph { width: 100%; height: 100%; object-fit: cover; }
.strip { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(64px, 1fr)); gap: 8px; }
.strip li { aspect-ratio: 4 / 5; border-radius: 5px; overflow: hidden; background: var(--ph); }
.strip img, .strip .ph { width: 100%; height: 100%; object-fit: cover; }
.ph { display: grid; place-items: center; background: var(--ph); color: var(--muted); font-size: 0.72rem; text-align: center; padding: 4px; min-height: 100%; }
.dest-body { display: flex; flex-direction: column; gap: 18px; min-width: 0; }
.dest-head { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px 16px; }
.quem { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; min-width: 0; }
.perfil { font-weight: 700; font-size: 1.05rem; overflow-wrap: anywhere; }
time { color: var(--muted); font-size: 0.85rem; font-variant-numeric: tabular-nums; }
.badge { display: inline-block; font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.09em; font-weight: 700; padding: 3px 9px; border-radius: 999px; background: var(--surface-2); color: var(--fg); }
.dest-tag { background: var(--accent-soft); color: var(--accent); }
.nota { position: relative; display: flex; align-items: baseline; gap: 2px; padding-bottom: 7px; min-width: 76px; }
.nota strong { font-family: var(--font-display); font-size: 1.9rem; line-height: 1; font-variant-numeric: tabular-nums; }
.nota small { color: var(--muted); font-size: 0.8rem; }
.nota i { position: absolute; left: 0; bottom: 0; height: 4px; border-radius: 4px; background: var(--accent); }
.nota::before { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 4px; border-radius: 4px; background: var(--surface-2); }
.nota i { z-index: 1; }
.nota.sem { gap: 8px; padding-bottom: 0; }
.nota.sem::before { display: none; }
.nota.sem small { font-size: 0.74rem; max-width: 24ch; line-height: 1.2; }
.sem-nota { color: var(--muted); font-size: 0.78rem; font-style: italic; }
.gancho { font-family: var(--font-display); font-size: clamp(1.3rem, 3vw, 1.75rem); line-height: 1.25; font-weight: 600; letter-spacing: -0.01em; overflow-wrap: anywhere; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; list-style: none; margin: 0; padding: 0; }
.chip { display: inline-flex; gap: 6px; align-items: baseline; padding: 4px 10px; border: 1px solid var(--line); border-radius: 999px; font-size: 0.82rem; }
.chip span { color: var(--muted); }
.chip b { font-variant-numeric: tabular-nums; font-weight: 700; }
.chip.muted b { font-weight: 500; font-style: italic; }
.chip.hl { background: var(--accent-soft); border-color: transparent; color: var(--accent); }
.campos { margin: 0; display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 210px), 1fr)); gap: 12px 20px; }
.campos > div { min-width: 0; }
.campos dt { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.09em; color: var(--muted); font-weight: 700; }
.campos dd { margin: 2px 0 0; overflow-wrap: anywhere; }
.duo { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 250px), 1fr)); gap: 16px; }
.duo section { background: var(--surface-2); border-radius: 10px; padding: 14px 16px; min-width: 0; overflow-wrap: anywhere; display: flex; flex-direction: column; gap: 6px; }
.duo h4 { margin: 0; color: var(--accent); }
.visual { display: flex; flex-direction: column; gap: 10px; border-top: 1px solid var(--line); padding-top: 16px; min-width: 0; }
.paleta { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 8px 14px; }
.paleta li { display: flex; align-items: center; gap: 7px; }
.sw { width: 34px; height: 34px; border-radius: 8px; border: 1px solid var(--line); box-shadow: inset 0 0 0 1px rgba(128,128,128,.25); flex: none; }
.tags { display: flex; flex-wrap: wrap; gap: 6px; list-style: none; margin: 0; padding: 0; }
.tags li { font-size: 0.8rem; color: var(--muted); }
.tags li::before { content: "#"; }
.legenda { font-size: 0.85rem; color: var(--muted); }
.legenda summary { cursor: pointer; width: fit-content; font-weight: 600; }
.legenda p { margin-top: 8px; white-space: pre-wrap; overflow-wrap: anywhere; }
.abrir { display: inline-block; background: var(--accent); color: var(--on-accent); text-decoration: none; font-weight: 700; padding: 9px 16px; border-radius: 8px; }
.abrir:hover { filter: brightness(1.08); }
.rodape { display: flex; }
.obj { display: flex; flex-wrap: wrap; gap: 6px; list-style: none; margin: 0; padding: 0; }
.obj li { font-size: 0.75rem; padding: 3px 10px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); font-weight: 600; }
.nao-rep { margin: 0; font-size: 0.85rem; color: var(--warn); font-weight: 600; }

/* catálogo de formatos */
.catalogo { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 260px), 1fr)); gap: 14px; }
.formato { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 16px 18px; display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.formato h3 { margin: 0; font-family: var(--font-display); font-size: 1.1rem; line-height: 1.25; overflow-wrap: anywhere; }
.formato .qtd { margin: 0; color: var(--muted); font-size: 0.82rem; }
.formato .ex { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 0.85rem; }
.formato .ex a { color: var(--accent); overflow-wrap: anywhere; }
.formato.tema { border-style: dashed; }

/* candidatos */
.controles { display: flex; flex-wrap: wrap; align-items: end; gap: 12px 16px; margin-bottom: 18px; }
.controles label { display: flex; flex-direction: column; gap: 3px; font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 700; color: var(--muted); min-width: 0; }
.controles select { font: inherit; font-size: 0.9rem; text-transform: none; letter-spacing: 0; font-weight: 400; background: var(--surface); color: var(--fg); border: 1px solid var(--line); border-radius: 8px; padding: 7px 10px; max-width: 100%; }
.controles .ck { flex-direction: row; align-items: center; gap: 8px; text-transform: none; letter-spacing: 0; font-size: 0.9rem; color: var(--fg); font-weight: 400; padding: 8px 0; }
.controles .ck input { width: 18px; height: 18px; accent-color: var(--accent); }
.contagem-vis { margin-left: auto; color: var(--muted); font-size: 0.85rem; padding-bottom: 8px; }
.grade { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 230px), 1fr)); gap: 16px; }
.item { display: flex; flex-direction: column; background: var(--surface); border: 1px solid var(--line); border-radius: 12px; overflow: hidden; min-width: 0; }
.thumb { position: relative; aspect-ratio: 4 / 5; background: var(--ph); }
.thumb img, .thumb .ph { width: 100%; height: 100%; object-fit: cover; }
.nota-mini { position: absolute; top: 8px; right: 8px; background: var(--surface); color: var(--fg); font-weight: 700; font-size: 0.85rem; min-width: 34px; text-align: center; padding: 3px 7px; border-radius: 999px; font-variant-numeric: tabular-nums; border: 1px solid var(--line); }
.ib { padding: 12px 14px 14px; display: flex; flex-direction: column; gap: 8px; min-width: 0; flex: 1; }
.l1 { display: flex; flex-wrap: wrap; gap: 5px 8px; align-items: center; overflow-wrap: anywhere; }
.leg { color: var(--muted); font-size: 0.82rem; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; overflow-wrap: anywhere; }
.mini-dl { margin: 0; display: grid; grid-template-columns: 1fr 1fr; gap: 6px 10px; font-size: 0.82rem; }
.mini-dl dt { color: var(--muted); font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 700; }
.mini-dl dd { margin: 0; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.mini-dl em { color: var(--muted); font-size: 0.75rem; }
.orig { color: var(--muted); font-size: 0.78rem; overflow-wrap: anywhere; }
.lk { margin-top: auto; font-size: 0.85rem; font-weight: 600; }
.vazio { color: var(--muted); }
.rodape-pagina { color: var(--muted); font-size: 0.8rem; }
@media (prefers-reduced-motion: no-preference) {
  .dest, .item { transition: border-color .15s ease; }
  .dest:hover, .item:hover { border-color: var(--accent); }
}
"""

JS = r"""
(function () {
  var g = document.getElementById('grade');
  var ctl = document.getElementById('controles');
  if (!g || !ctl) return;
  var fo = document.getElementById('f-origem'), ff = document.getElementById('f-formato'),
      fd = document.getElementById('f-destaque'), fs = document.getElementById('f-ordem'),
      cv = document.getElementById('contagem-vis');
  var itens = [].slice.call(g.children);
  function aplicar() {
    var o = fo.value, f = ff.value, d = fd.checked, k = fs.value, vis = 0;
    itens.forEach(function (it) {
      var ok = (!o || it.getAttribute('data-origens').split('|').indexOf(o) > -1) &&
               (!f || it.getAttribute('data-formato') === f) &&
               (!d || it.getAttribute('data-destaque') === '1');
      it.hidden = !ok; if (ok) vis++;
    });
    itens.slice().sort(function (a, b) {
      return (parseFloat(b.getAttribute('data-' + k)) || 0) - (parseFloat(a.getAttribute('data-' + k)) || 0);
    }).forEach(function (it) { g.appendChild(it); });
    cv.textContent = vis + (vis === 1 ? ' referência' : ' referências');
  }
  [fo, ff, fd, fs].forEach(function (el) { el.addEventListener('change', aplicar); });
  ctl.hidden = false;
  aplicar();
})();
"""


ORDEM_OBJETIVOS = ["alcance", "conexão", "venda", "autoridade", "elevação de consciência", "quebra de objeção", "atração"]


def _catalogo(destaques, por_id, ana_refs):
    """Destaques agrupados por formato (com os objetivos); os não replicáveis vão para 'Temas em alta'."""
    grupos, ordem, temas = {}, [], []
    for rid in destaques:
        a = _d(ana_refs.get(rid))
        if a.get("replicavel") is False:
            temas.append(rid)
            continue
        nome = _s(a.get("formato_conteudo"))
        if not nome:
            continue
        chave = re.sub(r"\s+", " ", nome.lower())
        if chave not in grupos:
            grupos[chave] = {"nome": nome, "ids": [], "obj": []}
            ordem.append(chave)
        grupo = grupos[chave]
        grupo["ids"].append(rid)
        for o in _l(a.get("objetivos")):
            o = _s(o)
            if o and o not in grupo["obj"]:
                grupo["obj"].append(o)
    if not grupos and not temas:
        return ""
    ordem.sort(key=lambda k: -len(grupos[k]["ids"]))  # sort estável: mais exemplos primeiro

    def exemplos(ids):
        return "".join('<li><a href="#ref-%s">%s</a></li>' % (
            id_seguro(i), html.escape(_perfil_txt(por_id[i]) or "post", quote=True)) for i in ids)

    cards = []
    for k in ordem:
        grupo = grupos[k]
        objs = sorted(grupo["obj"], key=lambda o: ORDEM_OBJETIVOS.index(o) if o in ORDEM_OBJETIVOS else 99)
        obj_html = ('<ul class="obj">%s</ul>' % "".join("<li>%s</li>" % html.escape(o, quote=True) for o in objs[:7])
                    if objs else "")
        n = len(grupo["ids"])
        cards.append('<article class="formato"><h3>%s</h3><p class="qtd">%d %s</p>%s<ul class="ex">%s</ul></article>' % (
            _e(grupo["nome"]), n, "exemplo" if n == 1 else "exemplos", obj_html, exemplos(grupo["ids"])))
    if temas:
        cards.append('<article class="formato tema"><h3>Temas em alta</h3><p class="qtd">%d %s que não %s formato '
                     'replicável (corte de podcast, entrevista, repost), mas pode%s virar gancho</p>'
                     '<ul class="ex">%s</ul></article>' % (
                         len(temas), "post" if len(temas) == 1 else "posts", "é" if len(temas) == 1 else "são",
                         "" if len(temas) == 1 else "m", exemplos(temas)))
    return ('<section aria-labelledby="t-cat"><div class="sec-head"><h2 id="t-cat">Catálogo de formatos</h2>'
            '<p>Os destaques agrupados por formato, com o objetivo de cada um.</p></div>'
            '<div class="catalogo">%s</div></section>') % "".join(cards)


def _montar(tema, refs, perfis, buscas, analise, imagens, nivel, datahora, n_inc=0, analise_erro=None):
    ana_refs = _d(analise.get("referencias"))
    por_id = {}
    for r in refs:
        rid = _s(r.get("id"))
        if rid and rid not in por_id:
            por_id[rid] = r

    # destaques: ordem_destaques primeiro, depois os demais marcados por nota
    marcados = [rid for rid, a in ana_refs.items() if isinstance(a, dict) and a.get("destaque") is True and rid in por_id]
    ordem = []
    for rid in _l(analise.get("ordem_destaques")):
        rid = _s(rid)
        if rid in por_id and rid not in ordem and (rid in marcados or isinstance(ana_refs.get(rid), dict)):
            ordem.append(rid)
    resto = [rid for rid in marcados if rid not in ordem]
    resto.sort(key=lambda x: _chave_nota(por_id[x]) + (x,))
    destaques = ordem + resto
    dset = set(destaques)

    n_hash = sum(1 for b in buscas if _s(_d(b).get("origem")) == "hashtag")
    n_web = sum(1 for b in buscas if _s(_d(b).get("origem")) == "link")
    contagens = [(len(refs), "candidatos"), (len(destaques), "destaques"), (len(perfis), "perfis"),
                 (n_hash, "hashtags"), (n_web, "buscas na web")]
    cont_html = "".join("<li><b>%d</b><span>%s</span></li>" % (n, t) for n, t in contagens)

    buscas_li = []
    for b in buscas:
        b = _d(b)
        tipo = ORIGENS_BUSCA.get(_s(b.get("origem")), _s(b.get("origem")) or "Busca")
        partes = ['<span class="tipo">%s</span>%s' % (html.escape(tipo, quote=True), _e(b.get("valor")))]
        qtd = _n(b.get("qtd"))
        if qtd is not None:
            partes.append(" · %s %s" % (fmt_int(qtd), "item" if int(qtd) == 1 else "itens"))
        q = fmt_data(b.get("quando"))
        if q:
            partes.append(" · " + q)
        if _s(b.get("erro")):
            partes.append(' · <span class="erro">%s</span>' % _e(b.get("erro")))
        buscas_li.append("<li>%s</li>" % "".join(partes))
    buscas_html = ""
    n_buscas = len(buscas_li)
    if n_inc:
        buscas_li.append('<li><span class="erro">%d %s</span></li>' % (
            n_inc, "link não pôde ser lido" if n_inc == 1 else "links não puderam ser lidos"))
    if buscas_li:
        tem_erro = n_inc > 0 or any(_s(_d(b).get("erro")) for b in buscas)
        buscas_html = '<details class="buscas"%s><summary>Buscas feitas (%d)</summary><ul>%s</ul></details>' % (
            " open" if tem_erro else "", n_buscas, "".join(buscas_li))

    aviso = ""
    if analise_erro:
        aviso += '<p class="aviso">A análise (analise.json) não pôde ser lida e foi ignorada: %s. Corrija o arquivo e gere o painel de novo.</p>' % html.escape(analise_erro, quote=True)
    if nivel > 0:
        aviso += '<p class="aviso">Para o arquivo não ficar pesado demais, algumas miniaturas foram omitidas. Rode "midia" de novo com menos itens se precisar delas.</p>'

    resumo = _md(analise.get("resumo"))
    padroes = ""
    if resumo:
        padroes = ('<section aria-labelledby="t-padroes"><div class="sec-head"><h2 id="t-padroes">Padrões encontrados</h2></div>'
                   '<div class="padroes"><div class="md">%s</div></div></section>') % resumo

    dest_html = ""
    if destaques:
        cards = "".join(_card_destaque(por_id[rid], _d(ana_refs.get(rid)), imagens, nivel) for rid in destaques)
        dest_html = ('<section aria-labelledby="t-dest"><div class="sec-head"><h2 id="t-dest">Destaques</h2>'
                     '<p>Referências analisadas: por que funcionam e como adaptar.</p></div>'
                     '<div class="destaques">%s</div></section>') % cards
    elif analise:
        dest_html = ('<section aria-labelledby="t-dest"><div class="sec-head"><h2 id="t-dest">Destaques</h2></div>'
                     '<p class="vazio">Nenhuma referência marcada como destaque ainda.</p></section>')

    # grade de candidatos
    ordenadas = sorted(refs, key=_chave_nota)
    origens_unicas = []
    for r in ordenadas:
        for o in _l(r.get("origens")):
            o = _s(o).replace("|", "/")
            if o and o not in origens_unicas:
                origens_unicas.append(o)
    formatos_unicos = []
    for r in ordenadas:
        f = _s(r.get("formato")).lower()
        if f and f not in formatos_unicos:
            formatos_unicos.append(f)
    if ordenadas:
        itens = "".join(_item_grade(r, _s(r.get("id")) in dset, imagens, nivel) for r in ordenadas)
        opt_o = '<option value="">Todas</option>' + "".join(
            '<option value="%s">%s</option>' % (html.escape(o, quote=True), html.escape(o, quote=True)) for o in origens_unicas)
        opt_f = '<option value="">Todos</option>' + "".join(
            '<option value="%s">%s</option>' % (html.escape(f, quote=True), html.escape(FORMATOS.get(f, f.capitalize()), quote=True))
            for f in formatos_unicos)
        controles = (
            '<div class="controles" id="controles" role="group" aria-label="Filtros e ordenação" hidden>'
            '<label>Origem<select id="f-origem">%s</select></label>'
            '<label>Formato<select id="f-formato">%s</select></label>'
            '<label class="ck"><input type="checkbox" id="f-destaque">Só destaques</label>'
            '<label>Ordenar por<select id="f-ordem"><option value="nota">Nota</option>'
            '<option value="inter">Interações</option><option value="taxa">Engajamento</option>'
            '<option value="data">Data (mais recentes)</option></select></label>'
            '<span class="contagem-vis" id="contagem-vis" aria-live="polite"></span></div>') % (opt_o, opt_f)
        cand = ('<section aria-labelledby="t-cand"><div class="sec-head"><h2 id="t-cand">Todos os candidatos</h2>'
                '<p>%d referências coletadas, da maior nota para a menor.</p></div>%s'
                '<ul class="grade" id="grade">%s</ul></section>') % (len(ordenadas), controles, itens)
    else:
        cand = ('<section aria-labelledby="t-cand"><div class="sec-head"><h2 id="t-cand">Todos os candidatos</h2></div>'
                '<p class="vazio">Nenhuma referência coletada ainda. Rode perfis, hashtags ou links para começar.</p></section>')

    topo = (
        '<header class="topo"><p class="eyebrow">Garimpo de referências</p>'
        '<h1>%s</h1><p class="data-ger">%s</p>'
        '<ul class="contagens" aria-label="Resumo">%s</ul>%s%s</header>'
    ) % (_e(tema) or "Sem tema", html.escape(datahora, quote=True), cont_html, aviso, buscas_html)

    catalogo = _catalogo(destaques, por_id, ana_refs)
    corpo = '<main class="wrap">%s%s%s%s%s<p class="rodape-pagina">Gerado a partir de referencias.json e analise.json. As imagens são miniaturas guardadas na pasta do garimpo.</p></main>' % (
        topo, padroes, catalogo, dest_html, cand)
    return corpo


def gerar_html(pasta, artifact=False, avisos=None):
    """Devolve o HTML do painel como texto. artifact=True omite doctype/html/head/body e a meta CSP.
    Se `avisos` for uma lista, acrescenta avisos legiveis (pt-BR)."""
    pasta = os.path.abspath(pasta)
    ref = _carregar_json(os.path.join(pasta, "referencias.json"), True)
    analise, analise_erro = _carregar_analise(os.path.join(pasta, "analise.json"))
    todas = [r for r in _l(ref.get("referencias")) if isinstance(r, dict)]
    refs = [r for r in todas if r.get("incompleta") is not True]
    n_inc = len(todas) - len(refs)
    perfis = _d(ref.get("perfis"))
    buscas = [b for b in _l(ref.get("buscas")) if isinstance(b, dict)]
    tema = _s(ref.get("tema"))
    quando = fmt_data(ref.get("atualizado_em")) or fmt_data(ref.get("criado_em")) or datetime.date.today().strftime("%d/%m/%Y")
    datahora = "Atualizado em " + quando

    if isinstance(avisos, list):
        if analise_erro:
            avisos.append("analise.json inválido (%s): o painel saiu sem a análise — corrija e rode painel de novo" % analise_erro)
        conhecidos = set(_s(r.get("id")) for r in todas)
        faltam = []
        ids = [_s(x) for x in _l(analise.get("ordem_destaques"))] + [_s(k) for k in _d(analise.get("referencias"))]
        for i in ids:
            if i and i not in conhecidos and i not in faltam:
                faltam.append(i)
        if faltam:
            txt = ", ".join(faltam[:20]) + (" …" if len(faltam) > 20 else "")
            avisos.append("ids da análise que não estão no garimpo: " + txt)

    imagens = _Imagens(pasta)
    csp = ('<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; '
           'style-src \'unsafe-inline\'; script-src \'unsafe-inline\'">')
    cabeca = ('<meta charset="utf-8">%s<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
              '<meta name="claude:viewport-fit-sides" content="cover">'
              '<title>Garimpo de referências</title><style>%s</style>') % (csp, CSS)
    resultado = ""
    for nivel in range(0, 4):
        corpo = _montar(tema, refs, perfis, buscas, analise, imagens, nivel, datahora, n_inc, analise_erro)
        script = "<script>%s</script>" % JS
        if artifact:
            resultado = '<title>Garimpo de referências</title><meta name="claude:viewport-fit-sides" content="cover"><style>%s</style>%s%s' % (CSS, corpo, script)
        else:
            resultado = ('<!doctype html><html lang="pt-BR"><head>%s</head><body>%s%s</body></html>'
                         % (cabeca, corpo, script))
        if len(resultado.encode("utf-8")) <= LIMITE_BYTES:
            break
    return resultado


def gerar_painel(pasta, avisos=None):
    """Le referencias.json (+ analise.json) em `pasta` e escreve painel.html. Devolve o caminho.
    Se `avisos` for uma lista, problemas nao fatais sao acrescentados nela (texto pt-BR)."""
    pasta = os.path.abspath(pasta)
    conteudo = gerar_html(pasta, avisos=avisos)
    destino = os.path.join(pasta, "painel.html")
    with open(destino, "w", encoding="utf-8", newline="\n") as f:
        f.write(conteudo)
    return destino


def main(argv):
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if len(argv) != 2:
        print("uso: python painel.py <pasta-do-garimpo>", file=sys.stderr)
        return 2
    try:
        avisos = []
        print("PAINEL=" + gerar_painel(argv[1], avisos))
        for a in avisos:
            print("aviso: " + a, file=sys.stderr)
    except (OSError, ValueError) as exc:
        print("erro: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
