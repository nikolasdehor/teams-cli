# teams

Opere o Microsoft Teams pelo terminal: chats, canais, reuniões e arquivos.

CLI híbrida com dois motores:

- **xteams** (padrão): chats, canais, mensagens, threads, times, pessoas e
  agenda. Usa as credenciais do próprio Teams, sem Azure app e sem
  consentimento de admin. Uma conta por vez (use a do trabalho).
- **Graph** (exceção): criar reunião, anexar arquivo, criar canal/chat,
  `chat send --to` e qualquer comando com perfil não-padrão (ex: `pessoal`).

## Instalação

```sh
uv tool install /Users/nikolas/Projects/teams-cli
# motor xteams (binário pré-compilado, sem Rust):
gh release download v0.3.0 --repo boazy/xteams-cli \
  --pattern 'xteams_0.3.0_darwin_arm64*' --dir /tmp
tar xzf /tmp/xteams_0.3.0_darwin_arm64.tar.gz -C /tmp
install -m 755 /tmp/xteams ~/.local/bin/xteams

teams auth login                    # xteams: aprova o código no navegador
teams auth login --graph            # Graph: só quando precisar (reuniões, arquivos)
teams auth status                   # mostra os dois motores
```

Requer Python 3.11+. Login é device-code nos dois motores.

## Uso

```
teams [flags globais] <comando> [args] [flags]
teams auth login|status|logout [--graph] [--profile P]
teams teams list                                  # xteams
teams teams channels TEAM_ID                     # xteams
teams teams channel-create TEAM_ID --name N      # Graph
teams channel messages TEAM_ID CHANNEL_ID        # xteams
teams channel send TEAM_ID CHANNEL_ID --message T  # xteams (--file vai pro Graph)
teams chats [list] [--limit N]                   # xteams
teams chat context CHAT_ID [--limit N]           # xteams: leia ANTES de escrever
teams chat messages CHAT_ID                      # xteams (até 200; Graph até 50)
teams chat send CHAT_ID --message T              # xteams (--to/--file vão pro Graph)
teams chat create --members A,B                  # Graph
teams people search QUERY                        # xteams
teams meetings [list] [--days N]                 # xteams
teams meeting create --subject S --start ...     # Graph
```

Flags globais (valem antes ou depois do subcomando):

| Flag | Efeito |
|---|---|
| `--profile P` | perfil Graph (padrão: `trabalho`); fora do padrão força Graph |
| `--graph` | força o motor Graph/MSAL no comando |
| `--client-id ID` | Azure app client ID (só Graph) |
| `--tenant T` | `organizations`, `consumers`, `common` (padrão) ou tenant ID |
| `--json` / `--plain` | saída JSON / JSONL para scripts |
| `-q, --quiet` / `-v, --verbose` | silêncio / mostra o motor em stderr |
| `--no-input` | nunca pergunta; falha se faltar dado |
| `--dry-run` | mostra o plano de escrita sem executar (sem sessão) |
| `--yes` | pula confirmação de escrita (não pula a guarda de mensagem recente) |
| `--timeout SEG` | timeout por chamada (padrão: 60) |

## Antes de enviar

Quem escreve em nome de alguém precisa do contexto da conversa, não só do texto.
Três problemas reais que este fluxo evita: falar de uma pessoa como se estivesse
fora do grupo quando já estava (e já tinha visto a mensagem), mandar mensagem no
meio de uma conversa em que o dono da conta estava digitando ao vivo, e não ver
uma mensagem cuja data estava errada.

```sh
teams chat context CHAT_ID                # humano
teams chat context CHAT_ID --limit 30 --json
```

Em uma chamada devolve: tipo (1:1, grupo, reunião, canal), título, **membros**
(nome, e-mail, quem sou eu), as últimas mensagens em ordem cronológica com autor,
horário local (`America/Sao_Paulo`, ajustável com `--tz`), citações/respostas
(quem respondeu a quem e o trecho citado), menções, e um resumo: minha última
mensagem há quantos minutos, se outros escreveram depois e se estou "ativo agora"
(mensagem minha nos últimos 5 min, ajustável com `--active-minutes`).

Limites que a saída declara de propósito:

- O xteams **não lista membros**. Eles são inferidos de autores, citações,
  menções e eventos de entrada nas últimas 200 mensagens (`members_approximate:
  true`). Quem nunca escreveu pode estar no grupo; membros só com MRI aparecem em
  `members_unresolved_mris`. E-mails vêm de `user search` (exige login; desligue
  com `--no-emails`).
- Não existe recibo de leitura: não dá para saber quem já viu uma mensagem.
- A janela de leitura é de **200 mensagens** (teto do Teams por chamada).

### Guarda de envio

`chat send CHAT_ID` e `channel send` olham as últimas mensagens antes de enviar:

