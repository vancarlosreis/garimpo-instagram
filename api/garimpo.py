#!/usr/bin/env python3
"""Garimpo de referências do Instagram (conteúdo + visual) — CLI da skill instagram-garimpo.

Coleta posts de perfis e hashtags pela API oficial da Meta (Instagram Graph API via Facebook Login)
e de links avulsos (achados na busca na web) via yt-dlp, sem baixar vídeo. Ranqueia por desempenho,
grava referencias.json + referencias.csv na pasta do garimpo, baixa as imagens e chama o painel.

  python garimpo.py configurar                         # a PESSOA roda no terminal dela (token via getpass)
  python garimpo.py status [--testar]
  python garimpo.py novo "clareamento dental" [--saida DIR]
  python garimpo.py perfis   --pasta P @perfil1 perfil2 [--limite 30] [--dias 90] [--formatos carrossel,reels]
  python garimpo.py hashtags --pasta P tag1 "#tag2" [--tipo top|recente] [--limite 50] [--dias 90]
  python garimpo.py links    --pasta P URL... [--arquivo links.txt]
  python garimpo.py midia    --pasta P [--top 20] [--slides 10] [--ids id1,id2]
  python garimpo.py painel   --pasta P

Todo comando termina com uma linha RESULTADO=<json>. Só biblioteca padrão; yt-dlp e ffmpeg são externos.
O token da Meta nunca é impresso (aparece como access_token=***).
"""
import argparse
import bisect
import csv
import getpass
import importlib.util
import inspect
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Graph API da Meta — tudo o que depende da documentação fica aqui em cima,
# para ser conferido e ajustado sem mexer no resto do código.
# ---------------------------------------------------------------------------
GRAPH_BASE = "https://graph.facebook.com"
GRAPH_VERSAO_PADRAO = "v26.0"  # sempre fixar: chamada sem versão usa o padrão do painel do app
GRAPH_TIMEOUT = 30  # segundos por chamada

# configurar: troca por token de longa duração (~60 dias) e descoberta das contas IG ligadas às Páginas
EP_TROCA_TOKEN = "oauth/access_token"
EP_CONTAS = "me/accounts"
CAMPOS_CONTAS = "name,instagram_business_account{id,username}"
LIMITE_PAGINA_CONTAS = 100

# status --testar
CAMPOS_TESTE = "id,username"
EP_DEBUG_TOKEN = "debug_token"                 # ?input_token=T&access_token=T → is_valid, expires_at, scopes
EP_HASHTAGS_RECENTES = "{ig_user_id}/recently_searched_hashtags"  # ?limit=30 → hashtags que contaram na semana
ESCOPOS_NECESSARIOS = ("pages_show_list", "pages_read_engagement", "instagram_basic", "instagram_manage_insights")

# perfis: Business Discovery chamado a partir da conta IG profissional da pessoa
CAMPOS_PERFIL = "username,name,biography,followers_count,media_count,profile_picture_url"
CAMPOS_MIDIA_PERFIL = ("id,caption,media_type,media_product_type,media_url,thumbnail_url,permalink,timestamp,"
                       "like_count,comments_count,view_count,children{media_type,media_url,thumbnail_url}")
# plano B (campos confirmados como públicos) quando a Meta recusa algum campo (código 100, "nonexisting field")
CAMPOS_PERFIL_MIN = "username,biography,followers_count,media_count"
CAMPOS_MIDIA_PERFIL_MIN = ("id,caption,media_type,media_url,thumbnail_url,permalink,timestamp,like_count,"
                           "comments_count,children{media_type,media_url}")
# {paginacao} vira ".limit(25)" ou ".after(CURSOR).limit(25)"
MODELO_BUSINESS_DISCOVERY = ("business_discovery.username({usuario})"
                             "{{{campos_perfil},media{paginacao}{{{campos_midia}}}}}")
LIMITE_PAGINA_PERFIL = 25
LIMITE_PAGINA_PERFIL_MIN = 5  # com "reduce the amount of data" a página cai pela metade até aqui

# hashtags: busca do id e mídias top/recentes (sem dono do post; sem media_product_type/thumbnail_url/view_count)
EP_HASHTAG_BUSCA = "ig_hashtag_search"
EP_HASHTAG_MIDIA = {"top": "{hashtag_id}/top_media", "recente": "{hashtag_id}/recent_media"}
CAMPOS_MIDIA_HASHTAG = ("id,caption,media_type,media_url,permalink,timestamp,like_count,comments_count,"
                        "children{media_type,media_url}")
# Sem children: usado quando a Meta responde "reduce the amount of data" (visto ao vivo em top_media com limit=50)
CAMPOS_MIDIA_HASHTAG_MIN = "id,caption,media_type,media_url,permalink,timestamp,like_count,comments_count"
LIMITE_PAGINA_HASHTAG = 50
LIMITE_PAGINA_HASHTAG_MIN = 5
LIMITE_HASHTAGS_7D = 30  # a Meta deixa consultar 30 hashtags únicas a cada 7 dias (janela móvel) por conta

MAX_PAGINAS = 10  # teto de páginas por perfil/hashtag em uma coleta

# Códigos de erro conhecidos (error.code / error.error_subcode da Graph API)
ERRO_TOKEN_CODIGOS = {190}                     # OAuthException: token expirado, revogado ou inválido
ERRO_TOKEN_SUBCODIGOS = {463: "expirou", 460: "foi invalidado (a senha do Facebook mudou)", 467: "é inválido",
                         458: "perdeu o acesso (o app foi removido da conta)"}
ERRO_PERFIL_CODIGOS = {110}                    # business_discovery: usuário não encontrado
ERRO_PERFIL_SUBCODIGOS = {2207013}             # "Invalid user id" / "Cannot find user" (pessoal, inexistente, +18)
ERRO_PERFIL_TEXTOS = ("cannot be found", "invalid user id", "cannot find user")
ERRO_CAMPO_CODIGOS = {100}                     # "(#100) Tried accessing nonexisting field (...)"
ERRO_CAMPO_TEXTOS = ("nonexisting field", "nonexistent field", "(#100)")
ERRO_DADOS_DEMAIS_CODIGOS = {1}                # "Please reduce the amount of data you're asking for"
ERRO_DADOS_DEMAIS_TEXTOS = ("reduce the amount of data",)
ERRO_LIMITE_HASHTAG_CODIGOS = {18}             # "Limit of 30 unique hashtag searches per week" (fonte de terceiros)
ERRO_LIMITE_HASHTAG_SUBCODIGOS = set()
ERRO_LIMITE_HASHTAG_TEXTOS = ("search limit", "30 unique")
ERRO_ACESSO_PUBLICO_TEXTOS = ("public content access",)  # hashtag sem o recurso Instagram Public Content Access
ERRO_LIMITE_CHAMADAS_CODIGOS = {4, 17, 32, 613, 80002}  # limites de chamadas (app, usuário, página, conta IG)
ERRO_PERMISSAO_CODIGOS = {10}                  # e qualquer código de 200 a 299
PERMISSOES_NECESSARIAS = ("pages_show_list, pages_read_engagement, business_management, instagram_basic e "
                          "instagram_manage_insights (ads_read se a Meta pedir)")
PREFIXOS_TOKEN_LOGIN_INSTAGRAM = ("IG",)       # tokens do "Login do Instagram" (graph.instagram.com) não servem

# ---------------------------------------------------------------------------
# Outras constantes
# ---------------------------------------------------------------------------
ARQ_REFS = "referencias.json"
ARQ_CSV = "referencias.csv"
PAUSA_LINKS = 2.0        # segundos entre chamadas do yt-dlp
TIMEOUT_YTDLP = 90
TIMEOUT_FFMPEG = 60
TIMEOUT_PIP = 300
PACOTE_YTDLP = "yt-dlp[default,curl-cffi]"
DICA_ATUALIZAR_YTDLP = ("atualize o yt-dlp (`winget upgrade yt-dlp.yt-dlp` ou "
                        f"`pip install -U \"{PACOTE_YTDLP}\"`) e tente de novo mais tarde; se continuar pedindo "
                        "login, peça à pessoa os links de outro jeito ou siga sem esses posts")
TIMEOUT_DOWNLOAD = 60          # por operação de rede
TEMPO_MAX_DOWNLOAD = 120       # segundos no total por arquivo
LIMITE_DOWNLOAD_BYTES = 25 * 1024 * 1024
HOSTS_MIDIA_PERMITIDOS = (".fbcdn.net", ".cdninstagram.com")  # CDN da Meta (ex.: instagram.fbau5-1.fna.fbcdn.net)
LIMITE_ARQUIVO_LINKS = 1024 * 1024
PAUSA_RETENTATIVA = 1.0        # segundos entre tentativas de gravar (OneDrive/Excel travando o arquivo)
TENTATIVAS_GRAVAR = 3
RE_ID_SEGURO = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
VARIAVEIS_SECRETAS = ("IG_GRAPH_TOKEN",)  # nunca repassadas a subprocessos
LARGURA_MINI = 360
UA_NAVEGADOR = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
UA_GARIMPO = "instagram-garimpo/1.0"
FORMATOS = ("carrossel", "reels", "imagem", "video")
APELIDOS_FORMATO = {"carrosel": "carrossel", "carousel": "carrossel", "reel": "reels", "foto": "imagem",
                    "image": "imagem", "imagens": "imagem", "videos": "video", "vídeo": "video"}
MSG_SEM_API = "API da Meta não configurada: rode configurar ou use links/busca na web"
RESERVADOS_PERFIL = {"p", "reel", "reels", "tv", "explore", "stories", "accounts", "direct", "about", "developer"}

RE_POST = re.compile(r"^(?:https?://)?(?:www\.|m\.)?(?:instagram\.com|instagr\.am)/"
                     r"(?:[A-Za-z0-9._]+/)?(p|reels?|tv)/([A-Za-z0-9_-]+)", re.I)
RE_PERFIL_URL = re.compile(r"^(?:https?://)?(?:www\.|m\.)?instagram\.com/([A-Za-z0-9._]+)/?(?:[?#].*)?$", re.I)
RE_HASHTAG_URL = re.compile(r"instagram\.com/explore/tags/([^/?#]+)", re.I)
RE_PARAM_SECRETO = re.compile(r"((?:access_token|client_secret|fb_exchange_token|input_token)=)[^&\s\"'<>]+", re.I)
RE_TOKEN_META = re.compile(r"\bEAA[A-Za-z0-9]{20,}")
RE_CAMPO_SECRETO = re.compile(r"(\"(?:token|app_secret|client_secret|access_token)\"\s*:\s*\")[^\"]+")


# ---------------------------------------------------------------------------
# Saída, segredos e utilidades
# ---------------------------------------------------------------------------
_SEGREDOS = set()
_AVISOS = []  # avisos não fatais de gravação (ex.: CSV aberto no Excel), somados aos erros do RESULTADO


def registrar_segredo(valor):
    if valor and len(str(valor)) >= 6:
        _SEGREDOS.add(str(valor))


def mascarar(texto):
    """Esconde token/segredos em qualquer texto (mensagens, URLs, RESULTADO)."""
    if texto is None:
        return texto
    texto = str(texto)
    for s in sorted(_SEGREDOS, key=len, reverse=True):
        texto = texto.replace(s, "***")
        codificado = urllib.parse.quote(s, safe="")
        if codificado != s:
            texto = texto.replace(codificado, "***")
    texto = RE_PARAM_SECRETO.sub(r"\1***", texto)
    texto = RE_TOKEN_META.sub("EAA***", texto)
    return RE_CAMPO_SECRETO.sub(r"\1***", texto)


def armar_mascara():
    """Registra os segredos conhecidos antes de qualquer saída (config salva e IG_GRAPH_TOKEN)."""
    try:
        ler_config_arquivo(tolerante=True)
    except Exception:  # noqa: BLE001
        pass
    registrar_segredo((os.environ.get("IG_GRAPH_TOKEN") or "").strip())


def _curto(valor, n=80):
    """Entrada inválida ecoada de volta: no máximo n caracteres."""
    s = str(valor)
    return s if len(s) <= n else s[: n - 1] + "…"


def env_subprocesso(**extra):
    """Ambiente para yt-dlp/ffmpeg/pip sem variáveis secretas."""
    env = {k: v for k, v in os.environ.items() if k not in VARIAVEIS_SECRETAS}
    env.update(extra)
    return env


def log(msg=""):
    print(mascarar(msg), flush=True)


