# Monitor de ofertas PS5

Roda no GitHub Actions a cada 5 minutos: lê as páginas públicas de canais de
ofertas do Telegram e o pelando.com.br, filtra ofertas de **console PS5** e
avisa por um bot do Telegram.

Não usa login de conta do Telegram: os canais são lidos pela página pública
`t.me/s/<canal>`.

## Configurar

1. Em **Settings > Secrets and variables > Actions**, crie dois segredos:
   - `BOT_TOKEN`: o token do bot (do @BotFather).
   - `CHAT_ID`: o seu id numérico no Telegram.
2. Ajuste canais, busca do Pelando e filtro em `config.toml`.

## Usar

- A aba **Actions** mostra cada rodada. **Run workflow** roda uma na hora.
- Para testar o filtro sem enviar nada: `python nuvem.py --simular`.
- Na primeira rodada ele só memoriza o que já está publicado; avisa do que
  aparecer depois.

## Limites

- O agendamento do GitHub pode atrasar em horários de pico.
- Em repositório público, o GitHub pausa agendamentos depois de 60 dias sem
  nenhum commit. Ele avisa por e-mail; para religar, é só clicar em
  "Enable workflow" na aba Actions.
