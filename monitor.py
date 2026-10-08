"""
monitor.py — avisa você quando aparece oferta de console PS5 nos grupos de uma
pasta do seu Telegram.

    sua conta (Telethon, API oficial do Telegram) lê as mensagens novas dos
    chats da pasta -> filtro (cita PS5? parece o console? qual o preço?) ->
    o seu bot (Bot API) te manda o aviso, com o link da mensagem original.

Uso:
    python monitor.py                     roda o monitor (na 1ª vez pede login)
    python monitor.py --pastas            lista as pastas do seu Telegram
    python monitor.py --testar "texto"    mostra o que o filtro acha de um texto
"""

import argparse
import asyncio
import csv
import html
import json
import re
import sys
import time
import tomllib
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient, events, utils
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import Channel, MessageEntityTextUrl, MessageEntityUrl, User

PASTA_PROJETO = Path(__file__).parent
ARQUIVO_CONFIG = PASTA_PROJETO / "config.toml"
ARQUIVO_ESTADO = PASTA_PROJETO / "estado.json"
ARQUIVO_HISTORICO = PASTA_PROJETO / "ofertas.csv"
ARQUIVO_SESSAO = PASTA_PROJETO / "sessao"  # o Telethon acrescenta ".session"

INTERVALO_PASTA_S = 300      # relê a pasta: pega grupos que você adicionou/removeu
INTERVALO_VARREDURA_S = 180  # rede de segurança para mensagens que o push não entregou
# A página de recentes do Pelando mostra ~1 h de promoções; de 3 em 3 minutos
# nenhuma sai da primeira página antes de ser vista.
INTERVALO_PELANDO_S = 180
JANELA_REPETIDA_S = 12 * 3600  # a mesma oferta repostada em outro grupo não avisa de novo


def log(texto: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {texto}", flush=True)


# --------------------------------------------------------------------------- #
# Filtro
# --------------------------------------------------------------------------- #
def normalizar(texto: str) -> str:
    """Minúsculas, sem acentos e sem ™/® (que o NFKD transformaria em letras)."""
    texto = texto.replace("™", " ").replace("®", " ")
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", sem_acento.lower())


def compilar_termos(termos: list[str]) -> re.Pattern | None:
    """Uma regex que acha qualquer um dos termos como palavra inteira; espaços
    no termo valem como "espaço opcional" ("ps 5" acha "ps5" e "ps 5")."""
    partes = []
    for termo in termos:
        termo = normalizar(termo).strip()
        if termo:
            partes.append(re.escape(termo).replace(r"\ ", r"\s*"))
    if not partes:
        return None
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(partes) + r")(?![a-z0-9])")


_PRECO = re.compile(
    r"r\$\s*(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?"
    r"|(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?\s*reais"
)
# Valores que não são o preço do produto: parcelas, cupons, frete, cashback...
_PARCELA_ANTES = re.compile(r"(\d+\s*x\s*(de\s*)?|juros\s*(de\s*)?|parcelas?\s*(de\s*)?)$")
_DESCONTO_ANTES = re.compile(r"(cupom|desconto|economi\w*|cashback|frete|off|ganhe|bonus|credito)\s*(de\s*)?$")
_DESCONTO_DEPOIS = re.compile(r"^\s*(off|de desconto|de cashback|em cashback|de volta|de bonus|de credito)")


def extrair_precos(texto_normalizado: str) -> list[float]:
    precos = []
    t = texto_normalizado
    for m in _PRECO.finditer(t):
        inteiro, centavos = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        antes = t[max(0, m.start() - 16):m.start()]
        depois = t[m.end():m.end() + 16]
        if _PARCELA_ANTES.search(antes) or _DESCONTO_ANTES.search(antes) or _DESCONTO_DEPOIS.search(depois):
            continue
        valor = int(inteiro.replace(".", ""))
        if centavos:
            valor += int(centavos.ljust(2, "0")) / 100
        precos.append(float(valor))
    return precos


