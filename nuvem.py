"""
nuvem.py — uma rodada do monitor de ofertas PS5, feita para o GitHub Actions.

A cada execução: lê as páginas públicas dos canais do Telegram (t.me/s/<canal>,
sem login nenhum), lê o Pelando, passa tudo pelo mesmo filtro do monitor.py e
avisa pelo bot. O que já foi visto fica em estado.json, que o workflow guarda
no cache do GitHub entre uma rodada e outra.

Variáveis de ambiente: BOT_TOKEN (token do bot) e CHAT_ID (seu id no Telegram).

Uso:
    python nuvem.py              uma rodada, avisando pelo bot
    python nuvem.py --simular    mostra os avisos no terminal em vez de enviar
"""

import argparse
import asyncio
import html
import os
import re
import sys
import tomllib
from dataclasses import dataclass

import monitor as m

# Canal muito ativo pode publicar mais que uma página (~20 posts) entre duas
# rodadas; nesse caso busca até estas páginas anteriores para não pular nada.
PAGINAS_ANTERIORES = 4
PELANDO_VISTAS_MAX = 1500
_AVISOU_FALTA_BOT: set[str] = set()  # no servidor, avisa a falta de um bot só uma vez

_POST = re.compile(r'class="tgme_widget_message [^"]*" data-post="([^"/]+)/(\d+)"')
_TEXTO = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_LINK = re.compile(r'<a[^>]+href="(https?://[^"]+)"')
_BOTAO = re.compile(r'<a class="tgme_widget_message_inline_button[^"]*" href="(https?://[^"]+)"')
_TITULO = re.compile(r'<meta property="og:title" content="([^"]*)"')


@dataclass
class Post:
    id: int
    texto: str
    links: list[str]


@dataclass
class OutroProduto:
    nome: str
    emoji: str
    canais: list[str]
    filtro: m.FiltroProduto
    bot_env: str  # variável de ambiente com o token do bot deste produto ("" = bot principal)
    chave_bot: str  # webhook e lista de inscritos; produtos no mesmo bot dividem a mesma


CHAVE_BOT_PRINCIPAL = "ps5"


def chave_do_bot(nome_produto: str) -> str:
    """Nome curto do bot de um produto ("Novablast" -> "novablast"): usado no
    endereço do webhook e na lista de inscritos."""
    return re.sub(r"[^a-z0-9]+", "-", m.normalizar(nome_produto)).strip("-")


def chaves_dos_bots(dados: dict) -> dict[str, str]:
    """Variável do token -> chave do bot. Vários produtos podem usar o mesmo
    bot; a chave vem do primeiro produto dele no config.toml, para a lista de
    inscritos continuar a mesma quando outro produto entra no bot."""
    chaves: dict[str, str] = {}
    for bloco in dados.get("outros", []):
        env = str(bloco.get("bot", "")).strip()
        if env and env not in chaves:
            chaves[env] = chave_do_bot(bloco.get("nome", "produto"))
    return chaves


def carregar_outros(dados: dict) -> list[OutroProduto]:
    """Os blocos [[outros]] do config.toml."""
    outros = []
    chaves = chaves_dos_bots(dados)
    for bloco in dados.get("outros", []):
        termos = m.compilar_termos(bloco.get("termos", []))
        if not termos or not bloco.get("canais"):
            m.log(f"Produto “{bloco.get('nome', '?')}” sem termos ou sem canais no config.toml; ignorado.")
            continue
        outros.append(OutroProduto(
            nome=bloco.get("nome", "produto"),
            emoji=bloco.get("emoji", "🔔"),
            canais=bloco["canais"],
            filtro=m.FiltroProduto(
                termos=termos,
                bloqueados=m.compilar_termos(bloco.get("termos_bloqueados", [])),
                preco_minimo=float(bloco.get("preco_minimo", 0)),
                preco_maximo=float(bloco.get("preco_maximo", 0)),
                exige=m.compilar_termos(bloco.get("placas", [])),
                ram_minima=int(bloco.get("ram_minima_gb", 0)),
            ),
            bot_env=str(bloco.get("bot", "")).strip(),
            chave_bot=chaves.get(str(bloco.get("bot", "")).strip(), CHAVE_BOT_PRINCIPAL),
        ))
    return outros