- Se **você** escreveu nessa conversa há menos de `--recent-minutes` (padrão 5),
  mostram as últimas mensagens e **abortam (exit 2)** quando não há como
  perguntar: `--no-input`, `--yes` ou sem TTY. Em terminal interativo avisam e
  seguem para a confirmação normal.
- `--force` ignora a guarda; `--recent-minutes 0` a desliga.
- `--dry-run` mostra as 5 últimas mensagens (e avisa se o envio real exigiria
  `--force`), sem nunca abortar.
- Não se aplica a `--to` nem a `--file` (vão pelo Graph, sem leitura da conversa)
  nem a quem o xteams não consegue identificar (avisa e segue).

Sugestão de texto para a skill do agente: `docs/skill-antes-de-enviar.md`.

### Ordem e datas erradas

Mensagens do xteams são ordenadas e deduplicadas por `sequenceId` (ou pelo id
numérico), nunca pela data: o relógio do remetente pode estar errado e o Teams
chega a exibir datas absurdas. O horário mostrado é o de **chegada ao servidor**
(`originalarrivaltime`); quando `composetime` diverge mais de 10 min, ou a data
destoa da ordem de `sequenceId`, a mensagem sai com uma anotação em `anomalies`.
Consumidores (scripts, vigias) devem guardar o último `sequenceId` visto, não a
última data.

## Contrato de I/O

- Dados em **stdout**, diagnósticos e erros em **stderr**.
- Escrita pede confirmação em terminal interativo; sem TTY exige `--yes`.
- `--message -` lê o texto do stdin; `--message-file` lê de arquivo.
- `chat send --to EMAIL` (Graph) usa o 1:1 existente ou cria um novo.
- Texto puro (sem `--html`) é enviado como HTML seguro: caracteres especiais escapados, URLs http(s) viram links clicáveis e quebras de linha viram `<br>`. Com `--html` o conteúdo vai como veio.

## Exit codes

| Código | Significado |
|---|---|
| 0 | sucesso (inclui `--dry-run` e ajuda) |
| 1 | falha do motor |
| 2 | uso inválido |
| 3 | sem sessão (`teams auth login`, ou com `--graph`) |
| 130 | interrompido (Ctrl-C) |

## Config

Precedência: flags > env > `~/.config/teamscli/config.toml` > padrões.

```toml
default_profile = "trabalho"
output = "human"  # human | json | plain
timeout = 60

[profiles.trabalho]
tenant = "organizations"

[profiles.pessoal]
tenant = "consumers"
```

Env: `TEAMS_PROFILE`, `TEAMS_CLIENT_ID`, `TEAMS_TENANT`, `TEAMS_XTEAMS_BIN`.
Cache Graph: `~/.config/teamscli/cache-<perfil>.json` (chmod 600).
Cache xteams: `~/.local/state/xteams/token-cache.json` (gerenciado pelo xteams).

## App do Azure (só Graph, opcional)

Por padrão o motor Graph usa um client ID público embutido. Para tenant
corporativo restritivo, registre seu app no Entra ID (public client,
delegated: `User.Read`, `Chat.ReadWrite`, `ChannelMessage.Read.All`,
`ChannelMessage.Send`, `Team.ReadBasic.All`, `Channel.ReadBasic.All`,
`Calendars.ReadWrite`, `OnlineMeetings.ReadWrite`, `Files.ReadWrite`)
e informe via `--client-id` ou `TEAMS_CLIENT_ID`.

## Exemplos

```sh
teams auth login
teams chats --limit 5
teams chat context '<chat-id>'
teams chat messages '<chat-id>' --limit 10
teams channel messages '<team>' '<channel-id>' --limit 5 --json
teams people search "Maria Silva"
teams meetings --days 7
teams chat send '<chat-id>' --message "indo" --dry-run
teams chat send --to fulano@exemplo.com --message "oi" --yes
teams meeting create --subject "Alinhamento" --start 2026-09-30T15:00 \
  --end 2026-09-30T16:00 --attendees a@x.com --yes
teams --profile pessoal chats --limit 5
```

## Limites conhecidos

- xteams usa APIs privadas do Teams: pode quebrar sem aviso. É conta única.
- Leitura de canal via Graph exige consentimento de admin no corporativo;
  por isso o padrão é xteams.
- `chat messages`/`channel messages` leem no máximo 200 mensagens por chamada
  via xteams (50 via Graph); `chat context` sempre lê 200 e exibe `--limit`.
- Anexo limitado a 4MB; link de compartilhamento é best-effort.
- Contas pessoais têm suporte parcial a Teams no Graph.

## Desenvolvimento

```sh
cd /Users/nikolas/Projects/teams-cli
uv venv && uv pip install --python .venv/bin/python -e . pytest
.venv/bin/python -m pytest tests/ -q
ruff check .
```
