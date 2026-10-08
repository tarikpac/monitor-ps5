"""
assinantes.py — quem recebe os avisos de cada bot.

O Render grátis apaga os arquivos a cada reinício, então a lista fica numa
mensagem fixada de um canal privado do Telegram (ARMAZEM_CHAT_ID), escrita
pelo bot principal: JSON {"ps5": [ids], "novablast": [ids]}. O dono
(CHAT_ID) entra em todas as listas na primeira vez.
"""

import json
import threading

import monitor as m

# Uma mensagem do Telegram tem até 4096 caracteres: ~300 inscritos no total.
LIMITE_TEXTO = 4000


class Assinantes:
    def __init__(self, bots: list[str], token: str, armazem: str, dono: str):
        self._lock = threading.Lock()
        self._token, self._armazem = token, armazem
        self._dono = int(dono) if dono.lstrip("-").isdigit() else None
        self._bots = bots
        self._msg_id: int | None = None
        self.listas: dict[str, list[int]] = {}

    @property
    def persistente(self) -> bool:
        return bool(self._token and self._armazem)

    def carregar(self) -> None:
        """Lê a lista da mensagem fixada no canal; bots novos começam com o dono."""
        if self.persistente:
            resposta = m._chamar_bot(self._token, "getChat", {"chat_id": self._armazem})
            if not resposta.get("ok"):
                m.log(f"Não consegui ler o canal dos inscritos: {resposta.get('description')}")
            fixada = (resposta.get("result") or {}).get("pinned_message") or {}
            try:
                lidas = json.loads(fixada.get("text") or "{}")
                self.listas = {bot: [int(x) for x in ids] for bot, ids in lidas.items()}
                self._msg_id = fixada.get("message_id")
            except (ValueError, TypeError, AttributeError):
                m.log("A mensagem fixada no canal dos inscritos não é uma lista válida; começando do zero.")
        else:
            m.log("Sem ARMAZEM_CHAT_ID: inscrições valem só até o próximo reinício.")
        novos = [bot for bot in self._bots if bot not in self.listas]
        for bot in novos:
            self.listas[bot] = [self._dono] if self._dono else []
        if novos:
            with self._lock:
                self._salvar()
        m.log("Inscritos: " + ", ".join(f"{bot} {len(self.listas[bot])}" for bot in self._bots))

    def de(self, bot: str) -> list[int]:
        with self._lock:
            return list(self.listas.get(bot, []))

    def entrar(self, bot: str, chat_id: int) -> bool:
        """True se a pessoa era nova nesta lista."""
        with self._lock:
            lista = self.listas.setdefault(bot, [])
            if chat_id in lista:
                return False
            lista.append(chat_id)
            self._salvar()
        m.log(f"Nova inscrição no bot {bot} ({len(lista)} inscritos).")
        return True

    def sair(self, bot: str, chat_id: int) -> bool:
        with self._lock:
            lista = self.listas.get(bot, [])
            if chat_id not in lista:
                return False
            lista.remove(chat_id)
            self._salvar()
        m.log(f"Saiu do bot {bot} ({len(lista)} inscritos).")
        return True

    def _salvar(self) -> None:
        """Chamado com o lock: edita a mensagem fixada (ou cria e fixa uma)."""
        if not self.persistente:
            return
        texto = json.dumps(self.listas, separators=(",", ":"))
        if len(texto) > LIMITE_TEXTO:
            m.log("Lista de inscritos grande demais para uma mensagem; a última inscrição não foi salva.")
            return
        if self._msg_id:
            resposta = m._chamar_bot(self._token, "editMessageText",
                                     {"chat_id": self._armazem, "message_id": self._msg_id, "text": texto})
            if resposta.get("ok") or "not modified" in str(resposta.get("description")):
                return
        resposta = m._chamar_bot(self._token, "sendMessage",
                                 {"chat_id": self._armazem, "text": texto, "disable_notification": True})
        if not resposta.get("ok"):
            m.log(f"Não consegui salvar os inscritos: {resposta.get('description')}")
            return
        self._msg_id = resposta["result"]["message_id"]
        m._chamar_bot(self._token, "pinChatMessage",
                      {"chat_id": self._armazem, "message_id": self._msg_id, "disable_notification": True})