def ler_posts(pagina: str) -> list[Post]:
    """Os posts da página pública do canal, do mais antigo para o mais novo."""
    marcas = list(_POST.finditer(pagina))
    posts = []
    for i, marca in enumerate(marcas):
        trecho = pagina[marca.end():marcas[i + 1].start() if i + 1 < len(marcas) else len(pagina)]
        corpo = _TEXTO.search(trecho)
        texto, links = "", []
        if corpo:
            bruto = re.sub(r"<br\s*/?>", "\n", corpo.group(1))
            texto = html.unescape(re.sub(r"<[^>]+>", "", bruto)).strip()
            links = _LINK.findall(corpo.group(1))
        links += _BOTAO.findall(trecho)
        lojas = []
        for link in map(html.unescape, links):
            if not link.startswith("https://t.me/") and link not in lojas:  # t.me aqui são hashtags/menções
                lojas.append(link)
        posts.append(Post(int(marca.group(2)), texto, lojas))
    return posts


def posts_novos(canal: str, estado: dict, baixar=m._baixar) -> tuple[str, list[Post]]:
    """Nome do canal e os posts publicados desde a última rodada."""
    pagina = baixar(f"https://t.me/s/{canal}")
    titulo = html.unescape(_TITULO.search(pagina).group(1)) if _TITULO.search(pagina) else f"@{canal}"
    posts = ler_posts(pagina)
    if not posts:
        return titulo, []
    vistos = estado["canais"]
    ultimo = vistos.get(canal)
    maior = max(p.id for p in posts)
    if ultimo is None:  # canal novo na lista: começa do agora, sem avisar o que já estava lá
        vistos[canal] = maior
        return titulo, []

    novos = {p.id: p for p in posts if p.id > ultimo}
    menor = min(p.id for p in posts)
    for _ in range(PAGINAS_ANTERIORES):
        if menor <= ultimo + 1:
            break
        anteriores = ler_posts(baixar(f"https://t.me/s/{canal}?before={menor}"))
        if not anteriores:
            break
        novos.update({p.id: p for p in anteriores if p.id > ultimo})
        menor = min(p.id for p in anteriores)
    vistos[canal] = max(ultimo, maior)
    return titulo, [novos[i] for i in sorted(novos)]


