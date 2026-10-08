# Monitor de ofertas PS5

Lê as páginas públicas de canais de ofertas do Telegram e o pelando.com.br,
filtra ofertas de **console PS5** e avisa por um bot do Telegram.

Não usa login de conta do Telegram: os canais são lidos pela página pública
`t.me/s/<canal>`.

## Onde roda

- **Render (principal):** `servidor_web.py` roda sem parar no plano grátis,
  conferindo os canais a cada 1 minuto e o Pelando a cada 3. A configuração
  do serviço está em `render.yaml`. O endereço do serviço mostra uma página
  de status (rodadas, avisos enviados, situação do Pelando).
- **GitHub Actions (reserva, desligado):** `.github/workflows/monitor.yml`
  roda `nuvem.py` a cada 5 minutos. Fica desativado enquanto o Render estiver
  no ar, para não avisar em dobro.

Os dois precisam das variáveis `BOT_TOKEN` (token do bot do @BotFather) e
`CHAT_ID` (seu id numérico no Telegram).

## Ajustar

Canais, busca do Pelando e filtro ficam em `config.toml`. Depois de um commit,
o Render publica a versão nova sozinho.

Para testar o filtro sem enviar nada: `python nuvem.py --simular`.

## Limites do Render grátis

- 750 horas por mês, o suficiente para um serviço ligado o mês todo.
- Desliga após 15 minutos sem visitas; o próprio monitor visita o seu
  endereço a cada 10 minutos para continuar ligado.
- Pode reiniciar a qualquer momento. Depois de um reinício, o monitor
  recomeça do agora (não avisa ofertas antigas).