def emitir(resultado):
    print("RESULTADO=" + mascarar(json.dumps(resultado, ensure_ascii=False)), flush=True)


def aviso(msg):
    if msg not in _AVISOS:
        _AVISOS.append(msg)
        log(f"!! {msg}")


class ErroFatal(Exception):
    """Erro que encerra o comando (saída != 0)."""


class ErroColeta(Exception):
    """Erro legível de um item (perfil, hashtag, link, imagem); o lote continua."""

    def __init__(self, mensagem, interrompe=False, motivo=None):
        super().__init__(mascarar(mensagem))
        self.interrompe = interrompe
        self.motivo = motivo  # "limite_hashtag": só as hashtags novas param; as já usadas na semana seguem


class ErroGraph(Exception):
    """Erro devolvido pela Graph API (ou de rede ao falar com ela)."""

    def __init__(self, codigo=None, subcodigo=None, mensagem="", status_http=None, tipo=None):
        self.codigo = codigo
        self.subcodigo = subcodigo
        self.mensagem = mascarar(mensagem or "")
        self.status_http = status_http
        self.tipo = tipo
        super().__init__(self.mensagem)

    def __str__(self):
        return traduzir_erro_graph(self)

    @property
    def interrompe(self):
        """Erros que vão se repetir em todos os próximos itens do lote."""
        c = self.codigo
        return (c in ERRO_TOKEN_CODIGOS or c in ERRO_LIMITE_CHAMADAS_CODIGOS or c in ERRO_PERMISSAO_CODIGOS
                or (c is not None and 200 <= c <= 299) or eh_limite_hashtag(self) or eh_acesso_publico(self))


def _int(v):
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def agora():
    return datetime.now().astimezone()


def agora_iso():
    return agora().isoformat(timespec="seconds")