async def rodada(simular: bool = False, pelando: bool = True, registrar: bool = True, entregar=None) -> dict:
    """Uma passada por canais (e Pelando, se `pelando`). Devolve um resumo;
    com `registrar`, também o escreve no log. `entregar(texto, token, chave_do_bot)`
    envia um aviso e devolve para quantas pessoas foi; sem ela, vai só para CHAT_ID."""
    token = os.environ.get("BOT_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()
    if not simular and (not token or not chat_id):
        sys.exit("Faltam BOT_TOKEN e CHAT_ID nas variáveis de ambiente (segredos do GitHub ou Environment do Render).")

    cfg = m.carregar_config(exigir_telegram=False, exigir_bot=False)
    with open(m.ARQUIVO_CONFIG, "rb") as arquivo:
        dados = tomllib.load(arquivo)
    canais = dados.get("nuvem", {}).get("canais", [])
    outros = carregar_outros(dados)

    estado = m.carregar_estado()
    estado.setdefault("canais", {})
    estado.setdefault("pelando", [])
    estado.setdefault("recentes", {})
    avisos: list[tuple[str, str, str, str]] = []  # (texto do aviso, resumo para o log, token, chave do bot)

    # Um canal lido por mais de um produto é baixado uma vez só por rodada.
    paginas: dict[str, str] = {}

    def baixar(url: str) -> str:
        if url not in paginas:
            paginas[url] = m._baixar(url)
        return paginas[url]

    total_posts = 0
    for canal in canais:
        try:
            nome, posts = await asyncio.to_thread(posts_novos, canal, estado, baixar)
        except Exception as erro:
            m.log(f"Não consegui ler @{canal}: {erro}")
            continue
        total_posts += len(posts)
        for post in posts:
            analise = m.analisar(post.texto, cfg.filtro) if post.texto else None
            if not analise or not analise.oferta or m.ja_avisada(estado["recentes"], post.texto, analise.preco):
                continue
            link = f"https://t.me/{canal}/{post.id}"
            avisos.append((m.montar_aviso(analise, post.texto, nome, link, post.links, estado), f"{nome}: {link}",
                           token, CHAVE_BOT_PRINCIPAL))

    # Outros produtos: cada um com seus canais, seu filtro, seu estado (o que
    # já viu, repetições e menor preço) e, se configurado, seu próprio bot.
    for alvo in outros:
        token_alvo, chave_alvo = token, alvo.chave_bot
        if alvo.bot_env:
            token_alvo = os.environ.get(alvo.bot_env, "").strip()
            if not token_alvo:  # bot próprio ainda não configurado: não perde o aviso
                if not simular and alvo.bot_env not in _AVISOU_FALTA_BOT:
                    _AVISOU_FALTA_BOT.add(alvo.bot_env)
                    m.log(f"Falta {alvo.bot_env} para {alvo.nome}; avisando pelo bot principal.")
                token_alvo, chave_alvo = token, CHAVE_BOT_PRINCIPAL
        estado_alvo = estado.setdefault("outros", {}).setdefault(alvo.nome, {})
        estado_alvo.setdefault("canais", {})
        estado_alvo.setdefault("recentes", {})
        for canal in alvo.canais:
            try:
                nome, posts = await asyncio.to_thread(posts_novos, canal, estado_alvo, baixar)
            except Exception as erro:
                m.log(f"Não consegui ler @{canal} ({alvo.nome}): {erro}")
                continue
            total_posts += len(posts)
            for post in posts:
                analise = m.analisar_produto(post.texto, alvo.filtro) if post.texto else None
                if not analise or not analise.oferta:
                    continue
                if m.ja_avisada(estado_alvo["recentes"], post.texto, analise.preco, analise.titulo):
                    continue
                link = f"https://t.me/{canal}/{post.id}"
                aviso = m.montar_aviso(analise, post.texto, nome, link, post.links, estado_alvo,
                                       produto=analise.titulo, emoji=alvo.emoji, chave=f"menor_{analise.motivo}")
                avisos.append((aviso, f"{analise.titulo} em {nome}: {link}", token_alvo, chave_alvo))

    situacao_pelando, pelando_falhou = "desligado", False
    if not cfg.pelando_ativo:
        pass
    elif not pelando:
        situacao_pelando = "fora desta rodada"
    else:
        try:
            promos = await m.buscar_pelando(cfg.pelando_busca)
            primeira = not estado.get("pelando_iniciado")
            vistas = set(estado["pelando"])
            for promo in promos:
                if promo.id in vistas:
                    continue
                vistas.add(promo.id)
                estado["pelando"].append(promo.id)
                if primeira or promo.inativa:
                    continue
                texto = f"{promo.titulo} {promo.preco}"
                analise = m.analisar(texto, cfg.filtro)
                if not analise.oferta or m.ja_avisada(estado["recentes"], texto, analise.preco):
                    continue
                detalhes = " · ".join(x for x in (promo.loja, promo.temperatura and f"{promo.temperatura} no Pelando") if x)
                aviso = m.montar_aviso(analise, f"{promo.titulo}\n{detalhes}".strip(), "Pelando", promo.link, [],
                                       estado, rotulo_link="Abrir no Pelando")
                avisos.append((aviso, f"Pelando: {promo.link}", token, CHAVE_BOT_PRINCIPAL))
            estado["pelando"] = estado["pelando"][-PELANDO_VISTAS_MAX:]
            estado["pelando_iniciado"] = True
            situacao_pelando = f"{len(promos)} promoções lidas"
        except Exception as erro:
            situacao_pelando, pelando_falhou = f"erro ({erro})", True

    for texto, resumo, token_bot, chave_bot in avisos:
        if simular:
            print("-" * 60 + "\n" + texto)
            continue
        if entregar:
            pessoas = await entregar(texto, token_bot, chave_bot)
            m.log(f"Aviso enviado para {pessoas} pessoa(s) pelo bot {chave_bot} — {resumo}")
            continue
        resposta = await m.chamar_bot(token_bot, "sendMessage", {"chat_id": chat_id, "text": texto, "parse_mode": "HTML"})
        m.log(f"Aviso {'enviado' if resposta.get('ok') else 'FALHOU: ' + str(resposta.get('description'))} — {resumo}")

    m.salvar_estado(estado)
    resumo = {"canais": len(canais), "posts": total_posts, "avisos": len(avisos),
              "pelando": situacao_pelando, "pelando_falhou": pelando_falhou}
    if registrar:
        m.log(f"Rodada: {len(canais)} canais, {total_posts} posts novos, "
              f"Pelando: {situacao_pelando}, {len(avisos)} avisos.")
    return resumo


def main() -> None:
    parser = argparse.ArgumentParser(description="Uma rodada do monitor de ofertas PS5 (GitHub Actions).")
    parser.add_argument("--simular", action="store_true", help="mostra os avisos em vez de enviar pelo bot")
    asyncio.run(rodada(simular=parser.parse_args().simular))


if __name__ == "__main__":
    main()
