"""
servidor_web.py — o monitor rodando sem parar no Render (plano grátis).

Repete a rodada do nuvem.py a cada minuto (o Pelando a cada 3) e responde num
endereço web, que é o que o Render exige de um serviço grátis. O Render
desliga o serviço depois de 15 minutos sem visitas, então o próprio monitor
visita o seu endereço a cada 10 minutos.

Os bots recebem mensagens por webhook em /telegram/<bot>: quem aperta
"Iniciar" (/start) recebe a mensagem de boas-vindas do config.toml.

Variáveis de ambiente: BOT_TOKEN, CHAT_ID (e os bots extras do config.toml);
o Render fornece PORT e RENDER_EXTERNAL_URL.
"""

import asyncio
import hashlib
import json
import os
import re
import threading
import time
import tomllib
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import monitor as m
import nuvem

INTERVALO_CANAIS_S = 60
INTERVALO_PELANDO_S = 180
# Se o Pelando recusar várias vezes seguidas (ele bloqueia alguns servidores
# de nuvem), tenta só de meia em meia hora para não insistir à toa.
PELANDO_FALHAS_ATE_ESPACAR = 3
INTERVALO_PELANDO_BLOQUEADO_S = 1800
INTERVALO_ACORDAR_S = 600  # bem abaixo dos 15 min que fazem o Render desligar o serviço
RESUMO_A_CADA_RODADAS = 60  # uma linha de saúde no log por hora

BOAS_VINDAS_PADRAO = "👋 Olá! A partir de agora eu te aviso aqui quando aparecer uma oferta."
# Para quem não é o dono (CHAT_ID): os avisos só vão para o dono.
BOAS_VINDAS_OUTROS = "👋 Olá! Este bot é de uso pessoal e só envia avisos para o dono dele."

status = {"inicio": datetime.now(), "rodadas": 0, "avisos": 0, "ultima": "-", "pelando": "-", "erro": ""}
# Contadores do resumo de hora em hora; zeram a cada resumo.
hora = {"posts": 0, "avisos": 0, "erros": 0, "visitas_ok": 0, "visitas_falha": 0}
# caminho do webhook ("ps5", "novablast"...) -> {"token", "boas_vindas"}
bots: dict[str, dict] = {}


def segredo_webhook(token: str) -> str:
    """O Telegram devolve este valor no cabeçalho de cada entrega; só quem tem
    o token do bot consegue calculá-lo, então ninguém mais finge ser o Telegram."""
    return hashlib.sha256(token.encode()).hexdigest()[:32]


def carregar_bots() -> dict[str, dict]:
    with open(m.ARQUIVO_CONFIG, "rb") as arquivo:
        dados = tomllib.load(arquivo)
    encontrados = {}
    principal = os.environ.get("BOT_TOKEN", "").strip()
    if principal:
        encontrados["ps5"] = {"token": principal,
                              "boas_vindas": dados.get("nuvem", {}).get("boas_vindas", BOAS_VINDAS_PADRAO)}
    for bloco in dados.get("outros", []):
        token = os.environ.get(str(bloco.get("bot", "")).strip() or "-", "").strip()
        if token:
            caminho = re.sub(r"[^a-z0-9]+", "-", m.normalizar(bloco.get("nome", "produto"))).strip("-")
            encontrados[caminho] = {"token": token, "boas_vindas": bloco.get("boas_vindas", BOAS_VINDAS_PADRAO)}
    return encontrados


async def registrar_webhooks() -> None:
    base = os.environ.get("RENDER_EXTERNAL_URL")
    if not base:  # rodando fora do Render: sem endereço público para o Telegram entregar
        return
    for caminho, bot in bots.items():
        resposta = await m.chamar_bot(bot["token"], "setWebhook", {
            "url": f"{base}/telegram/{caminho}",
            "secret_token": segredo_webhook(bot["token"]),
            "allowed_updates": ["message"],
            "drop_pending_updates": True,
        })
        if not resposta.get("ok"):
            m.log(f"Não consegui ligar as mensagens do bot {caminho}: {resposta.get('description')}")


def resposta_ao_bot(bot: dict, atualizacao: dict) -> dict:
    """A resposta vai no próprio retorno do webhook (o Telegram aceita um
    método no corpo), sem precisar de outra chamada à API."""
    mensagem = atualizacao.get("message") or {}
    chat_id = (mensagem.get("chat") or {}).get("id")
    if not chat_id or not (mensagem.get("text") or "").startswith("/start"):
        return {}
    dono = str(chat_id) == os.environ.get("CHAT_ID", "").strip()
    return {"method": "sendMessage", "chat_id": chat_id,
            "text": bot["boas_vindas"] if dono else BOAS_VINDAS_OUTROS}