def ler_iso(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.astimezone()


def ler_timestamp_graph(s):
    """'2024-05-01T12:34:56+0000' (Graph API) → datetime com fuso."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return ler_iso(s)


def gravar_texto_atomico(caminho, texto, encoding="utf-8"):
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding=encoding, newline="") as f:
        f.write(texto)
    os.replace(tmp, caminho)


# ---------------------------------------------------------------------------
# Ferramentas externas (mesmo padrão de instagram-transcrever/scripts/ig.py)
# ---------------------------------------------------------------------------
class ErroInstalacao(Exception):
    """pip não conseguiu instalar um pacote (mensagem curta e legível)."""


def achar_executavel(nome):
    """Caminho absoluto de `nome` só nas entradas absolutas do PATH (ignora vazias, relativas e a pasta atual).

    Não usa shutil.which: no Windows (Python < 3.12) ele procura primeiro na pasta atual.
    """
    try:
        atual = os.path.normcase(os.path.realpath(os.getcwd()))
    except OSError:
        atual = None
    extensoes = (".exe", ".com") if os.name == "nt" else ("",)
    for pasta in (os.environ.get("PATH") or "").split(os.pathsep):
        pasta = pasta.strip().strip('"')
        if not pasta or not os.path.isabs(pasta):
            continue
        if atual and os.path.normcase(os.path.realpath(pasta)) == atual:
            continue
        for ext in extensoes:
            alvo = os.path.join(pasta, nome + ext)
            if os.path.isfile(alvo) and os.access(alvo, os.X_OK):
                return os.path.abspath(alvo)
    return None


def _rodar_isolado(args, timeout, **kw):
    """Roda um subprocesso numa pasta temporária vazia, sem variáveis secretas no ambiente."""
    pasta = tempfile.mkdtemp(prefix="garimpo-")
    try:
        return subprocess.run(args, cwd=pasta, env=kw.pop("env", None) or env_subprocesso(), timeout=timeout,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
    finally:
        shutil.rmtree(pasta, ignore_errors=True)


def _importavel_isolado(modulo):
    try:
        return _rodar_isolado([sys.executable, "-I", "-c", f"import {modulo}"], 60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def garantir(pacote, modulo, atualizar=False, isolado=False):
    """Instala `pacote` com pip se `modulo` não estiver disponível (isolado: visível para `python -I`)."""
    disponivel = _importavel_isolado(modulo) if isolado else importlib.util.find_spec(modulo) is not None
    if disponivel and not atualizar:
        return
    nome = pacote.split("[")[0]
    log(f">> Instalando {nome} com pip (só nesta máquina)...")
    cmd = [sys.executable, "-I", "-m", "pip", "install", "-q", "-U", pacote]
    try:
        r = _rodar_isolado(cmd, TIMEOUT_PIP)
        if r.returncode != 0:
            r = _rodar_isolado(cmd + ["--break-system-packages"], TIMEOUT_PIP)
    except subprocess.TimeoutExpired:
        raise ErroInstalacao(f"o pip demorou mais de {TIMEOUT_PIP} s para instalar {nome}") from None
    except OSError as e:
        raise ErroInstalacao(f"não consegui rodar o pip para instalar {nome}: {e}") from None
    if r.returncode != 0:
        linhas = [l.strip() for l in (r.stderr or r.stdout or "").splitlines() if l.strip()]
        motivo = next((l for l in reversed(linhas) if l.upper().startswith("ERROR")), linhas[-1] if linhas else "")
        raise ErroInstalacao(f"o pip não conseguiu instalar {nome}" + (f": {motivo[:200]}" if motivo else ""))
    importlib.invalidate_caches()


_FFMPEG = {"caminho": None, "tentado": False}


def caminho_ffmpeg():
    """Caminho absoluto do ffmpeg (PATH absoluto ou imageio-ffmpeg) ou None."""
    if _FFMPEG["tentado"]:
        return _FFMPEG["caminho"]
    _FFMPEG["tentado"] = True
    exe = achar_executavel("ffmpeg")
    if not exe:
        garantir("imageio-ffmpeg", "imageio_ffmpeg")
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    _FFMPEG["caminho"] = os.path.abspath(exe) if exe and os.path.isfile(exe) else None
    return _FFMPEG["caminho"]


def ffmpeg_disponivel():
    try:
        return bool(caminho_ffmpeg())
    except Exception as e:  # noqa: BLE001
        log(f"!! ffmpeg indisponível ({e}); as imagens serão copiadas sem converter")
        return False


def ff(*args):
    """ffmpeg pelo caminho absoluto; levanta subprocess.SubprocessError em falha ou demora."""
    exe = caminho_ffmpeg()
    if not exe:
        raise subprocess.SubprocessError("ffmpeg indisponível")
    _rodar_ok([exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args], TIMEOUT_FFMPEG)


def _rodar_ok(args, timeout):
    r = _rodar_isolado(args, timeout)
    if r.returncode != 0:
        raise subprocess.CalledProcessError(r.returncode, args[0], r.stdout, r.stderr)


def comando_ytdlp():
    """yt-dlp do PATH (só entradas absolutas); senão `python -I -m yt_dlp` (instala via pip se faltar).

    Desde jun/2026 a leitura sem login do Instagram usa impersonação de navegador (curl_cffi);
    o executável oficial já vem com ele, a instalação via pip precisa do extra curl-cffi.
    """
    exe = achar_executavel("yt-dlp")
    if exe:
        return [exe]
    try:
        garantir(PACOTE_YTDLP, "yt_dlp", isolado=True)
    except ErroInstalacao as e:
        raise ErroFatal(f"{e}. Instale com: pip install -U \"{PACOTE_YTDLP}\"") from None
    try:
        garantir("curl_cffi", "curl_cffi", isolado=True)
    except ErroInstalacao as e:
        log(f"!! {e}; o yt-dlp vai pedir login com mais frequência")
    return [sys.executable, "-I", "-X", "utf8", "-m", "yt_dlp"]


# ---------------------------------------------------------------------------
# Graph API: um único ponto de saída HTTP (os testes trocam esta função)
# ---------------------------------------------------------------------------
def _texto_tem(e, textos):
    baixa = (e.mensagem or "").lower()
    return any(t in baixa for t in textos)


def eh_limite_hashtag(e):
    if e.codigo in ERRO_LIMITE_HASHTAG_CODIGOS or e.subcodigo in ERRO_LIMITE_HASHTAG_SUBCODIGOS:
        return True
    baixa = (e.mensagem or "").lower()
    return _texto_tem(e, ERRO_LIMITE_HASHTAG_TEXTOS) or ("hashtag" in baixa and ("limit" in baixa or "30" in baixa))


def eh_acesso_publico(e):
    return _texto_tem(e, ERRO_ACESSO_PUBLICO_TEXTOS)


def eh_campo_inexistente(e):
    return e.codigo in ERRO_CAMPO_CODIGOS and _texto_tem(e, ERRO_CAMPO_TEXTOS)


def eh_dados_demais(e):
    return e.codigo in ERRO_DADOS_DEMAIS_CODIGOS or _texto_tem(e, ERRO_DADOS_DEMAIS_TEXTOS)


MSG_ACESSO_PUBLICO = ("A busca por hashtag exige que o app passe pela Revisão de Apps da Meta (recurso Instagram "
                      "Public Content Access). Use perfis + links enquanto isso.")


def traduzir_erro_graph(e, contexto=None, alvo=None):
    """Mensagem legível em português para um ErroGraph. contexto: configurar|perfil|hashtag."""
    c, s, msg = e.codigo, e.subcodigo, e.mensagem or ""
    baixa = msg.lower()
    if c in ERRO_TOKEN_CODIGOS:
        if contexto == "configurar":
            return "a Meta recusou o token (expirado ou inválido): gere um token novo e rode `garimpo.py configurar` de novo"
        detalhe = ERRO_TOKEN_SUBCODIGOS.get(s, "expirou ou foi revogado")
        return f"token da Meta {detalhe}: rode `garimpo.py configurar` de novo (no seu terminal)"
    if contexto == "perfil" and (c in ERRO_PERFIL_CODIGOS or s in ERRO_PERFIL_SUBCODIGOS
                                 or any(t in baixa for t in ERRO_PERFIL_TEXTOS)):
        return f"@{alvo} não encontrado ou não é conta profissional (Business/Creator); use busca na web + links"
    if eh_acesso_publico(e) or (contexto == "hashtag" and c in ERRO_PERMISSAO_CODIGOS):
        return MSG_ACESSO_PUBLICO
    if eh_limite_hashtag(e):
        return (f"limite da Meta atingido: no máximo {LIMITE_HASHTAGS_7D} hashtags únicas a cada 7 dias por conta; "
                "espere liberar (veja `garimpo.py status`) ou use busca na web + links")
    if c in ERRO_LIMITE_CHAMADAS_CODIGOS:
        return f"limite de chamadas da API da Meta atingido (código {c}); espere cerca de 1 hora e tente de novo"
    if c in ERRO_PERMISSAO_CODIGOS or (c is not None and 200 <= c <= 299):
        return (f"o token não tem a permissão necessária (código {c}): gere um token com {PERMISSOES_NECESSARIAS} "
                "e rode `garimpo.py configurar` de novo")
    if c is None:
        return msg or "falha desconhecida ao falar com a Graph API"
    sub = f"/{s}" if s else ""
    return f"erro da Graph API (código {c}{sub}): {msg}"


def _erro_da_resposta(corpo, status=None):
    try:
        dados = json.loads(corpo)
    except ValueError:
        dados = None
    err = dados.get("error") if isinstance(dados, dict) else None
    if isinstance(err, dict):
        msg = err.get("message") or ""
        if err.get("error_user_msg") and err.get("error_user_msg") not in msg:
            msg = f"{msg} — {err['error_user_msg']}" if msg else err["error_user_msg"]
        return ErroGraph(_int(err.get("code")), _int(err.get("error_subcode")), msg, status, err.get("type"))
    return ErroGraph(None, None, f"resposta inesperada da Graph API (HTTP {status}): {(corpo or '')[:200]}", status)


def graph_get(caminho, params=None, token=None, versao=None):
    """GET na Graph API. Devolve o JSON (dict) ou levanta ErroGraph com mensagem já mascarada."""
    versao = versao or GRAPH_VERSAO_PADRAO
    consulta = {k: v for k, v in (params or {}).items() if v is not None}
    if token:
        registrar_segredo(token)
        consulta["access_token"] = token
    url = f"{GRAPH_BASE}/{versao}/{caminho.lstrip('/')}?{urllib.parse.urlencode(consulta)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA_GARIMPO})
    try:
        with urllib.request.urlopen(req, timeout=GRAPH_TIMEOUT) as r:
            corpo = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            corpo = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            corpo = ""
        raise _erro_da_resposta(corpo, e.code) from None
    except (urllib.error.URLError, OSError) as e:
        motivo = getattr(e, "reason", None) or e
        raise ErroGraph(None, None, f"falha de rede ao falar com a Graph API: {motivo}") from None
    try:
        dados = json.loads(corpo)
    except ValueError:
        raise ErroGraph(None, None, f"resposta inválida da Graph API: {corpo[:200]}") from None
    if isinstance(dados, dict) and "error" in dados:
        raise _erro_da_resposta(corpo, 200)
    return dados


# ---------------------------------------------------------------------------
# Configuração (~/.instagram-garimpo/config.json; IG_GARIMPO_CONFIG troca o caminho)
# ---------------------------------------------------------------------------
def caminho_config():
    return os.environ.get("IG_GARIMPO_CONFIG") or os.path.join(
        os.path.expanduser("~"), ".instagram-garimpo", "config.json")


def ler_config_arquivo(tolerante=False):
    p = caminho_config()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            dados = json.load(f)
    except (OSError, ValueError) as e:
        if tolerante:
            return {}
        raise ErroFatal(f"não consegui ler a configuração em {p} ({e}); rode `garimpo.py configurar` de novo")
    if not isinstance(dados, dict):
        return {}
    registrar_segredo(dados.get("token"))
    return dados


def gravar_config_arquivo(cfg):
    p = caminho_config()
    os.makedirs(os.path.dirname(p) or ".", mode=0o700, exist_ok=True)
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    if os.name == "posix":
        os.chmod(p, 0o600)


def config_efetiva():
    """Config do arquivo + variáveis de ambiente (IG_GRAPH_TOKEN, IG_USER_ID, IG_GRAPH_VERSION)."""
    cfg = dict(ler_config_arquivo())
    tok = (os.environ.get("IG_GRAPH_TOKEN") or "").strip()
    uid = (os.environ.get("IG_USER_ID") or "").strip()
    ver = (os.environ.get("IG_GRAPH_VERSION") or "").strip()
    if tok:
        cfg["token"] = tok
        cfg["_token_do_ambiente"] = True
        registrar_segredo(tok)
    if uid:
        cfg["conta_padrao"] = uid
    if ver:
        cfg["versao_api"] = ver
    if not re.fullmatch(r"v\d+\.\d+", cfg.get("versao_api") or ""):
        if cfg.get("versao_api"):
            log(f"!! versão da API inválida ({cfg['versao_api']}); usando {GRAPH_VERSAO_PADRAO}")
        cfg["versao_api"] = GRAPH_VERSAO_PADRAO
    return cfg


def api_pronta(cfg):
    return bool(cfg.get("token") and cfg.get("conta_padrao"))


def hashtags_recentes(cfg_arquivo):
    """Entradas de hashtags_usadas dos últimos 7 dias (uma por tag, a mais antiga dentro da janela)."""
    limite = agora() - timedelta(days=7)
    vistas = {}
    for h in cfg_arquivo.get("hashtags_usadas") or []:
        quando = ler_iso(h.get("quando")) if isinstance(h, dict) else None
        tag = (h.get("tag") or "").lower() if isinstance(h, dict) else ""
        if not tag or not quando or quando < limite:
            continue
        if tag not in vistas or quando < ler_iso(vistas[tag]["quando"]):
            vistas[tag] = dict(h, tag=tag)
    return list(vistas.values())


def registrar_hashtag(tag, hashtag_id):
    arq = ler_config_arquivo(tolerante=True)
    lista = hashtags_recentes(arq)
    atual = next((h for h in lista if h["tag"] == tag), None)
    if atual:
        atual.setdefault("id", hashtag_id)
    else:
        lista.append({"tag": tag, "quando": agora_iso(), "id": hashtag_id})
    arq["hashtags_usadas"] = lista
    gravar_config_arquivo(arq)


# ---------------------------------------------------------------------------
# referencias.json / referencias.csv
# ---------------------------------------------------------------------------
def nova_ref(ident, link=None):
    return {
        "id": ident, "ig_media_id": None, "link": link, "perfil": None, "origens": [], "formato": None,
        "data": None, "legenda": "", "curtidas": None, "curtidas_ocultas": False, "comentarios": None,
        "visualizacoes": None, "interacoes": None, "seguidores": None, "taxa_engajamento": None, "acima_da_media": None, "nota": 0,
        "capa_url": None, "midia_urls": [], "midia": [], "mini": [], "incompleta": False,
    }


def contagem(dados):
    """total = referências completas; incompletas = links que falharam (só id/link)."""
    refs = dados.get("referencias") or []
    incompletas = sum(1 for r in refs if r.get("incompleta"))
    return {"total": len(refs) - incompletas, "incompletas": incompletas}


def dados_vazios(tema):
    t = agora_iso()
    return {"versao": 1, "tema": tema, "criado_em": t, "atualizado_em": t,
            "buscas": [], "perfis": {}, "referencias": []}


def tema_da_pasta(pasta):
    return re.sub(r"^\d{4}-\d{2}-\d{2} - ", "", os.path.basename(os.path.normpath(pasta)))


def carregar(pasta):
    if not os.path.isdir(pasta):
        raise ErroFatal(f"pasta não encontrada: {pasta}. Rode `garimpo.py novo \"<tema>\"` primeiro")
    p = os.path.join(pasta, ARQ_REFS)
    pendente = p + ".pendente"
    if os.path.exists(pendente) and (not os.path.exists(p) or os.path.getmtime(pendente) >= os.path.getmtime(p)):
        log(f">> Retomando os dados de {ARQ_REFS}.pendente (a última gravação não conseguiu atualizar {ARQ_REFS})")
        p = pendente
    if not os.path.exists(p):
        return dados_vazios(tema_da_pasta(pasta))
    try:
        with open(p, encoding="utf-8") as f:
            dados = json.load(f)
    except (OSError, ValueError) as e:
        raise ErroFatal(f"{ARQ_REFS} está corrompido ou ilegível ({e}); corrija ou apague o arquivo")
    if not isinstance(dados, dict):
        raise ErroFatal(f"{ARQ_REFS} não tem o formato esperado")
    base = dados_vazios(tema_da_pasta(pasta))
    for k, v in base.items():
        if not isinstance(dados.get(k), type(v)):
            dados[k] = v if k != "tema" or not dados.get(k) else str(dados[k])
    refs = []
    for r in dados["referencias"]:
        if isinstance(r, dict) and r.get("id"):
            completa = nova_ref(r["id"], r.get("link"))
            completa.update(r)
            refs.append(completa)
    dados["referencias"] = refs
    return dados


def nova_busca(origem, valor, qtd, erro, **extra):
    b = {"origem": origem, "valor": valor, "quando": agora_iso(), "qtd": qtd, "erro": erro}
    b.update(extra)
    return b


def mesclar(dados, novas):
    """Junta referências novas às existentes, deduplicando por id (shortcode).

    Devolve quantas referências completas passaram a existir (as incompletas não contam).
    """
    refs = dados.setdefault("referencias", [])
    indice = {r["id"]: r for r in refs}
    novos = 0
    for n in novas:
        if not n or not n.get("id"):
            continue
        velho = indice.get(n["id"])
        if velho is None:
            r = nova_ref(n["id"], n.get("link"))
            r.update({k: v for k, v in n.items() if k in r})
            r["incompleta"] = bool(n.get("incompleta"))
            refs.append(r)
            indice[r["id"]] = r
            novos += 0 if r["incompleta"] else 1
            continue
        if not n.get("incompleta") and velho.get("incompleta"):
            velho["incompleta"] = False  # um dado completo completa o esboço
            novos += 1
        origens = list(velho.get("origens") or [])
        for o in n.get("origens") or []:
            if o not in origens:
                origens.append(o)
        # o formato vindo da API (ig_media_id) é mais confiável que o deduzido pelo yt-dlp
        manter_formato = bool(velho.get("formato") and velho.get("ig_media_id") and not n.get("ig_media_id"))
        for k, v in n.items():
            if k in ("id", "origens", "nota", "curtidas_ocultas", "incompleta") or v is None or v == "" or v == []:
                continue
            if k == "formato" and manter_formato:
                continue
            velho[k] = v
        velho["origens"] = origens
    return novos


def mesclar_perfil(dados, usuario, info):
    p = dados.setdefault("perfis", {}).setdefault(usuario, {})
    for k, v in (info or {}).items():
        if k == "interacoes_amostra":
            # amostra para a mediana do perfil: todos os posts da janela de --dias, antes do filtro de --formatos
            p[k] = dict(p.get(k) or {}, **(v or {}))
        elif v not in (None, ""):
            p[k] = v
    p.setdefault("mediana_interacoes", None)


def grupos_da_ref(r):
    gs = set()
    for o in r.get("origens") or []:
        o = str(o)
        gs.add("perfil" if o.startswith("@") else "hashtag" if o.startswith("#") else "link")
    return gs


def _mediana(vals):
    m = round(float(statistics.median(vals)), 1)
    return int(m) if m.is_integer() else m


def calcular_notas(dados):
    """Recalcula interacoes, mediana por perfil, taxa_engajamento, acima_da_media e nota; ordena."""
    refs = dados.get("referencias") or []
    perfis = dados.get("perfis") or {}
    for r in refs:
        c, m = _int(r.get("curtidas")), _int(r.get("comentarios"))
        # curtidas ocultas pelo dono: a Meta omite like_count; interações ficam só com os comentários
        r["curtidas_ocultas"] = c is None and m is not None
        r["interacoes"] = None if c is None and m is None else (c or 0) + (m or 0)
        r["incompleta"] = bool(r.get("incompleta"))

    def comparavel(r):
        return not r["curtidas_ocultas"] and not r["incompleta"]

    # mediana por perfil: todos os posts do perfil dentro de --dias (amostra gravada na coleta, antes do
    # filtro de --formatos) + os posts coletados dele; sem curtidas ocultas nem incompletas
    for usuario, p in perfis.items():
        alvo = "@" + usuario.lower()
        amostra = {str(k): v for k, v in (p.get("interacoes_amostra") or {}).items() if _int(v) is not None}
        for r in refs:
            if (r["interacoes"] is not None and comparavel(r)
                    and alvo in [str(o).lower() for o in r.get("origens") or []]):
                amostra[r["id"]] = r["interacoes"]
        vals = [_int(v) for v in amostra.values()]
        p["mediana_interacoes"] = _mediana(vals) if vals else None
    acima_bruto = {}
    for r in refs:
        p = perfis.get((r.get("perfil") or "").lower()) or {}
        seg = _int(p.get("seguidores")) or _int(r.get("seguidores"))
        r["seguidores"] = seg
        inter = r["interacoes"] if comparavel(r) else None  # só comentários não se compara com a mediana
        r["taxa_engajamento"] = round(inter / seg, 4) if inter is not None and seg else None
        med = p.get("mediana_interacoes")
        acima = inter / med if inter is not None and med else None
        r["acima_da_media"] = round(acima, 2) if acima is not None else None
        acima_bruto[r["id"]] = acima

    def valor(r, grupo):
        return acima_bruto.get(r["id"]) if grupo == "perfil" else r["interacoes"]

    # nota = percentil dentro do grupo da origem (perfil: acima_da_media; hashtag/link: interacoes);
    # curtidas ocultas e incompletas ficam fora dos grupos e sem nota (null)
    grupos = {"perfil": [], "hashtag": [], "link": []}
    for r in refs:
        if not comparavel(r):
            continue
        for g in grupos_da_ref(r):
            v = valor(r, g)
            if v is not None:
                grupos[g].append(v)
    for g in grupos.values():
        g.sort()
    for r in refs:
        if not comparavel(r):
            r["nota"] = None
            continue
        nota = 0
        for g in grupos_da_ref(r):
            v = valor(r, g)
            if v is None or not grupos[g]:
                continue
            nota = max(nota, int(round(100 * bisect.bisect_right(grupos[g], v) / len(grupos[g]))))
        r["nota"] = nota

    def chave(r):
        if r["incompleta"]:
            return (2, 0, 0)
        if r.get("nota") is None:  # curtidas ocultas: depois das com nota, por comentários
            return (1, -(_int(r.get("comentarios")) or 0), 0)
        return (0, -r["nota"], -(r["interacoes"] if r.get("interacoes") is not None else -1))

    refs.sort(key=chave)
    return dados


COLUNAS_CSV = ["nota", "id", "link", "perfil", "formato", "data", "curtidas", "comentarios", "visualizacoes",
               "interacoes", "seguidores", "taxa_engajamento", "acima_da_media", "origens", "legenda"]


def _num_csv(v, casas):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.{casas}f}".replace(".", ",")
    return str(v)


def _texto_csv(s, limite=None):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    if limite:
        s = s[:limite]
    if s and s[0] in "=+-@":  # evita que o Excel interprete a legenda como fórmula
        s = "'" + s
    return s


def _origens_csv(origens):
    rotulos = []
    for o in origens or []:
        o = str(o)
        rotulos.append(f"perfil {o}" if o.startswith("@") else f"hashtag {o}" if o.startswith("#")
                       else "busca na web" if o == "busca" else o)
    return ", ".join(rotulos)


MSG_ARQUIVO_TRAVADO = "feche o arquivo no Excel ou espere o OneDrive sincronizar e rode de novo"


def _gravar_com_retentativa(caminho, texto, encoding="utf-8"):
    """Grava de forma atômica; com PermissionError (OneDrive/Excel travando) tenta de novo. Devolve True/False."""
    for tentativa in range(TENTATIVAS_GRAVAR):
        try:
            gravar_texto_atomico(caminho, texto, encoding)
            return True
        except PermissionError:
            _remover(caminho + ".tmp")
            if tentativa + 1 < TENTATIVAS_GRAVAR:
                time.sleep(PAUSA_RETENTATIVA)
    return False


def texto_csv(dados):
    linhas = []

    class _Saida:
        def write(self, s):
            linhas.append(s)

    w = csv.writer(_Saida(), delimiter=";", lineterminator="\r\n")
    w.writerow(COLUNAS_CSV)
    for r in dados.get("referencias") or []:
        if r.get("incompleta"):
            continue
        w.writerow([
            "" if r.get("nota") is None else r["nota"], _texto_csv(r.get("id")), _texto_csv(r.get("link")),
            _texto_csv(r.get("perfil")), _texto_csv(r.get("formato")), _texto_csv(r.get("data")),
            _num_csv(r.get("curtidas"), 0), _num_csv(r.get("comentarios"), 0),
            _num_csv(r.get("visualizacoes"), 0), _num_csv(r.get("interacoes"), 0),
            _num_csv(r.get("seguidores"), 0), _num_csv(r.get("taxa_engajamento"), 4),
            _num_csv(r.get("acima_da_media"), 2), _texto_csv(_origens_csv(r.get("origens"))),
            _texto_csv(r.get("legenda"), 300),
        ])
    return "".join(linhas)


def escrever_csv(pasta, dados):
    if not _gravar_com_retentativa(os.path.join(pasta, ARQ_CSV), texto_csv(dados), encoding="utf-8-sig"):
        aviso(f"não consegui atualizar {ARQ_CSV} (arquivo travado): {MSG_ARQUIVO_TRAVADO} — "
              f"o {ARQ_REFS} tem todos os dados")


def salvar(pasta, dados):
    """Recalcula tudo e grava referencias.json + referencias.csv (com plano B em referencias.json.pendente)."""
    calcular_notas(dados)
    dados["atualizado_em"] = agora_iso()
    texto = json.dumps(dados, ensure_ascii=False, indent=2)
    caminho = os.path.join(pasta, ARQ_REFS)
    pendente = caminho + ".pendente"
    if _gravar_com_retentativa(caminho, texto):
        _remover(pendente)
    elif _gravar_com_retentativa(pendente, texto):
        aviso(f"não consegui gravar {ARQ_REFS} (arquivo travado): os dados desta coleta ficaram em "
              f"{ARQ_REFS}.pendente e serão usados na próxima vez; {MSG_ARQUIVO_TRAVADO}")
    else:
        raise ErroFatal(f"não consegui gravar {ARQ_REFS} nem {ARQ_REFS}.pendente (arquivos travados): "
                        f"{MSG_ARQUIVO_TRAVADO}")
    escrever_csv(pasta, dados)


# ---------------------------------------------------------------------------
# Entradas: links, perfis, hashtags
# ---------------------------------------------------------------------------
def id_do_link(url):
    """Link de post → (SHORTCODE, link normalizado) ou None."""
    if not url:
        return None
    m = RE_POST.match(str(url).strip().strip("<>\"'"))
    if not m:
        return None
    tipo = "reel" if m[1].lower().startswith("reel") else "p"
    return m[2], f"https://www.instagram.com/{tipo}/{m[2]}/"


def normalizar_perfil(valor):
    v = str(valor).strip()
    if RE_POST.match(v):
        raise ValueError("é link de post, não de perfil; use o comando links")
    m = RE_PERFIL_URL.match(v)
    if m:
        v = m[1]
    elif "instagram.com" in v.lower():
        raise ValueError("não é link de perfil do Instagram")
    if v.startswith("#"):
        raise ValueError("parece hashtag; use o comando hashtags")
    v = v.lstrip("@").strip().lower()
    if not re.fullmatch(r"[a-z0-9._]{1,30}", v) or v in RESERVADOS_PERFIL:
        raise ValueError("nome de perfil inválido (use @usuario, usuario ou o link do perfil)")
    return v


def normalizar_hashtag(valor):
    v = str(valor).strip()
    m = RE_HASHTAG_URL.search(v)
    if m:
        v = urllib.parse.unquote(m[1])
    v = v.lstrip("#").strip().lower()
    if not re.fullmatch(r"\w{1,100}", v):
        raise ValueError("hashtag inválida (use tag ou #tag, sem espaços)")
    return v


def mapear_formato(media_type, product_type=None, link=None):
    t = (media_type or "").upper()
    if t == "CAROUSEL_ALBUM":
        return "carrossel"
    if t == "VIDEO":
        if (product_type or "").upper() == "REELS" or "/reel/" in (link or ""):
            return "reels"
        return "video"
    if t == "IMAGE":
        return "imagem"
    return None


def urls_de_midia_graph(item):
    """(urls, capa): uma URL por slide (capa do vídeo para reels) e a primeira que é imagem.

    Na hashtag não existe thumbnail_url: o slide de vídeo fica com o mp4 (o comando midia tira o quadro
    com ffmpeg), mas um mp4 nunca vira capa_url.
    """
    def imagem_de(x):
        if (x.get("media_type") or "").upper() == "VIDEO":
            if x.get("thumbnail_url"):
                return x["thumbnail_url"], False
            return x.get("media_url"), True
        return x.get("media_url") or x.get("thumbnail_url"), False

    pares = []
    filhos = ((item.get("children") or {}).get("data")) or []
    if (item.get("media_type") or "").upper() == "CAROUSEL_ALBUM" and filhos:
        pares = [p for p in (imagem_de(f) for f in filhos) if p[0]]
    if not pares:
        p = imagem_de(item)
        if p[0]:
            pares = [p]
    urls = [u for u, _ in pares]
    capa = next((u for u, eh_video in pares if not eh_video), None)
    return urls, capa


def ref_de_graph(item, origem, perfil=None, seguidores=None):
    """Item de mídia da Graph API → (ref, datetime) ou (None, None) se não tiver permalink válido."""
    ident = id_do_link(item.get("permalink"))
    if not ident:
        return None, None
    sc, link = ident
    ts = ler_timestamp_graph(item.get("timestamp"))
    urls, capa = urls_de_midia_graph(item)
    r = nova_ref(sc, link)
    r.update({
        "ig_media_id": str(item["id"]) if item.get("id") else None,
        "perfil": perfil, "origens": [origem],
        "formato": mapear_formato(item.get("media_type"), item.get("media_product_type"), link),
        "data": ts.astimezone().date().isoformat() if ts else None,
        "legenda": item.get("caption") or "",
        "curtidas": _int(item.get("like_count")), "comentarios": _int(item.get("comments_count")),
        "visualizacoes": _int(item.get("view_count")),
        "seguidores": seguidores, "capa_url": capa, "midia_urls": urls,
    })
    return r, ts


def _passa_filtros(ref, ts, corte, formatos):
    if corte and ts and ts < corte:
        return False
    return not formatos or ref.get("formato") in formatos


# ---------------------------------------------------------------------------
# Coleta pela Graph API
# ---------------------------------------------------------------------------
def coletar_perfil(usuario, cfg, limite=30, dias=90, formatos=None):
    """Business Discovery de @usuario. Devolve (info_do_perfil, refs). Levanta ErroColeta."""
    corte = agora() - timedelta(days=dias) if dias else None
    tam = LIMITE_PAGINA_PERFIL if formatos else max(1, min(LIMITE_PAGINA_PERFIL, limite))
    refs, vistos, info, cursor = [], set(), None, None
    amostra = {}  # id → interações de todo post da janela (antes de --formatos), para a mediana do perfil
    campos_perfil, campos_midia, minimos = CAMPOS_PERFIL, CAMPOS_MIDIA_PERFIL, False
    paginas = 0
    while paginas < MAX_PAGINAS:
        pag = f".after({cursor}).limit({tam})" if cursor else f".limit({tam})"
        campos = MODELO_BUSINESS_DISCOVERY.format(usuario=usuario, campos_perfil=campos_perfil,
                                                  paginacao=pag, campos_midia=campos_midia)
        try:
            resp = graph_get(str(cfg["conta_padrao"]), {"fields": campos},
                             token=cfg.get("token"), versao=cfg.get("versao_api"))
        except ErroGraph as e:
            if not minimos and eh_campo_inexistente(e):
                # a Meta recusou algum campo não confirmado como público: tenta uma vez com o conjunto mínimo
                log(f"!! @{usuario}: a Meta recusou algum campo ({e.mensagem[:120]}); repetindo com campos mínimos")
                campos_perfil, campos_midia, minimos = CAMPOS_PERFIL_MIN, CAMPOS_MIDIA_PERFIL_MIN, True
                continue
            if eh_dados_demais(e) and tam > LIMITE_PAGINA_PERFIL_MIN:
                tam = max(LIMITE_PAGINA_PERFIL_MIN, tam // 2)
                log(f"!! @{usuario}: a Meta pediu menos dados por vez; repetindo com {tam} posts por página")
                continue
            msg = traduzir_erro_graph(e, "perfil", usuario)
            if refs:
                log(f"!! @{usuario}: paginação interrompida ({msg}); ficam os {len(refs)} posts já lidos")
                break
            raise ErroColeta(msg, e.interrompe) from None
        paginas += 1
        bd = (resp or {}).get("business_discovery") or {}
        if not bd:
            raise ErroColeta(f"@{usuario} não encontrado ou não é conta profissional (Business/Creator); "
                             "use busca na web + links")
        if info is None:
            info = {"nome": bd.get("name"), "bio": bd.get("biography"),
                    "seguidores": _int(bd.get("followers_count")), "posts": _int(bd.get("media_count")),
                    "foto_url": bd.get("profile_picture_url"), "ig_user_id": bd.get("id"),
                    "interacoes_amostra": amostra}
        midia = bd.get("media") or {}
        itens = midia.get("data") or []
        ultimo_ts = None
        for it in itens:
            ref, ts = ref_de_graph(it, "@" + usuario, usuario, info["seguidores"])
            ultimo_ts = ts or ultimo_ts
            if ref is None or ref["id"] in vistos:
                continue
            vistos.add(ref["id"])
            if corte and ts and ts < corte:
                continue
            if ref["curtidas"] is not None:  # curtidas ocultas ficam fora da mediana
                amostra[ref["id"]] = ref["curtidas"] + (ref["comentarios"] or 0)
            if not formatos or ref.get("formato") in formatos:
                refs.append(ref)
                if len(refs) >= limite:
                    return info, refs
        # o feed vem do mais novo para o mais antigo: se o último da página passou do corte, para
        apos = ((midia.get("paging") or {}).get("cursors") or {}).get("after")
        if not itens or not apos or apos == cursor or (corte and ultimo_ts and ultimo_ts < corte):
            break
        cursor = apos
    return info, refs


def coletar_hashtag(tag, cfg, tipo="top", limite=50, dias=90, formatos=None):
    """Mídias top/recentes de #tag. Registra o uso em hashtags_usadas. Levanta ErroColeta."""
    recentes = hashtags_recentes(ler_config_arquivo(tolerante=True))
    conhecida = next((h for h in recentes if h["tag"] == tag), None)
    if not conhecida and len(recentes) >= LIMITE_HASHTAGS_7D:
        libera = min(ler_iso(h["quando"]) for h in recentes) + timedelta(days=7)
        raise ErroColeta(f"limite da Meta: {len(recentes)}/{LIMITE_HASHTAGS_7D} hashtags únicas nos últimos 7 dias; "
                         f"a próxima vaga libera em {libera:%d/%m às %H:%M}. Repita hashtags já consultadas, "
                         "use busca na web + links ou espere", interrompe=True, motivo="limite_hashtag")
    uid, tok, ver = str(cfg["conta_padrao"]), cfg.get("token"), cfg.get("versao_api")
    hid = (conhecida or {}).get("id")
    if not hid:
        try:
            resp = graph_get(EP_HASHTAG_BUSCA, {"user_id": uid, "q": tag}, token=tok, versao=ver)
        except ErroGraph as e:
            raise ErroColeta(traduzir_erro_graph(e, "hashtag", tag), e.interrompe,
                             "limite_hashtag" if eh_limite_hashtag(e) else None) from None
        achados = (resp or {}).get("data") or []
        if not achados or not achados[0].get("id"):
            raise ErroColeta(f"#{tag} não encontrada na API da Meta (confira a grafia)")
        hid = str(achados[0]["id"])
    registrar_hashtag(tag, hid)

    # recent_media só cobre as últimas 24 h: o filtro de dias não se aplica
    corte = agora() - timedelta(days=dias) if dias and tipo != "recente" else None
    caminho = EP_HASHTAG_MIDIA[tipo].format(hashtag_id=hid)
    refs, vistos, apos = [], set(), None
    campos = CAMPOS_MIDIA_HASHTAG
    tam = min(LIMITE_PAGINA_HASHTAG, max(limite, LIMITE_PAGINA_HASHTAG_MIN))
    paginas = 0
    while paginas < MAX_PAGINAS:
        params = {"user_id": uid, "fields": campos, "limit": tam, "after": apos}
        try:
            resp = graph_get(caminho, params, token=tok, versao=ver)
        except ErroGraph as e:
            # "Please reduce the amount of data": primeiro tira os slides (children), depois reduz a página.
            # Repetir a mesma hashtag não gasta outra vaga do limite semanal.
            if eh_dados_demais(e) and campos != CAMPOS_MIDIA_HASHTAG_MIN:
                campos = CAMPOS_MIDIA_HASHTAG_MIN
                log(f"   #{tag}: a Meta pediu menos dados; tentando sem os slides dos carrosséis")
                continue
            if eh_dados_demais(e) and tam > LIMITE_PAGINA_HASHTAG_MIN:
                tam = max(LIMITE_PAGINA_HASHTAG_MIN, tam // 2)
                log(f"   #{tag}: a Meta pediu menos dados; tentando com {tam} posts por página")
                continue
            msg = traduzir_erro_graph(e, "hashtag", tag)
            if refs:
                log(f"!! #{tag}: paginação interrompida ({msg}); ficam os {len(refs)} posts já lidos")
                break
            raise ErroColeta(msg, e.interrompe, "limite_hashtag" if eh_limite_hashtag(e) else None) from None
        paginas += 1
        itens = (resp or {}).get("data") or []
        ids_novos = 0
        for it in itens:
            ref, ts = ref_de_graph(it, "#" + tag)
            if ref is None or ref["id"] in vistos:
                continue
            vistos.add(ref["id"])
            ids_novos += 1
            if _passa_filtros(ref, ts, corte, formatos):
                refs.append(ref)
                if len(refs) >= limite:
                    return refs
        # o cursor "after" se repete em todas as páginas (comportamento documentado): não dá para parar nele;
        # para quando a página não traz nada novo ou não há paging.next
        paging = (resp or {}).get("paging") or {}
        prox = (paging.get("cursors") or {}).get("after")
        if not ids_novos or not paging.get("next") or not prox:
            break
        apos = prox
    return refs


# ---------------------------------------------------------------------------
# Coleta por link (yt-dlp, sem baixar vídeo)
# ---------------------------------------------------------------------------
def _primeiro(*vals):
    return next((v for v in vals if v not in (None, "", [])), None)


def thumb_original(e):
    """A primeira miniatura sem 'stp=' na URL é o arquivo original (como em ig.py)."""
    thumbs = [t.get("url") for t in (e.get("thumbnails") or []) if isinstance(t, dict) and t.get("url")]
    orig = next((u for u in thumbs if "stp=" not in u), None)
    return orig or e.get("thumbnail") or (thumbs[-1] if thumbs else None)


def _tem_video(e):
    return bool(e.get("duration")) or any(
        isinstance(f, dict) and f.get("url") and (f.get("vcodec") or "") != "none" for f in e.get("formats") or [])


def preencher_ref_ytdlp(ref, info):
    entradas = [e for e in (info.get("entries") or []) if isinstance(e, dict)]
    itens = entradas or [info]

    def pega(chave):
        return _primeiro(info.get(chave), *(e.get(chave) for e in itens))

    ref["curtidas"] = _int(pega("like_count"))
    ref["comentarios"] = _int(pega("comment_count"))
    ref["visualizacoes"] = _int(pega("view_count"))
    ref["legenda"] = pega("description") or ""
    perfil = pega("channel")
    uid = pega("uploader_id")
    if not perfil and uid and not str(uid).isdigit():
        perfil = uid
    if perfil and re.fullmatch(r"[A-Za-z0-9._]{1,30}", str(perfil)):
        ref["perfil"] = str(perfil).lower()
    ts = pega("timestamp")
    d = str(pega("upload_date") or "")
    if ts:
        try:
            ref["data"] = datetime.fromtimestamp(float(ts), timezone.utc).astimezone().date().isoformat()
        except (OverflowError, OSError, ValueError):
            pass
    if not ref["data"] and re.fullmatch(r"\d{8}", d):
        ref["data"] = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
    if len(itens) > 1:
        ref["formato"] = "carrossel"
    elif "/reel/" in ref["link"] or any(_tem_video(e) for e in itens):
        ref["formato"] = "reels"
    else:
        ref["formato"] = "imagem"
    urls = [u for u in (thumb_original(e) for e in itens) if u]
    ref["midia_urls"] = urls
    ref["capa_url"] = urls[0] if urls else None
    return ref


def traduzir_erro_ytdlp(stderr):
    linhas = [l.strip() for l in (stderr or "").splitlines() if l.strip()]
    erro = next((l for l in reversed(linhas) if l.startswith("ERROR")), linhas[-1] if linhas else "sem detalhes")
    baixa = erro.lower()
    if any(k in baixa for k in ("login", "rate-limit", "rate limit", "cookies", "authentication", "checkpoint",
                                "empty media response")):
        return ("o Instagram pediu login para ler este post (limite de acesso anônimo ou post restrito): "
                + DICA_ATUALIZAR_YTDLP)
    if any(k in baixa for k in ("not available", "404", "does not exist", "unavailable", "private")):
        return "post indisponível (removido, privado ou link errado)"
    return "o yt-dlp não conseguiu ler o post: " + erro[:200]


def coletar_link(link, cmd_ytdlp):
    """Lê métricas e miniaturas de um post via yt-dlp (sem login, sem cookies). Devolve (ref, erro|None).

    Em falha a ref volta só com id/link e `incompleta: true`. Nunca levanta.
    """
    sc, link = id_do_link(link)
    ref = nova_ref(sc, link)
    ref["origens"] = ["busca"]
    args = list(cmd_ytdlp) + ["--ignore-config", "-J", "--skip-download", "--ignore-no-formats-error",
                              "--no-warnings", "--", link]
    env = env_subprocesso(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    try:
        p = _rodar_isolado(args, TIMEOUT_YTDLP, env=env)
    except subprocess.TimeoutExpired:
        ref["incompleta"] = True
        return ref, f"o yt-dlp demorou mais de {TIMEOUT_YTDLP} s; tente de novo mais tarde"
    except OSError as e:
        ref["incompleta"] = True
        return ref, f"não consegui rodar o yt-dlp: {e}"
    info = None
    saida = (p.stdout or "").strip()
    for candidato in (saida, saida.splitlines()[-1] if saida else ""):
        try:
            info = json.loads(candidato)
            break
        except ValueError:
            continue
    if not isinstance(info, dict):
        ref["incompleta"] = True
        return ref, traduzir_erro_ytdlp(p.stderr)
    return preencher_ref_ytdlp(ref, info), None


# ---------------------------------------------------------------------------
# Mídia: download, conversão para JPG e miniaturas
# ---------------------------------------------------------------------------
def _tipo_por_cabecalho(cab):
    if cab.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if cab.startswith(b"\x89PNG"):
        return "png"
    if cab.startswith(b"GIF8"):
        return "gif"
    if cab[:4] == b"RIFF" and cab[8:12] == b"WEBP":
        return "webp"
    if cab[4:8] == b"ftyp":
        marca = cab[8:12]
        if marca in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return "heic"
        if marca in (b"avif", b"avis"):
            return "avif"
        return "mp4"
    return "bin"


def url_midia_permitida(url):
    """Só https em hosts da CDN da Meta (*.fbcdn.net, *.cdninstagram.com), sem barra invertida nem porta estranha."""
    if not url or "\\" in str(url):
        return False
    try:
        p = urllib.parse.urlsplit(str(url))
        host = (p.hostname or "").lower().rstrip(".")
        porta = p.port
    except ValueError:
        return False
    if p.scheme != "https" or p.username or p.password or porta not in (None, 443):
        return False
    return any(host.endswith(sufixo) and len(host) > len(sufixo) for sufixo in HOSTS_MIDIA_PERMITIDOS)


class _HostRecusado(Exception):
    pass


class _RedirecionamentoSeguro(urllib.request.HTTPRedirectHandler):
    """Confere a lista de hosts permitidos também em cada redirecionamento."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not url_midia_permitida(newurl):
            raise _HostRecusado(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _host_de(url):
    try:
        return urllib.parse.urlsplit(str(url)).hostname or _curto(url, 60)
    except ValueError:
        return _curto(url, 60)


def baixar_url(url, destino):
    """Baixa só da CDN da Meta, até LIMITE_DOWNLOAD_BYTES e TEMPO_MAX_DOWNLOAD segundos; apaga o parcial se abortar."""
    if not url_midia_permitida(url):
        raise ErroColeta(f"endereço de mídia recusado ({_host_de(url)}): só baixo da CDN do Instagram "
                         f"({', '.join('*' + h for h in HOSTS_MIDIA_PERMITIDOS)}) por https")
    req = urllib.request.Request(url, headers={"User-Agent": UA_NAVEGADOR})
    abridor = urllib.request.build_opener(_RedirecionamentoSeguro)
    inicio = time.monotonic()
    try:
        with abridor.open(req, timeout=TIMEOUT_DOWNLOAD) as r, open(destino, "wb") as f:
            tamanho = _int(r.headers.get("Content-Length"))
            if tamanho and tamanho > LIMITE_DOWNLOAD_BYTES:
                raise ErroColeta(f"arquivo grande demais ({tamanho // (1024 * 1024)} MB; o limite é "
                                 f"{LIMITE_DOWNLOAD_BYTES // (1024 * 1024)} MB)")
            total = 0
            while True:
                bloco = r.read(64 * 1024)
                if not bloco:
                    break
                total += len(bloco)
                if total > LIMITE_DOWNLOAD_BYTES:
                    raise ErroColeta(f"arquivo passou de {LIMITE_DOWNLOAD_BYTES // (1024 * 1024)} MB; download cancelado")
                if time.monotonic() - inicio > TEMPO_MAX_DOWNLOAD:
                    raise ErroColeta(f"download demorou mais de {TEMPO_MAX_DOWNLOAD} s; cancelado")
                f.write(bloco)
    except ErroColeta:
        _remover(destino)
        raise
    except _HostRecusado as e:
        _remover(destino)
        raise ErroColeta(f"o link redirecionou para um endereço fora da CDN do Instagram ({_host_de(e.args[0])}); "
                         "recusado") from None
    except urllib.error.HTTPError as e:
        _remover(destino)
        if e.code in (403, 410):
            raise ErroColeta(f"o link da imagem expirou (HTTP {e.code}); rode a coleta de novo "
                             "(perfis/hashtags/links) e depois midia", interrompe=True) from None
        raise ErroColeta(f"falha ao baixar (HTTP {e.code})") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        _remover(destino)
        raise ErroColeta(f"falha ao baixar: {getattr(e, 'reason', None) or e}") from None
    if os.path.getsize(destino) == 0:
        _remover(destino)
        raise ErroColeta("o download veio vazio")


def _remover(caminho):
    try:
        os.remove(caminho)
    except OSError:
        pass


def _existente(pasta, nome):
    for ext in ("jpg", "png", "webp", "gif", "heic", "avif"):
        p = os.path.join(pasta, f"{nome}.{ext}")
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
    return None


def obter_imagem(url, pasta_midia, nome, com_ffmpeg):
    """Baixa url em pasta_midia/nome.jpg (converte webp/heic/vídeo com ffmpeg). Devolve o caminho final."""
    tmp = os.path.join(pasta_midia, nome + ".part")
    baixar_url(url, tmp)
    with open(tmp, "rb") as f:
        tipo = _tipo_por_cabecalho(f.read(16))
    destino = os.path.join(pasta_midia, nome + ".jpg")
    if tipo == "jpg":
        os.replace(tmp, destino)
        return destino
    if tipo == "bin":
        _remover(tmp)
        raise ErroColeta("o arquivo baixado não é imagem nem vídeo reconhecido; descartado")
    original = os.path.join(pasta_midia, f"{nome}.orig.{tipo}")
    os.replace(tmp, original)
    if com_ffmpeg:
        try:
            ff("-i", original, "-frames:v", "1", "-q:v", "2", destino)  # serve para imagem e para capa de vídeo
            _remover(original)
            return destino
        except subprocess.SubprocessError:
            _remover(destino)
            if tipo == "mp4":
                _remover(original)
                raise ErroColeta("o ffmpeg não conseguiu tirar a capa do vídeo") from None
            log(f"!! ffmpeg não converteu {nome} ({tipo}); mantendo o arquivo original")
    elif tipo == "mp4":
        _remover(original)
        raise ErroColeta("veio vídeo em vez de imagem e não há ffmpeg para tirar a capa")
    final = os.path.join(pasta_midia, f"{nome}.{tipo}")
    os.replace(original, final)
    return final


def fazer_mini(arquivo, pasta_mini, com_ffmpeg):
    base = os.path.splitext(os.path.basename(arquivo))[0]
    existente = _existente(pasta_mini, base)
    if existente:
        return existente
    if com_ffmpeg:
        destino = os.path.join(pasta_mini, base + ".jpg")
        try:
            ff("-i", arquivo, "-vf", f"scale={LARGURA_MINI}:-2", "-q:v", "3", destino)
            return destino
        except subprocess.SubprocessError:
            _remover(destino)
    destino = os.path.join(pasta_mini, os.path.basename(arquivo))
    shutil.copy(arquivo, destino)
    return destino


def baixar_midia(pasta, top=20, slides=10, ids=None):
    dados = carregar(pasta)
    refs = dados["referencias"]
    erros = []
    if ids:
        indice = {r["id"]: r for r in refs}
        alvo = []
        for i in ids:
            if i in indice:
                alvo.append(indice[i])
            else:
                erros.append(f"{_curto(i)}: id não está em {ARQ_REFS}")
    else:
        calcular_notas(dados)
        alvo = [r for r in refs if not r.get("incompleta") and (r.get("midia_urls") or r.get("capa_url"))
                and RE_ID_SEGURO.match(str(r["id"]))][:max(0, top)]
    # o id vira nome de arquivo: só letras, números, _ e -
    seguros = []
    for r in alvo:
        if RE_ID_SEGURO.match(str(r.get("id") or "")):
            seguros.append(r)
        else:
            erros.append(f"{_curto(r.get('id'))}: id inválido para nome de arquivo; pulado")
    alvo = seguros
    if not alvo:
        salvar(pasta, dados)
        return dict(contagem(dados), pasta=pasta, novos=0, posts=0, imagens=0,
                    erros=erros or ["nenhuma referência com mídia para baixar"])
    com_ffmpeg = ffmpeg_disponivel()
    pasta_midia = os.path.join(pasta, "midia")
    pasta_mini = os.path.join(pasta_midia, "mini")
    os.makedirs(pasta_mini, exist_ok=True)
    baixadas, posts_ok = 0, 0
    for r in alvo:
        urls = r.get("midia_urls") or ([r["capa_url"]] if r.get("capa_url") else [])
        if not urls:
            erros.append(f"{r['id']}: sem URL de mídia (rode a coleta de novo)")
            continue
        log(f">> Mídia de {r['id']} ({min(len(urls), slides)} imagem(ns))")
        midia, mini = [], []
        for n, url in enumerate(urls[:max(1, slides)], 1):
            nome = f"{r['id']}-{n:02d}"
            try:
                arq = _existente(pasta_midia, nome)
                if not arq:
                    arq = obter_imagem(url, pasta_midia, nome, com_ffmpeg)
                    baixadas += 1
                pm = fazer_mini(arq, pasta_mini, com_ffmpeg)
            except ErroColeta as e:
                erros.append(f"{r['id']} imagem {n:02d}: {e}")
                if e.interrompe:
                    break
                continue
            midia.append("midia/" + os.path.basename(arq))
            mini.append("midia/mini/" + os.path.basename(pm))
        if midia:
            r["midia"], r["mini"] = midia, mini
            posts_ok += 1
    salvar(pasta, dados)
    return dict(contagem(dados), pasta=pasta, novos=baixadas, erros=erros, posts=posts_ok,
                imagens=sum(len(r.get("midia") or []) for r in alvo))


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------
def nome_de_pasta(tema, quando=None):
    prefixo = (quando or agora()).strftime("%Y-%m-%d") + " - "
    limpo = re.sub(r"\s+", " ", re.sub(r"[^\w \-.,!]", "", tema)).strip()
    maximo = 60 - len(prefixo)
    if len(limpo) > maximo:
        corte = limpo[:maximo].rfind(" ")
        limpo = limpo[: corte if corte >= 15 else maximo]
    limpo = limpo.strip().rstrip(".,!- ")  # o Windows não aceita pasta terminada em ponto
    return prefixo + (limpo or "garimpo")


def pasta_livre(base, nome):
    destino = os.path.join(base, nome)
    n = 2
    while os.path.exists(destino):
        destino = os.path.join(base, f"{nome} ({n})")
        n += 1
    os.makedirs(destino)
    return destino


def criar_garimpo(tema, saida=None):
    tema = " ".join(str(tema or "").split())
    if not tema:
        raise ErroFatal('informe o tema, por exemplo: garimpo.py novo "clareamento dental"')
    if not saida:
        saida = "/mnt/user-data/outputs" if os.path.isdir("/mnt/user-data/outputs") else os.path.join(os.getcwd(), "Garimpo")
    os.makedirs(saida, exist_ok=True)
    destino = pasta_livre(os.path.abspath(saida), nome_de_pasta(tema))
    salvar(destino, dados_vazios(tema))
    return destino


def cmd_novo(args):
    destino = criar_garimpo(args.tema, args.saida)
    log(f"PASTA={destino}")
    return {"pasta": destino, "novos": 0, "total": 0, "erros": []}


def resumo_status(cfg_arquivo=None, cfg=None):
    arq = cfg_arquivo if cfg_arquivo is not None else ler_config_arquivo()
    cfg = cfg if cfg is not None else config_efetiva()
    contas = arq.get("contas") or []
    padrao = cfg.get("conta_padrao")
    usuario_padrao = next((c.get("username") for c in contas if str(c.get("ig_user_id")) == str(padrao)), None)
    dias_token = dias_expira = None
    if not cfg.get("_token_do_ambiente"):
        salvo = ler_iso(arq.get("token_salvo_em"))
        if salvo and arq.get("token"):
            dias_token = max(0, (agora() - salvo).days)
        expira = ler_iso(arq.get("token_expira_em"))
        if expira and arq.get("token"):
            dias_expira = (expira - agora()).days
    recentes = hashtags_recentes(arq)
    return {
        "api_configurada": api_pronta(cfg), "contas": contas, "conta_padrao": padrao,
        "conta_padrao_usuario": usuario_padrao, "dias_desde_token": dias_token, "dias_para_expirar": dias_expira,
        "token_do_ambiente": bool(cfg.get("_token_do_ambiente")), "versao_api": cfg.get("versao_api"),
        "hashtags_7d_usadas": len(recentes), "hashtags_7d_restantes": max(0, LIMITE_HASHTAGS_7D - len(recentes)),
        "hashtags_fonte": "local", "config": caminho_config(),
    }


def _quando_epoch(v):
    v = _int(v)
    return datetime.fromtimestamp(v, timezone.utc).astimezone() if v else None


def testar_api(cfg, erros):
    """status --testar: validade real e permissões do token (debug_token), conta padrão e x/30 real."""
    tok, ver, uid = cfg["token"], cfg.get("versao_api"), cfg.get("conta_padrao")
    res = {"teste_api": "ok", "hashtags_fonte": "local"}
    log("== Teste da API ==")
    try:
        d = (graph_get(EP_DEBUG_TOKEN, {"input_token": tok}, token=tok, versao=ver) or {}).get("data") or {}
    except ErroGraph as e:
        erros.append(f"teste do token falhou: {traduzir_erro_graph(e)}")
        res["teste_api"] = "falhou"
        return res
    res["token_valido"] = bool(d.get("is_valid"))
    escopos = [str(x) for x in d.get("scopes") or []]
    res["escopos"] = escopos
    res["escopos_faltando"] = [x for x in ESCOPOS_NECESSARIOS if x not in escopos]
    expira, acesso = _quando_epoch(d.get("expires_at")), _quando_epoch(d.get("data_access_expires_at"))
    if "expires_at" in d:
        res["token_expira_em"] = expira.isoformat(timespec="seconds") if expira else None
        res["dias_para_expirar"] = (expira - agora()).days if expira else None
    if acesso:
        res["acesso_dados_expira_em"] = acesso.isoformat(timespec="seconds")
    log("Token: " + ("válido" if res["token_valido"] else "INVÁLIDO")
        + (f"; expira em {expira:%d/%m/%Y} (~{res['dias_para_expirar']} dia(s))" if expira
           else "; sem data de expiração" if "expires_at" in d else "")
        + (f"; acesso aos dados até {acesso:%d/%m/%Y}" if acesso else ""))
    log("Permissões: " + (", ".join(escopos) or "(nenhuma informada)"))
    if not res["token_valido"]:
        erros.append("o token não é mais válido: rode `garimpo.py configurar` de novo")
        res["teste_api"] = "falhou"
    if res["escopos_faltando"]:
        erros.append(f"faltam permissões no token: {', '.join(res['escopos_faltando'])}; gere outro com "
                     f"{PERMISSOES_NECESSARIAS} e rode `garimpo.py configurar` de novo")
    if not uid or not res["token_valido"]:
        return res
    try:
        r = graph_get(str(uid), {"fields": CAMPOS_TESTE}, token=tok, versao=ver)
        log(f"Conta padrão responde: @{r.get('username')}")
    except ErroGraph as e:
        erros.append(f"a conta padrão não respondeu: {traduzir_erro_graph(e)}")
        res["teste_api"] = "falhou"
        return res
    try:
        r = graph_get(EP_HASHTAGS_RECENTES.format(ig_user_id=uid), {"limit": LIMITE_HASHTAGS_7D}, token=tok, versao=ver)
        n = len((r or {}).get("data") or [])
        res.update({"hashtags_fonte": "api", "hashtags_7d_usadas": n,
                    "hashtags_7d_restantes": max(0, LIMITE_HASHTAGS_7D - n)})
        log(f"Hashtags nos últimos 7 dias (pela API): {n}/{LIMITE_HASHTAGS_7D}")
    except ErroGraph as e:
        msg = traduzir_erro_graph(e, "hashtag")
        if msg == MSG_ACESSO_PUBLICO:
            res["hashtags_api"] = "indisponivel"
            erros.append(msg)
        else:
            log(f"!! não deu para ler as hashtags da semana pela API ({msg}); vale o registro local")
    return res


def cmd_status(args):
    cfg = config_efetiva()
    s = resumo_status(ler_config_arquivo(), cfg)
    erros = []
    log("== Garimpo do Instagram: status ==")
    if s["api_configurada"]:
        log(f"API da Meta: LIGADA (Graph API {s['versao_api']})")
        nome = f"@{s['conta_padrao_usuario']}" if s["conta_padrao_usuario"] else "(conta definida por IG_USER_ID)"
        log(f"Conta padrão: {nome} — id {s['conta_padrao']}")
    else:
        log("API da Meta: DESLIGADA — perfis/hashtags ficam indisponíveis; use busca na web + links,")
        log("  ou peça para a pessoa rodar `garimpo.py configurar` no terminal dela.")
    if s["contas"]:
        log("Contas encontradas: " + ", ".join(f"@{c.get('username')} (Página {c.get('pagina')})" for c in s["contas"]))
    if s["token_do_ambiente"]:
        log("Token: vindo da variável de ambiente IG_GRAPH_TOKEN")
    elif s["dias_desde_token"] is not None:
        linha = f"Token: salvo há {s['dias_desde_token']} dia(s)"
        if s["dias_para_expirar"] is not None:
            linha += f"; expira em ~{s['dias_para_expirar']} dia(s)"
            if s["dias_para_expirar"] <= 7:
                erros.append("o token da Meta expira em breve: rode `garimpo.py configurar` de novo")
        log(linha)
    log(f"Hashtags nos últimos 7 dias: {s['hashtags_7d_usadas']}/{LIMITE_HASHTAGS_7D} "
        f"(restam {s['hashtags_7d_restantes']})")
    log(f"Configuração: {s['config']}")
    if getattr(args, "testar", False):
        if cfg.get("token"):
            s.update(testar_api(cfg, erros))
        else:
            log("Teste da API: não há token para testar")
            s["teste_api"] = "sem_token"
    for e in erros:
        log(f"!! {e}")
    return dict({"pasta": None, "novos": 0, "total": 0, "erros": erros}, **s)


def _limpar_token(t):
    t = (t or "").strip().strip("\"'")
    for prefixo in ("access_token=", "Bearer ", "bearer "):
        if t.startswith(prefixo):
            t = t[len(prefixo):]
    return t.strip()


DICA_COLAR = ("No PowerShell, cole com o BOTÃO DIREITO do mouse: em campo escondido o Ctrl+V às vezes "
              "envia um caractere invisível em vez do texto. Rode configurar de novo.")


def problema_no_colado(valor, nome, minimo, formato):
    """Diz por que o texto colado não parece válido, sem nunca mostrar o conteúdo (só o tamanho)."""
    invisivel = any(ord(c) < 32 or ord(c) == 127 for c in valor)
    if invisivel or len(valor) < minimo or not re.fullmatch(formato, valor):
        detalhe = f"{len(valor)} caractere(s)" + (", com caractere invisível" if invisivel else "")
        return f"o {nome} colado não parece completo ({detalhe}). {DICA_COLAR}"
    return None


def terminal_interativo():
    """True só quando stdin e stdout são um terminal de verdade (uma pessoa digitando).

    No Windows, isatty() também é True para o dispositivo NUL; por isso confere o modo de console.
    """
    for fluxo in (sys.stdin, sys.stdout):
        try:
            if fluxo is None or not fluxo.isatty():
                return False
        except (AttributeError, ValueError):
            return False
        if os.name == "nt":
            try:
                import ctypes
                import msvcrt
                modo = ctypes.c_uint32()
                alca = msvcrt.get_osfhandle(fluxo.fileno())
                if not ctypes.windll.kernel32.GetConsoleMode(ctypes.c_void_p(alca), ctypes.byref(modo)):
                    return False
            except Exception:  # noqa: BLE001
                return False
    return True


def cmd_configurar(args):
    script = os.path.abspath(__file__)
    if not terminal_interativo():
        raise ErroFatal("configurar é interativo e precisa ser rodado pela própria pessoa, no terminal dela "
                        f"(o token não deve passar pela conversa). Abra um terminal e rode: python \"{script}\" configurar")
    arq = ler_config_arquivo(tolerante=True)
    versao = (os.environ.get("IG_GRAPH_VERSION") or arq.get("versao_api") or GRAPH_VERSAO_PADRAO).strip()
    if not re.fullmatch(r"v\d+\.\d+", versao):
        versao = GRAPH_VERSAO_PADRAO
    log("== Configurar a API da Meta (Instagram Graph API) ==")
    log("Você vai colar o token de acesso do Facebook Login (começa com EAA). Ele não aparece na tela")
    log("e fica salvo só neste computador. Passo a passo: referencias/configurar-api-meta.md da skill.")
    log("")
    token = _limpar_token(getpass.getpass("Token de acesso (não aparece ao digitar/colar): "))
    if not token:
        raise ErroFatal("nenhum token informado; nada foi alterado")
    registrar_segredo(token)
    erro_colado = problema_no_colado(token, "token", 100, r"[A-Za-z0-9_.|-]+")
    if erro_colado:
        raise ErroFatal(erro_colado + " Um token da Meta tem uns 200 caracteres e começa com EAA. Nada foi alterado.")
    if token.startswith(PREFIXOS_TOKEN_LOGIN_INSTAGRAM):
        log("!! Esse token parece ser do 'Login do Instagram' (graph.instagram.com). O garimpo precisa do token")
        log("   do Facebook Login (Graph API Explorer / app com Facebook Login), que costuma começar com EAA.")
    app_id = input("App ID (opcional, para trocar por token de 60 dias; Enter para pular): ").strip()
    app_secret = ""
    if app_id:
        app_secret = getpass.getpass("App Secret (não aparece ao digitar/colar; Enter para pular): ").strip()
        registrar_segredo(app_secret)
        if app_secret:
            erro_colado = problema_no_colado(app_secret, "App Secret", 32, r"[0-9a-fA-F]{32}")
            if erro_colado:
                raise ErroFatal(erro_colado + " O App Secret tem 32 letras e números. Nada foi alterado.")
    tipo, expira = "informado", None
    if app_id and app_secret:
        try:
            r = graph_get(EP_TROCA_TOKEN, {"grant_type": "fb_exchange_token", "client_id": app_id,
                                           "client_secret": app_secret, "fb_exchange_token": token}, versao=versao)
            if r.get("access_token"):
                token = r["access_token"]
                registrar_segredo(token)
                tipo = "longa_duracao"
                if _int(r.get("expires_in")):
                    expira = (agora() + timedelta(seconds=_int(r["expires_in"]))).isoformat(timespec="seconds")
                log(">> Token trocado por um de longa duração" + (f" (expira em {expira[:10]})" if expira else "") + ".")
        except ErroGraph as e:
            log(f"!! Não consegui trocar por token de longa duração: {traduzir_erro_graph(e, 'configurar')}")
            log("   Sigo com o token informado (pode durar só algumas horas).")
    elif not app_id:
        log(">> Sem App ID/Secret: o token fica como veio (o do Graph API Explorer dura ~1-2 h; o estendido, ~60 dias).")

    log(">> Procurando contas do Instagram profissionais ligadas às suas Páginas...")
    contas, apos = [], None
    for _ in range(MAX_PAGINAS):
        try:
            r = graph_get(EP_CONTAS, {"fields": CAMPOS_CONTAS, "limit": LIMITE_PAGINA_CONTAS, "after": apos},
                          token=token, versao=versao)
        except ErroGraph as e:
            raise ErroFatal(traduzir_erro_graph(e, "configurar")) from None
        for pagina in r.get("data") or []:
            iga = pagina.get("instagram_business_account") or {}
            if iga.get("id"):
                contas.append({"ig_user_id": str(iga["id"]), "username": iga.get("username"),
                               "pagina": pagina.get("name")})
        prox = ((r.get("paging") or {}).get("cursors") or {}).get("after")
        if not r.get("data") or not prox or prox == apos or not (r.get("paging") or {}).get("next"):
            break
        apos = prox
    if not contas:
        raise ErroFatal("nenhuma conta do Instagram profissional ligada às Páginas deste token. Confira: (1) a conta "
                        "do Instagram é Business ou Creator; (2) ela está conectada a uma Página do Facebook; "
                        f"(3) o token tem as permissões {PERMISSOES_NECESSARIAS}. Nada foi salvo")
    padrao = contas[0]["ig_user_id"]
    if len(contas) > 1:
        log("Contas encontradas:")
        for i, c in enumerate(contas, 1):
            log(f"  {i}) @{c['username']} (Página {c['pagina']})")
        escolha = input(f"Qual será a conta padrão? [1-{len(contas)}, Enter = 1]: ").strip() or "1"
        if escolha.isdigit() and 1 <= int(escolha) <= len(contas):
            padrao = contas[int(escolha) - 1]["ig_user_id"]
    novo = {
        "token": token, "token_salvo_em": agora_iso(), "token_tipo": tipo, "token_expira_em": expira,
        "versao_api": versao, "contas": contas, "conta_padrao": padrao,
        "hashtags_usadas": hashtags_recentes(arq),
    }
    gravar_config_arquivo(novo)
    usuario = next(c["username"] for c in contas if c["ig_user_id"] == padrao)
    log(f">> Pronto! Configuração salva em {caminho_config()}")
    log(f"   Conta padrão: @{usuario}. Volte para o Claude e peça o garimpo.")
    return {"pasta": None, "novos": 0, "total": 0, "erros": [], "api_configurada": True, "contas": contas,
            "conta_padrao": padrao, "config": caminho_config()}


def executar_perfis(pasta, nomes, limite=30, dias=90, formatos=None, cfg=None):
    cfg = cfg if cfg is not None else config_efetiva()
    dados = carregar(pasta)
    erros, resumo, alvos, novos = [], [], [], 0
    for n in nomes:
        try:
            u = normalizar_perfil(n)
            if u not in alvos:
                alvos.append(u)
        except ValueError as e:
            erros.append(f"{_curto(n)}: {e}")
            dados["buscas"].append(nova_busca("perfil", _curto(n), 0, str(e)))
    sem_api = None if api_pronta(cfg) else MSG_SEM_API
    interrompido = None
    for u in alvos:
        if sem_api or interrompido:
            msg = sem_api or f"não coletado: {interrompido}"
            erros.append(f"@{u}: {msg}")
            dados["buscas"].append(nova_busca("perfil", "@" + u, 0, msg))
            resumo.append({"perfil": u, "qtd": 0, "erro": msg})
            continue
        log(f">> Coletando @{u} pela API da Meta...")
        try:
            info, refs = coletar_perfil(u, cfg, limite, dias, formatos)
        except ErroColeta as e:
            erros.append(f"@{u}: {e}")
            dados["buscas"].append(nova_busca("perfil", "@" + u, 0, str(e)))
            resumo.append({"perfil": u, "qtd": 0, "erro": str(e)})
            if e.interrompe:
                interrompido = str(e)
            continue
        mesclar_perfil(dados, u, info)
        n_novos = mesclar(dados, refs)
        novos += n_novos
        dados["buscas"].append(nova_busca("perfil", "@" + u, len(refs), None))
        resumo.append({"perfil": u, "qtd": len(refs), "erro": None})
        log(f"   {len(refs)} posts ({n_novos} novos)")
        salvar(pasta, dados)
    salvar(pasta, dados)
    return dict(contagem(dados), pasta=pasta, novos=novos, erros=erros, perfis=resumo, api_configurada=not sem_api)


def executar_hashtags(pasta, tags, tipo="top", limite=50, dias=90, formatos=None, cfg=None):
    cfg = cfg if cfg is not None else config_efetiva()
    dados = carregar(pasta)
    erros, resumo, alvos, novos = [], [], [], 0
    for t in tags:
        try:
            h = normalizar_hashtag(t)
            if h not in alvos:
                alvos.append(h)
        except ValueError as e:
            erros.append(f"{_curto(t)}: {e}")
            dados["buscas"].append(nova_busca("hashtag", _curto(t), 0, str(e), tipo=tipo))
    sem_api = None if api_pronta(cfg) else MSG_SEM_API
    avisos = []
    if tipo == "recente" and alvos and not sem_api:
        avisos.append("recent_media só traz posts das últimas 24 h (fora de ordem); --dias foi ignorado")
        log(f"!! {avisos[-1]}")
    interrompido = None
    limite_semana = None  # depois do limite semanal, só as hashtags já usadas nos últimos 7 dias seguem
    for h in alvos:
        ja_usada = h in {x["tag"] for x in hashtags_recentes(ler_config_arquivo(tolerante=True))}
        bloqueio = sem_api or interrompido or (limite_semana if not ja_usada else None)
        if bloqueio:
            msg = sem_api or f"não coletada: {bloqueio}"
            erros.append(f"#{h}: {msg}")
            dados["buscas"].append(nova_busca("hashtag", "#" + h, 0, msg, tipo=tipo))
            resumo.append({"hashtag": h, "qtd": 0, "erro": msg})
            continue
        log(f">> Coletando #{h} ({tipo}) pela API da Meta...")
        try:
            refs = coletar_hashtag(h, cfg, tipo, limite, dias, formatos)
        except ErroColeta as e:
            erros.append(f"#{h}: {e}")
            dados["buscas"].append(nova_busca("hashtag", "#" + h, 0, str(e), tipo=tipo))
            resumo.append({"hashtag": h, "qtd": 0, "erro": str(e)})
            if e.motivo == "limite_hashtag":
                limite_semana = limite_semana or str(e)
            elif e.interrompe:
                interrompido = str(e)
            continue
        n_novos = mesclar(dados, refs)
        novos += n_novos
        dados["buscas"].append(nova_busca("hashtag", "#" + h, len(refs), None, tipo=tipo))
        resumo.append({"hashtag": h, "qtd": len(refs), "erro": None})
        log(f"   {len(refs)} posts ({n_novos} novos)")
        salvar(pasta, dados)
    salvar(pasta, dados)
    recentes = hashtags_recentes(ler_config_arquivo(tolerante=True))
    return dict(contagem(dados), pasta=pasta, novos=novos, erros=erros, hashtags=resumo, api_configurada=not sem_api,
                avisos=avisos, hashtags_7d_usadas=len(recentes),
                hashtags_7d_restantes=max(0, LIMITE_HASHTAGS_7D - len(recentes)))


def _mesmo_arquivo(a, b):
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except OSError:
        return False


def ler_arquivo_links(caminho):
    """Só os trechos https?://... do arquivo. Devolve (urls, linhas_sem_link); nunca ecoa o conteúdo."""
    if _mesmo_arquivo(caminho, caminho_config()):
        raise ErroFatal("esse arquivo é a configuração do garimpo (tem o token da Meta); passe um arquivo só com links")
    try:
        tamanho = os.path.getsize(caminho)
    except OSError as e:
        raise ErroFatal(f"não consegui ler {_curto(caminho)}: {e.strerror or e}") from None
    if tamanho > LIMITE_ARQUIVO_LINKS:
        raise ErroFatal(f"{_curto(caminho)} tem mais de {LIMITE_ARQUIVO_LINKS // 1024} KB; passe um arquivo só com links")
    try:
        with open(caminho, encoding="utf-8-sig", errors="replace") as f:
            linhas = f.read().splitlines()
    except OSError as e:
        raise ErroFatal(f"não consegui ler {_curto(caminho)}: {e.strerror or e}") from None
    urls, sem_link = [], 0
    for linha in linhas:
        linha = linha.strip()
        if not linha or linha.startswith("#"):
            continue
        achadas = re.findall(r"https?://\S+", linha)
        if achadas:
            urls.extend(achadas)
        else:
            sem_link += 1
    return urls, sem_link


def executar_links(pasta, urls, arquivo=None):
    entradas = list(urls or [])
    sem_link = 0
    if arquivo:
        do_arquivo, sem_link = ler_arquivo_links(arquivo)
        entradas += do_arquivo
    if not entradas:
        raise ErroFatal("nenhum link informado: passe as URLs dos posts ou --arquivo links.txt")
    dados = carregar(pasta)
    erros, validos, vistos, novos, lidos = [], [], set(), 0, 0
    if sem_link:
        erros.append(f"{sem_link} linha(s) de --arquivo sem link foram ignoradas")
    for u in entradas:
        ident = id_do_link(u)
        if not ident:
            msg = "não é link de post do Instagram (aceito: /p/, /reel/, /reels/, /tv/)"
            erros.append(f"{_curto(u)}: {msg}")
            dados["buscas"].append(nova_busca("link", _curto(u), 0, msg))
            continue
        if ident[0] not in vistos:
            vistos.add(ident[0])
            validos.append(ident)
    if validos:
        cmd = comando_ytdlp()
        for i, (_sc, link) in enumerate(validos):
            if i:
                time.sleep(PAUSA_LINKS)
            log(f">> Lendo {link} ({i + 1}/{len(validos)})")
            ref, erro = coletar_link(link, cmd)
            novos += mesclar(dados, [ref])
            dados["buscas"].append(nova_busca("link", link, 0 if erro else 1, erro))
            if erro:
                erros.append(f"{link}: {erro}")
                log(f"!! {erro}")
            else:
                lidos += 1
            salvar(pasta, dados)
    salvar(pasta, dados)
    return dict(contagem(dados), pasta=pasta, novos=novos, erros=erros, lidos=lidos,
                rejeitados=sum(1 for u in entradas if not id_do_link(u)))


def executar_painel(pasta):
    if not os.path.isdir(pasta):
        raise ErroFatal(f"pasta não encontrada: {pasta}")
    if not os.path.exists(os.path.join(pasta, ARQ_REFS)):
        raise ErroFatal(f"{ARQ_REFS} não existe em {pasta}: colete referências antes de gerar o painel")
    pasta_script = os.path.dirname(os.path.abspath(__file__))
    alvo = os.path.join(pasta_script, "painel.py")
    if not os.path.exists(alvo):
        raise ErroFatal(f"não encontrei painel.py em {pasta_script}: a skill está incompleta (reinstale instagram-garimpo)")
    if pasta_script not in sys.path:
        sys.path.insert(0, pasta_script)
    try:
        spec = importlib.util.spec_from_file_location("painel", alvo)
        modulo = importlib.util.module_from_spec(spec)
        sys.modules["painel"] = modulo
        spec.loader.exec_module(modulo)
    except Exception as e:  # noqa: BLE001
        raise ErroFatal(f"erro ao carregar painel.py: {e}") from None
    if not hasattr(modulo, "gerar_painel"):
        raise ErroFatal("painel.py não tem a função gerar_painel(pasta)")
    avisos = []
    try:
        aceita_avisos = True
        try:
            parametros = inspect.signature(modulo.gerar_painel).parameters
            aceita_avisos = "avisos" in parametros or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in parametros.values())
        except (TypeError, ValueError):
            pass
        if aceita_avisos:
            try:
                html = modulo.gerar_painel(pasta, avisos=avisos)
            except TypeError as e:
                if "avisos" not in str(e):
                    raise
                html = modulo.gerar_painel(pasta)  # assinatura antiga
        else:
            html = modulo.gerar_painel(pasta)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(mascarar(traceback.format_exc()))
        raise ErroFatal(f"painel.py falhou ao gerar o painel: {e}") from None
    dados = carregar(pasta)
    for a in avisos:
        log(f"!! {a}")
    log(f"PAINEL={html}")
    return dict(contagem(dados), pasta=pasta, novos=0, erros=[str(a) for a in avisos], painel=html)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def tipo_formatos(valor):
    itens = set()
    for x in str(valor).split(","):
        x = x.strip().lower()
        if x:
            itens.add(APELIDOS_FORMATO.get(x, x))
    invalidos = itens - set(FORMATOS)
    if invalidos:
        raise argparse.ArgumentTypeError(f"formato inválido: {', '.join(sorted(invalidos))} (use {', '.join(FORMATOS)})")
    return itens or None


def inteiro_min(minimo):
    def converter(valor):
        try:
            n = int(valor)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(f"precisa ser um número inteiro (recebi {_curto(valor, 20)!r})") from None
        if n < minimo:
            raise argparse.ArgumentTypeError(f"precisa ser um número inteiro maior ou igual a {minimo} (recebi {n})")
        return n
    return converter


class ErroArgumentos(Exception):
    """Erro de linha de comando (o argparse sairia sem a linha RESULTADO=)."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        raise ErroArgumentos(f"{self.prog}: {message}")


def tipo_ids(valor):
    ids = []
    for x in str(valor).split(","):
        x = x.strip()
        if x:
            ident = id_do_link(x)
            ids.append(ident[0] if ident else x)
    return ids


def construir_parser():
    ap = _Parser(prog="garimpo.py", description="Garimpo de referências do Instagram")
    sub = ap.add_subparsers(dest="comando", metavar="comando")
    sub.required = True

    p = sub.add_parser("configurar", help="(a pessoa roda no terminal dela) salva o token da Meta")
    p.set_defaults(func=cmd_configurar)

    p = sub.add_parser("status", help="mostra se a API está ligada, contas e limite de hashtags")
    p.add_argument("--testar", action="store_true", help="faz uma chamada de teste na API")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("novo", help="cria a pasta do garimpo")
    p.add_argument("tema")
    p.add_argument("--saida")
    p.set_defaults(func=cmd_novo)

    p = sub.add_parser("perfis", help="coleta posts de perfis profissionais (API da Meta)")
    p.add_argument("--pasta", required=True)
    p.add_argument("nomes", nargs="+")
    p.add_argument("--limite", type=inteiro_min(1), default=30)
    p.add_argument("--dias", type=inteiro_min(0), default=90, help="0 = sem filtro de data")
    p.add_argument("--formatos", type=tipo_formatos)
    p.set_defaults(func=lambda a: executar_perfis(os.path.abspath(a.pasta), a.nomes, a.limite, a.dias, a.formatos))

    p = sub.add_parser("hashtags", help="coleta posts de hashtags (API da Meta)")
    p.add_argument("--pasta", required=True)
    p.add_argument("tags", nargs="+")
    p.add_argument("--tipo", choices=["top", "recente"], default="top")
    p.add_argument("--limite", type=inteiro_min(1), default=50)
    p.add_argument("--dias", type=inteiro_min(0), default=90, help="0 = sem filtro de data")
    p.add_argument("--formatos", type=tipo_formatos)
    p.set_defaults(func=lambda a: executar_hashtags(os.path.abspath(a.pasta), a.tags, a.tipo, a.limite, a.dias,
                                                    a.formatos))

    p = sub.add_parser("links", help="lê posts avulsos (links da busca na web) via yt-dlp")
    p.add_argument("--pasta", required=True)
    p.add_argument("urls", nargs="*")
    p.add_argument("--arquivo")
    p.set_defaults(func=lambda a: executar_links(os.path.abspath(a.pasta), a.urls, a.arquivo))

    p = sub.add_parser("midia", help="baixa imagens e miniaturas das melhores referências")
    p.add_argument("--pasta", required=True)
    p.add_argument("--top", type=inteiro_min(1), default=20)
    p.add_argument("--slides", type=inteiro_min(1), default=10)
    p.add_argument("--ids", type=tipo_ids)
    p.set_defaults(func=lambda a: baixar_midia(os.path.abspath(a.pasta), a.top, a.slides, a.ids))

    p = sub.add_parser("painel", help="gera painel.html na pasta")
    p.add_argument("--pasta", required=True)
    p.set_defaults(func=lambda a: executar_painel(os.path.abspath(a.pasta)))
    return ap


def main(argv=None):
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    del _AVISOS[:]
    armar_mascara()  # antes de qualquer saída: o token salvo e o do ambiente já ficam mascarados
    try:
        args = construir_parser().parse_args(argv)
    except ErroArgumentos as e:
        log(f"!! {e}")
        emitir({"pasta": None, "novos": 0, "total": 0, "erros": [str(e)]})
        return 2
    except SystemExit as e:  # --help (código 0) segue igual
        if e.code in (0, None):
            return 0
        emitir({"pasta": None, "novos": 0, "total": 0, "erros": ["argumentos inválidos (veja --help)"]})
        return 2
    pasta = os.path.abspath(args.pasta) if getattr(args, "pasta", None) else None
    try:
        resultado = args.func(args)
        codigo = 0
    except ErroFatal as e:
        log(f"!! {e}")
        resultado, codigo = {"pasta": pasta, "novos": 0, "total": 0, "erros": [str(e)]}, 1
    except KeyboardInterrupt:
        log("!! Cancelado.")
        resultado, codigo = {"pasta": pasta, "novos": 0, "total": 0, "erros": ["cancelado"]}, 130
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(mascarar(traceback.format_exc()))
        log(f"!! erro inesperado: {e}")
        resultado, codigo = {"pasta": pasta, "novos": 0, "total": 0, "erros": [f"erro inesperado: {e}"]}, 1
    resultado.setdefault("erros", [])
    resultado["erros"] = list(resultado["erros"]) + [a for a in _AVISOS if a not in resultado["erros"]]
    emitir(resultado)
    return codigo


if __name__ == "__main__":
    sys.exit(main())