def reais(valor: float) -> str:
    return f"{valor:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


@dataclass
class Filtro:
    ps5: re.Pattern
    console: re.Pattern | None
    acessorio: re.Pattern | None
    bloqueados: re.Pattern | None
    preco_minimo_console: float
    preco_maximo: float


@dataclass
class Analise:
    oferta: bool
    preco: float | None
    motivo: str


def analisar(texto: str, f: Filtro) -> Analise:
    """Decide se a mensagem é oferta do console. A ordem importa:
    1. precisa citar PS5;
    2. termos bloqueados descartam sempre ("para ps5", VR2, Portal...);
    3. termos de acessório descartam, a menos que a mensagem também diga
       que é o console ("console", "slim"...);
    4. se há preços e todos são baixos demais para um console, é jogo/controle;
    5. sem preço nenhum, só passa se disser "console" (ou outro termo de console);
    6. por fim, o seu teto de preço."""
    t = normalizar(texto)
    if not f.ps5.search(t):
        return Analise(False, None, "não cita PS5")
    if f.bloqueados and (m := f.bloqueados.search(t)):
        return Analise(False, None, f"termo bloqueado: {m.group(0)!r}")
    console = f.console.search(t) if f.console else None
    acessorio = f.acessorio.search(t) if f.acessorio else None
    if acessorio and not console:
        return Analise(False, None, f"parece acessório: {acessorio.group(0)!r}")
    precos = extrair_precos(t)
    precos_console = [p for p in precos if p >= f.preco_minimo_console]
    if precos and not precos_console:
        return Analise(False, min(precos), f"R$ {reais(min(precos))} é barato demais para ser o console")
    if not precos_console and not console:
        return Analise(False, None, "sem preço e sem dizer que é o console")
    preco = min(precos_console) if precos_console else None
    if f.preco_maximo and preco and preco > f.preco_maximo:
        return Analise(False, preco, f"R$ {reais(preco)} passa do seu teto de R$ {reais(f.preco_maximo)}")
    return Analise(True, preco, "oferta de console PS5")


def chave_de_repeticao(texto: str, preco: float | None) -> str:
    """A mesma promoção circula por vários grupos, cada um com seus emojis e
    links de afiliado. Com preço: mesmo valor e mesmo modelo (Pro? digital?)
    contam como repetida. Sem preço: compara o texto sem os links."""
    t = normalizar(re.sub(r"https?://\S+", " ", texto))
    if preco:
        modelo = [nome for nome, padrao in (("pro", r"\bpro\b"), ("digital", r"digital")) if re.search(padrao, t)]
        return f"{preco:.0f}|{'+'.join(modelo) or 'padrao'}"
    return re.sub(r"[^a-z0-9$,.]+", " ", t).strip()[:200]


# --------------------------------------------------------------------------- #
# Configuração e estado
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    api_id: int
    api_hash: str
    pasta: str
    bot_token: str
    filtro: Filtro
    pelando_ativo: bool
    pelando_busca: str


