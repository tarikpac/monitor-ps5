"""
servidor_web.py — o monitor rodando sem parar no Render (plano grátis).

Repete a rodada do nuvem.py a cada minuto (o Pelando a cada 3) e responde num
endereço web, que é o que o Render exige de um serviço grátis. O Render
desliga o serviço depois de 15 minutos sem visitas, então o próprio monitor
visita o seu endereço a cada 10 minutos.

Variáveis de ambiente: BOT_TOKEN, CHAT_ID; o Render fornece PORT e
RENDER_EXTERNAL_URL.
"""

import asyncio
import os
import threading
import time
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

status = {"inicio": datetime.now(), "rodadas": 0, "avisos": 0, "ultima": "-", "pelando": "-", "erro": ""}
# Contadores do resumo de hora em hora; zeram a cada resumo.
hora = {"posts": 0, "avisos": 0, "erros": 0, "visitas_ok": 0, "visitas_falha": 0}


class Pagina(BaseHTTPRequestHandler):
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
    servir_pagina()
    asyncio.create_task(manter_acordado())
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