class Pagina(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        bot = bots.get(self.path.rstrip("/").removeprefix("/telegram/")) if self.path.startswith("/telegram/") else None
        if not bot or self.headers.get("X-Telegram-Bot-Api-Secret-Token") != segredo_webhook(bot["token"]):
            self.send_response(403)
            self.end_headers()
            return
        try:
            atualizacao = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            atualizacao = {}
        corpo = json.dumps(resposta_ao_bot(bot, atualizacao)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self) -> None:
        linhas = [
            f"Monitor de ofertas PS5 no ar desde {status['inicio']:%d/%m %H:%M}",
            f"Rodadas: {status['rodadas']} (última às {status['ultima']}), avisos enviados: {status['avisos']}",
            f"Pelando: {status['pelando']}",
        ]
        if status["erro"]:
            linhas.append(f"Último erro: {status['erro']}")
        corpo = ("\n".join(linhas) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def log_message(self, *args) -> None:  # sem uma linha de log a cada visita
        pass


def servir_pagina() -> None:
    servidor = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))), Pagina)
    threading.Thread(target=servidor.serve_forever, name="pagina", daemon=True).start()


def _visitar(url: str) -> None:
    with urllib.request.urlopen(url, timeout=30) as resposta:
        resposta.read()


async def manter_acordado() -> None:
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return
    while True:
        await asyncio.sleep(INTERVALO_ACORDAR_S)
        try:
            await asyncio.to_thread(_visitar, url)
            hora["visitas_ok"] += 1
        except Exception as erro:
            hora["visitas_falha"] += 1
            m.log(f"Não consegui visitar {url}: {erro}")


async def main() -> None:
    bots.update(carregar_bots())
    servir_pagina()
    asyncio.create_task(manter_acordado())
    await registrar_webhooks()
    m.log("Monitor ligado: canais a cada 1 min, Pelando a cada 3 min.")

    proximo_pelando, falhas_pelando = 0.0, 0
    while True:
        inicio = time.monotonic()
        com_pelando = inicio >= proximo_pelando
        try:
            resumo = await nuvem.rodada(pelando=com_pelando, registrar=False)
            status["erro"] = ""
            status["avisos"] += resumo["avisos"]
            hora["posts"] += resumo["posts"]
            hora["avisos"] += resumo["avisos"]
            if com_pelando:
                status["pelando"] = resumo["pelando"]
                falhas_pelando = falhas_pelando + 1 if resumo["pelando_falhou"] else 0
                espera = INTERVALO_PELANDO_S
                if falhas_pelando >= PELANDO_FALHAS_ATE_ESPACAR:
                    espera = INTERVALO_PELANDO_BLOQUEADO_S
                    if falhas_pelando == PELANDO_FALHAS_ATE_ESPACAR:
                        m.log(f"Pelando recusando ({resumo['pelando']}); tentando só a cada 30 min.")
                proximo_pelando = inicio + espera
            if resumo["avisos"]:
                m.log(f"{resumo['avisos']} aviso(s) enviado(s); {resumo['posts']} posts novos nesta rodada.")
        except SystemExit as erro:  # faltam BOT_TOKEN/CHAT_ID: fica no ar mostrando o problema
            status["erro"] = str(erro)
            hora["erros"] += 1
            m.log(str(erro))
        except Exception as erro:
            status["erro"] = f"{datetime.now():%H:%M} {erro}"
            hora["erros"] += 1
            m.log(f"Erro na rodada: {erro}")
        status["rodadas"] += 1
        status["ultima"] = f"{datetime.now():%H:%M:%S}"
        if status["rodadas"] % RESUMO_A_CADA_RODADAS == 0:
            m.log(f"Resumo da última hora: {RESUMO_A_CADA_RODADAS} rodadas, {hora['posts']} posts lidos, "
                  f"{hora['avisos']} avisos, {hora['erros']} erros, visitas para manter acordado: "
                  f"{hora['visitas_ok']} ok / {hora['visitas_falha']} falhas. Pelando: {status['pelando']}.")
            hora.update(dict.fromkeys(hora, 0))
        await asyncio.sleep(max(5.0, INTERVALO_CANAIS_S - (time.monotonic() - inicio)))


if __name__ == "__main__":
    asyncio.run(main())