def carregar_config(exigir_telegram: bool = True, exigir_bot: bool = True) -> Config:
    if not ARQUIVO_CONFIG.exists():
        sys.exit(f"Não achei {ARQUIVO_CONFIG.name}. Ele precisa ficar na mesma pasta do monitor.py.")
    try:
        with open(ARQUIVO_CONFIG, "rb") as arquivo:
            dados = tomllib.load(arquivo)
    except tomllib.TOMLDecodeError as erro:
        sys.exit(f"Erro no {ARQUIVO_CONFIG.name}: {erro}. Confira aspas e vírgulas nessa linha.")

    tg = dados.get("telegram", {})
    bot = dados.get("bot", {})
    fl = dados.get("filtro", {})
    pl = dados.get("pelando", {})
    cfg = Config(
        api_id=int(tg.get("api_id") or 0),
        api_hash=str(tg.get("api_hash") or "").strip(),
        pasta=str(tg.get("pasta") or "").strip(),
        bot_token=str(bot.get("token") or "").strip(),
        filtro=Filtro(
            ps5=compilar_termos(fl.get("termos_ps5") or ["ps5", "playstation 5"]),
            console=compilar_termos(fl.get("termos_console", [])),
            acessorio=compilar_termos(fl.get("termos_acessorio", [])),
            bloqueados=compilar_termos(fl.get("termos_bloqueados", [])),
            preco_minimo_console=float(fl.get("preco_minimo_console", 1500)),
            preco_maximo=float(fl.get("preco_maximo", 0)),
        ),
        pelando_ativo=bool(pl.get("ativo", False)),
        pelando_busca=str(pl.get("busca") or "").strip(),
    )
    if exigir_telegram and (not cfg.api_id or not cfg.api_hash):
        sys.exit("Preencha api_id e api_hash no config.toml (LEIAME.md, passo 1).")
    if exigir_telegram and not cfg.pasta:
        sys.exit("Preencha o nome da pasta do Telegram no config.toml (campo pasta).")
    if exigir_bot and not cfg.bot_token:
        sys.exit("Preencha o token do seu bot no config.toml (LEIAME.md, passo 2).")
    return cfg


def carregar_estado() -> dict:
    try:
        return json.loads(ARQUIVO_ESTADO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def salvar_estado(estado: dict) -> None:
    ARQUIVO_ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")


def registrar_historico(preco: float | None, grupo: str, link: str | None, texto: str) -> None:
    novo = not ARQUIVO_HISTORICO.exists()
    # utf-8-sig: o Excel abre os acentos certinho
    with open(ARQUIVO_HISTORICO, "a", newline="", encoding="utf-8-sig") as arquivo:
        escritor = csv.writer(arquivo, delimiter=";")
        if novo:
            escritor.writerow(["data", "preco", "grupo", "link", "texto"])
        escritor.writerow([
            f"{datetime.now():%d/%m/%Y %H:%M}",
            reais(preco) if preco else "",
            grupo,
            link or "",
            " ".join(texto.split())[:300],
        ])


# --------------------------------------------------------------------------- #
# Bot de avisos (Bot API por HTTPS, sem dependência extra)
# --------------------------------------------------------------------------- #
def _chamar_bot(token: str, metodo: str, dados: dict) -> dict:
    pedido = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{metodo}",
        data=json.dumps(dados).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(pedido, timeout=20) as resposta:
            return json.load(resposta)
    except urllib.error.HTTPError as erro:
        try:
            return json.load(erro)
        except ValueError:
            return {"ok": False, "error_code": erro.code, "description": str(erro)}
    except OSError as erro:
        return {"ok": False, "error_code": 0, "description": str(erro)}


async def chamar_bot(token: str, metodo: str, dados: dict) -> dict:
    for _ in range(3):
        resposta = await asyncio.to_thread(_chamar_bot, token, metodo, dados)
        if resposta.get("error_code") == 429:  # limite de envio: espera o que o Telegram pedir
            await asyncio.sleep(resposta.get("parameters", {}).get("retry_after", 5))
            continue
        return resposta
    return resposta


# --------------------------------------------------------------------------- #
# Pelando (site): a mesma busca do alerta da extensão + a página de recentes
# --------------------------------------------------------------------------- #
PELANDO = "https://www.pelando.com.br"
_NAVEGADOR = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
# Título de cada cartão de promoção; o resto do cartão vem logo depois dele.
_TITULO_PELANDO = re.compile(
    r'<a href="(https://www\.pelando\.com\.br/d/[^"]+)" data-deal-id="([^"]+)" data-inactive="([^"]+)"[^>]*>([^<]*)</a>'
)


@dataclass
class PromoPelando:
    id: str
    titulo: str
    link: str
    inativa: bool
    preco: str
    loja: str
    quando: str
    temperatura: str


def ler_promocoes_pelando(pagina: str) -> list[PromoPelando]:
    """As páginas do Pelando vêm prontas do servidor (sem API pública); cada
    promoção é um cartão: título, há quanto tempo, loja, preço, temperatura."""
    texto_limpo = lambda trecho: " ".join(re.sub(r"<[^>]+>", " ", html.unescape(trecho)).split())
    titulos = list(_TITULO_PELANDO.finditer(pagina))
    promos = []
    for i, m in enumerate(titulos):
        cartao = pagina[m.end():titulos[i + 1].start() if i + 1 < len(titulos) else len(pagina)]
        preco = re.search(r'_deal-card-stamp[^>]*>(.*?)</span>', cartao, re.S)
        loja = re.search(r'Vendido por </span><strong>([^<]*)', cartao)
        quando = re.search(r'#Clock"></use></svg></i>([^<]*)<', cartao)
        temperatura = re.search(r'data-temperature-level=.*?<span>([^<]*)</span>', cartao, re.S)
        promos.append(PromoPelando(
            id=m.group(2),
            titulo=texto_limpo(m.group(4)),
            link=m.group(1),
            inativa=m.group(3) == "true",
            preco=texto_limpo(preco.group(1)) if preco else "",
            loja=texto_limpo(loja.group(1)) if loja else "",
            quando=texto_limpo(quando.group(1)) if quando else "",
            temperatura=texto_limpo(temperatura.group(1)) if temperatura else "",
        ))
    return promos


def _baixar(url: str) -> str:
    pedido = urllib.request.Request(url, headers={"User-Agent": _NAVEGADOR, "Accept-Language": "pt-BR"})
    with urllib.request.urlopen(pedido, timeout=25) as resposta:
        return resposta.read().decode("utf-8", "replace")


async def buscar_pelando(busca: str) -> list[PromoPelando]:
    paginas = [f"{PELANDO}/recentes"]
    if busca:
        paginas.append(f"{PELANDO}/busca/{urllib.parse.quote_plus(busca)}")
    promos: dict[str, PromoPelando] = {}
    for url in paginas:
        for promo in ler_promocoes_pelando(await asyncio.to_thread(_baixar, url)):
            promos.setdefault(promo.id, promo)
    return list(promos.values())


# --------------------------------------------------------------------------- #
# Telegram: pastas, links e o monitor em si
# --------------------------------------------------------------------------- #
def titulo_da_pasta(pasta) -> str | None:
    titulo = getattr(pasta, "title", None)
    if titulo is None:  # a pasta "Todas as conversas" não tem título
        return None
    return getattr(titulo, "text", titulo)


async def listar_pastas(client: TelegramClient) -> list:
    resultado = await client(GetDialogFiltersRequest())
    return [p for p in getattr(resultado, "filters", resultado) if titulo_da_pasta(p) is not None]


def mesmo_nome(a: str, b: str) -> bool:
    limpa = lambda s: re.sub(r"[^a-z0-9]+", " ", normalizar(s)).strip()
    return limpa(a) == limpa(b)


async def chats_da_pasta(client: TelegramClient, pasta) -> dict[int, object]:
    """id do chat -> peer/entidade. Cobre os chats adicionados um a um e também
    as categorias da pasta ("Grupos", "Canais"...), menos os excluídos."""
    chats: dict[int, object] = {}
    for peer in [*pasta.pinned_peers, *pasta.include_peers]:
        try:
            chats[utils.get_peer_id(peer)] = peer
        except (TypeError, ValueError):  # ex.: "Mensagens salvas"
            pass
    if any(getattr(pasta, flag, False) for flag in ("groups", "broadcasts", "contacts", "non_contacts", "bots")):
        async for dialogo in client.iter_dialogs():
            e = dialogo.entity
            usuario = isinstance(e, User)
            if ((getattr(pasta, "groups", False) and dialogo.is_group)
                    or (getattr(pasta, "broadcasts", False) and dialogo.is_channel and not dialogo.is_group)
                    or (getattr(pasta, "bots", False) and usuario and e.bot)
                    or (getattr(pasta, "contacts", False) and usuario and e.contact and not e.bot)
                    or (getattr(pasta, "non_contacts", False) and usuario and not e.contact and not e.bot)):
                chats.setdefault(dialogo.id, e)
    for peer in getattr(pasta, "exclude_peers", []):
        try:
            chats.pop(utils.get_peer_id(peer), None)
        except (TypeError, ValueError):
            pass
    return chats


def nome_do_chat(entidade) -> str:
    return getattr(entidade, "title", None) or utils.get_display_name(entidade) or "chat sem nome"


def link_da_mensagem(chat, msg_id: int) -> str | None:
    usuario = getattr(chat, "username", None)
    if not usuario:
        usuario = next((u.username for u in (getattr(chat, "usernames", None) or []) if u.active), None)
    if usuario:
        return f"https://t.me/{usuario}/{msg_id}"
    if isinstance(chat, Channel):  # grupo/canal privado: só abre para quem é membro
        return f"https://t.me/c/{chat.id}/{msg_id}"
    return None  # grupo básico antigo: o Telegram não tem link de mensagem


def links_da_oferta(msg) -> list[str]:
    """Links da loja: escritos no texto, escondidos em palavras ("Compre aqui")
    ou em botões embaixo da mensagem."""
    links = []
    texto = msg.message or ""
    for entidade in msg.entities or []:
        if isinstance(entidade, MessageEntityTextUrl):
            links.append(entidade.url)
        elif isinstance(entidade, MessageEntityUrl):
            # offset/length do Telegram contam em UTF-16
            utf16 = texto.encode("utf-16-le")
            links.append(utf16[entidade.offset * 2:(entidade.offset + entidade.length) * 2].decode("utf-16-le"))
    for linha in getattr(getattr(msg, "reply_markup", None), "rows", None) or []:
        for botao in linha.buttons:
            # Camadas antigas: KeyboardButtonUrl(url). Atuais: KeyboardButton(type=InlineButtonTypeUrl(url)).
            alvo = getattr(botao, "type", botao)
            if type(alvo).__name__ in ("KeyboardButtonUrl", "InlineButtonTypeUrl"):
                links.append(alvo.url)
    vistos = []
    for link in links:
        if link.startswith(("http://", "https://")) and link not in vistos:
            vistos.append(link)
    return vistos


class Monitor:
    def __init__(self, client: TelegramClient, cfg: Config, meu_id: int):
        self.client = client
        self.cfg = cfg
        self.meu_id = meu_id
        self.vigiados: dict[int, str] = {}        # chat -> nome
        self.entidades: dict[int, object] = {}
        self.ultimo_id: dict[int, int] = {}       # última mensagem já vista por chat
        self.processadas: dict[tuple[int, int], None] = {}
        self.recentes: dict[str, float] = {}
        self.pelando_vistas: dict[str, None] | None = None  # ids já vistos, em ordem; None até a 1ª leitura
        self.pelando_falhas = 0
        self.estado = carregar_estado()

    async def enviar(self, texto_html: str) -> dict:
        return await chamar_bot(self.cfg.bot_token, "sendMessage", {
            "chat_id": self.meu_id,
            "text": texto_html,
            "parse_mode": "HTML",
        })

    async def atualizar_pasta(self) -> bool:
        pastas = await listar_pastas(self.client)
        pasta = next((p for p in pastas if mesmo_nome(titulo_da_pasta(p), self.cfg.pasta)), None)
        if pasta is None:
            nomes = ", ".join(f"“{titulo_da_pasta(p)}”" for p in pastas) or "nenhuma"
            log(f"Não achei a pasta “{self.cfg.pasta}”. Suas pastas: {nomes}.")
            return False
        atuais = await chats_da_pasta(self.client, pasta)
        novos = [cid for cid in atuais if cid not in self.vigiados]
        removidos = [cid for cid in self.vigiados if cid not in atuais]
        for cid in novos:
            try:
                entidade = await self.client.get_entity(atuais[cid])
                ultima = await self.client.get_messages(entidade, limit=1)
            except Exception as erro:
                log(f"Não consegui abrir o chat {cid}: {erro}")
                continue
            self.entidades[cid] = entidade
            self.vigiados[cid] = nome_do_chat(entidade)
            # Começa do agora: ofertas antigas não disparam aviso.
            self.ultimo_id[cid] = ultima[0].id if ultima else 0
        for cid in removidos:
            log(f"Saiu da pasta: {self.vigiados.pop(cid)}")
            self.entidades.pop(cid, None)
            self.ultimo_id.pop(cid, None)
        if novos:
            log("Vigiando: " + ", ".join(self.vigiados[c] for c in novos if c in self.vigiados))
        return True

    async def ao_receber(self, evento) -> None:
        if evento.chat_id in self.vigiados:
            await self.processar(evento.chat_id, evento.message)

    async def varrer(self) -> None:
        """O Telegram às vezes deixa de empurrar mensagens de canais grandes;
        isto busca o que chegou desde a última mensagem vista."""
        for cid in list(self.vigiados):
            try:
                async for msg in self.client.iter_messages(
                    self.entidades[cid], min_id=self.ultimo_id.get(cid, 0), limit=30, reverse=True
                ):
                    await self.processar(cid, msg)
            except Exception as erro:
                log(f"Falha ao buscar mensagens de {self.vigiados.get(cid, cid)}: {erro}")

    async def laco(self, intervalo: float, tarefa) -> None:
        while True:
            await asyncio.sleep(intervalo)
            try:
                await tarefa()
            except Exception as erro:
                log(f"Erro em {tarefa.__name__}: {erro}")

    async def processar(self, cid: int, msg) -> None:
        chave = (cid, msg.id)
        if chave in self.processadas:
            return
        self.processadas[chave] = None
        if len(self.processadas) > 5000:
            del self.processadas[next(iter(self.processadas))]
        self.ultimo_id[cid] = max(self.ultimo_id.get(cid, 0), msg.id)

        texto = msg.message or ""
        if not texto.strip():
            return
        analise = analisar(texto, self.cfg.filtro)
        if not analise.oferta:
            return
        grupo = self.vigiados.get(cid, "?")
        if self.repetida(texto, analise.preco, grupo):
            return

        chat = self.entidades.get(cid) or await msg.get_chat()
        link = link_da_mensagem(chat, msg.id)
        await self.avisar(analise, texto, grupo, link, links_da_oferta(msg))

    def repetida(self, texto: str, preco: float | None, origem: str) -> bool:
        """Vale para Telegram e Pelando juntos: a oferta que já chegou por um
        lado não avisa de novo pelo outro."""
        if ja_avisada(self.recentes, texto, preco):
            log(f"Oferta repetida em {origem}, sem novo aviso.")
            return True
        return False

    async def verificar_pelando(self) -> None:
        try:
            promos = await buscar_pelando(self.cfg.pelando_busca)
        except Exception as erro:
            self.pelando_falhas += 1
            if self.pelando_falhas in (1, 10):  # avisa no console sem repetir a cada 3 min
                log(f"Não consegui ler o Pelando ({erro}). Sigo tentando.")
            return
        if self.pelando_falhas:
            log("Pelando voltou a responder.")
            self.pelando_falhas = 0
        if not promos:
            log("O Pelando respondeu, mas não achei promoções na página (o site pode ter mudado).")
            return

        primeira_leitura = self.pelando_vistas is None
        vistas = self.pelando_vistas if self.pelando_vistas is not None else {}
        self.pelando_vistas = vistas
        for promo in promos:
            if promo.id in vistas:
                continue
            vistas[promo.id] = None
            # Começa do agora, como no Telegram: o que já estava lá não avisa.
            if primeira_leitura or promo.inativa:
                continue
            texto = f"{promo.titulo} {promo.preco}"
            analise = analisar(texto, self.cfg.filtro)
            if not analise.oferta or self.repetida(texto, analise.preco, "Pelando"):
                continue
            detalhes = " · ".join(x for x in (promo.loja, promo.temperatura and f"{promo.temperatura} no Pelando") if x)
            await self.avisar(analise, f"{promo.titulo}\n{detalhes}".strip(), "Pelando", promo.link, [],
                              rotulo_link="Abrir no Pelando")
        while len(vistas) > 3000:  # as mais antigas já saíram das páginas há dias
            del vistas[next(iter(vistas))]
        if primeira_leitura:
            log(f"Pelando: {len(promos)} promoções na tela agora; aviso das que entrarem daqui pra frente.")

    async def avisar(self, analise: Analise, texto: str, grupo: str, link: str | None, lojas: list[str],
                     rotulo_link: str = "Abrir a mensagem no grupo") -> None:
        aviso = montar_aviso(analise, texto, grupo, link, lojas, self.estado, rotulo_link)
        salvar_estado(self.estado)
        resposta = await self.enviar(aviso)
        preco = analise.preco
        situacao = "avisado" if resposta.get("ok") else f"FALHA ao avisar: {resposta.get('description')}"
        log(f"PS5 {'R$ ' + reais(preco) if preco else '(sem preço)'} em {grupo} — {situacao}")
        registrar_historico(preco, grupo, link, texto)


def ja_avisada(recentes: dict[str, float], texto: str, preco: float | None) -> bool:
    """Marca a oferta como avisada e diz se ela já tinha sido, nas últimas 12 h."""
    agora = time.time()
    for chave in [k for k, t in recentes.items() if agora - t >= JANELA_REPETIDA_S]:
        del recentes[chave]
    chave = chave_de_repeticao(texto, preco)
    if chave in recentes:
        return True
    recentes[chave] = agora
    return False


def montar_aviso(analise: Analise, texto: str, origem: str, link: str | None, lojas: list[str],
                 estado: dict, rotulo_link: str = "Abrir a mensagem no grupo") -> str:
    """O texto do aviso no bot. Atualiza o menor preço já visto em `estado`."""
    preco = analise.preco
    linhas = [f"🎮 <b>PS5 · {'R$ ' + reais(preco) if preco else 'preço não identificado'}</b>"]
    menor = estado.get("menor_preco")
    if preco and (menor is None or preco < menor):
        linhas.append("🏆 Menor preço desde que o monitor começou")
        estado.update(menor_preco=preco, menor_preco_em=f"{datetime.now():%d/%m %H:%M}", menor_preco_grupo=origem)
    elif menor is not None:
        linhas.append(f"Menor já visto: R$ {reais(menor)} ({estado.get('menor_preco_em', '')})")
    linhas.append(f"📍 {html.escape(origem)}")
    resumo = texto.strip()
    if len(resumo) > 700:
        resumo = resumo[:700].rsplit(" ", 1)[0] + "…"
    linhas += ["", html.escape(resumo)]
    if lojas:
        linhas.append("")
        for url in lojas[:3]:
            exibido = re.sub(r"^https?://(www\.)?", "", url)
            exibido = exibido if len(exibido) <= 45 else exibido[:44] + "…"
            linhas.append(f'🛒 <a href="{html.escape(url, quote=True)}">{html.escape(exibido)}</a>')
    if link:
        linhas += ["", f'<a href="{html.escape(link, quote=True)}">{rotulo_link}</a>']
    return "\n".join(linhas)


# --------------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------------- #
async def conectar(cfg: Config) -> TelegramClient:
    client = TelegramClient(str(ARQUIVO_SESSAO), cfg.api_id, cfg.api_hash)
    print("Conectando ao Telegram... (na primeira vez ele pede seu telefone com +55 e o código)")
    await client.start()
    return client


async def comando_pastas(cfg: Config) -> None:
    client = await conectar(cfg)
    try:
        pastas = await listar_pastas(client)
        if not pastas:
            print("Você ainda não tem pastas. No Telegram: Configurações > Pastas de conversas.")
        for pasta in pastas:
            n = len(pasta.pinned_peers) + len(pasta.include_peers)
            extra = " + categorias (grupos/canais...)" if any(
                getattr(pasta, f, False) for f in ("groups", "broadcasts", "contacts", "non_contacts", "bots")) else ""
            print(f"  “{titulo_da_pasta(pasta)}”: {n} chats{extra}")
    finally:
        await client.disconnect()


async def comando_monitorar(cfg: Config) -> None:
    eu_bot = await chamar_bot(cfg.bot_token, "getMe", {})
    if not eu_bot.get("ok"):
        sys.exit(f"O token do bot não funcionou ({eu_bot.get('description')}). Confira o config.toml.")
    nome_bot = eu_bot["result"]["username"]

    client = await conectar(cfg)
    try:
        eu = await client.get_me()
        monitor = Monitor(client, cfg, eu.id)
        if not await monitor.atualizar_pasta():
            sys.exit("Ajuste o nome da pasta no config.toml e rode de novo.")
        if not monitor.vigiados:
            sys.exit(f"A pasta “{cfg.pasta}” está vazia. Adicione os grupos de ofertas nela.")

        teto = f" até R$ {reais(cfg.filtro.preco_maximo)}" if cfg.filtro.preco_maximo else ""
        pelando = ""
        if cfg.pelando_ativo:
            await monitor.verificar_pelando()
            busca = f" (busca “{html.escape(cfg.pelando_busca)}” + recentes)" if cfg.pelando_busca else " (recentes)"
            pelando = f" e o site do Pelando{busca}"
        resposta = await monitor.enviar(
            f"✅ Monitor ligado. Vigiando {len(monitor.vigiados)} chats da pasta "
            f"“{html.escape(cfg.pasta)}”{pelando} atrás de PS5{teto}."
        )
        if not resposta.get("ok"):
            sys.exit(
                f"O bot não conseguiu te mandar mensagem ({resposta.get('description')}).\n"
                f"Abra @{nome_bot} no Telegram, toque em Iniciar e rode o monitor de novo."
            )

        client.add_event_handler(monitor.ao_receber, events.NewMessage(incoming=True))
        tarefas = [
            asyncio.create_task(monitor.laco(INTERVALO_PASTA_S, monitor.atualizar_pasta)),
            asyncio.create_task(monitor.laco(INTERVALO_VARREDURA_S, monitor.varrer)),
        ]
        if cfg.pelando_ativo:
            tarefas.append(asyncio.create_task(monitor.laco(INTERVALO_PELANDO_S, monitor.verificar_pelando)))
        log(f"Monitor ligado: {len(monitor.vigiados)} chats. Avisos chegam por @{nome_bot}. "
            "Deixe esta janela aberta (Ctrl+C para parar).")
        try:
            await client.run_until_disconnected()
        finally:
            for tarefa in tarefas:
                tarefa.cancel()
    finally:
        await client.disconnect()


def comando_testar(cfg: Config, texto: str) -> None:
    analise = analisar(texto, cfg.filtro)
    preco = f"R$ {reais(analise.preco)}" if analise.preco else "sem preço"
    print(f"{'AVISARIA' if analise.oferta else 'ignoraria'} · {preco} · {analise.motivo}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Avisa no Telegram quando aparece oferta de console PS5.")
    parser.add_argument("--pastas", action="store_true", help="lista as pastas do seu Telegram e sai")
    parser.add_argument("--testar", metavar="TEXTO", help="mostra o que o filtro acha de um texto e sai")
    args = parser.parse_args()

    if args.testar is not None:
        comando_testar(carregar_config(exigir_telegram=False, exigir_bot=False), args.testar)
        return
    try:
        if args.pastas:
            asyncio.run(comando_pastas(carregar_config(exigir_bot=False)))
        else:
            asyncio.run(comando_monitorar(carregar_config()))
    except KeyboardInterrupt:
        print("\nMonitor parado.")


if __name__ == "__main__":
    main()
