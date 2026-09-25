# Tironi Outreach

Disparador B2B em Python, configurado para e-mail via SendPulse SMTP. Integra-se ao mesmo PostgreSQL do CNPJ ETL, mas grava somente no schema `outreach`.

## Segurança antes de começar

A senha SMTP nunca deve ser versionada. Copie as credenciais em **Configurações SMTP > Geral** da SendPulse, configure-as somente no `.env`/secret do servidor e mantenha `DRY_RUN=true` até validar remetente, DNS e campanha. A senha não está incluída neste projeto.

O perfil SMTP precisa estar aprovado e o remetente validado na SendPulse. Configure SPF, DKIM, DMARC e, se usar rastreamento, o CNAME próprio no domínio.

## Recursos

- Sincronização de leads das views v1 ou v2 do CNPJ ETL.
- Campanhas e templates personalizados.
- Aprovação obrigatória por padrão.
- Fila PostgreSQL concorrente com `SKIP LOCKED`.
- Limites diário, por hora e por domínio.
- SMTP SendPulse com STARTTLS ou TLS implícito.
- E-mail responsivo em HTML com alternativa em texto puro.
- Cabeçalhos de descadastro em um clique e `Precedence: bulk`.
- Modo seguro `DRY_RUN`.
- Link assinado de descadastro e lista permanente de supressão.
- Webhook SendPulse normalizado para bounce, spam e descadastro, com supressão imediata.
- Pausa preventiva automática em 5% de bounce ou 0,2% de denúncias após amostra mínima.
- Seleção A-primeiro por score e confiança, mantendo somente 400 mensagens na fila.
- Oito janelas úteis de até 50 envios/hora, com 72 segundos entre mensagens.
- API FastAPI e dashboard agregado em JSON.
- Estrutura preparada para adaptadores futuros.
- Modelos narrativos são carregados de forma idempotente na primeira consulta à
  aba **Modelos**, inclusive em deploys serverless que não executam o CLI de migração.

## Instalação

```bash
cp .env.example .env
# Preencha a nova credencial SMTP, remetente verificado, API_KEY e UNSUBSCRIBE_SECRET.
docker compose up -d postgres
docker compose run --rm api outreach migrate
docker compose up -d api worker
```

Se usar o PostgreSQL do CNPJ ETL, altere `DATABASE_URL` e não suba o serviço `postgres` deste Compose.

## Fluxo

### 1. Sincronizar leads qualificados

```bash
docker compose exec api outreach sync-leads
```

### 2. Criar campanha pela API

```bash
curl -X POST http://localhost:8000/api/campaigns \
  -H "X-API-Key: SUA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name":"Varejo sem chatbot",
    "subject_template":"Uma ideia para a {empresa}",
    "body_template":"Olá, equipe da {empresa}. Identificamos uma oportunidade de automatizar atendimento e vendas. Posso enviar uma sugestão objetiva?"
  }'
```

Placeholders permitidos: `{empresa}`, `{razao_social}` e `{cnpj}`. Evite colocar o CNPJ ou dados cadastrais no texto enviado.

### 3. Preparar e revisar

```bash
curl -X POST http://localhost:8000/api/campaigns/1/prepare -H "X-API-Key: SUA_API_KEY"
```

Consulte as mensagens no banco ou, futuramente, pelo front-end. A campanha padrão já inclui o layout HTML Tironi Tech. Depois de revisar:

```bash
curl -X POST http://localhost:8000/api/campaigns/1/approve -H "X-API-Key: SUA_API_KEY"
```

### 4. Envio real

Somente depois de testes, troque:

```env
DRY_RUN=false
```

O worker envia apenas no horário configurado e respeita todos os limites.

## Envio horário pelo GitHub Actions

O workflow `Envio horário dos melhores leads` conecta ao PostgreSQL definido em
`DATABASE_URL`, sincroniza a base, mantém uma fila curta ordenada por qualidade A,
score e confiança e processa até 50 mensagens por hora, espaçadas em 72 segundos.
Ele roda de segunda a sexta, em oito janelas entre 09:00 e 17:00 no horário de São
Paulo, respeitando o teto gratuito de 400 mensagens por dia.

Antes de habilitar o envio real:

1. Configure os secrets `DATABASE_URL`, `SMTP_USERNAME`, `SMTP_PASSWORD`,
   `OUTREACH_FROM_EMAIL`, `OUTREACH_REPLY_TO` e `UNSUBSCRIBE_SECRET`.
2. Configure a variável `PUBLIC_BASE_URL` com a URL HTTPS da API.
3. Execute manualmente com `dry_run=true` e limite `1`.
4. Revise o resumo da execução antes de usar `dry_run=false`.
5. Depois da validação, configure `ENABLE_HOURLY_OUTREACH=true` nas variables do
   GitHub Actions para liberar o agendamento. A variável antiga
   `ENABLE_DAILY_OUTREACH=true` continua aceita por compatibilidade.

Cada lead recebe no máximo um envio inicial e um follow-up após sete dias. Leads
marcados como `replied` ou presentes em `outreach.suppressions` não recebem o
follow-up. A lista não reinicia depois disso; apenas novos leads entram na campanha.
Enquanto não houver integração com a caixa de entrada, registre uma resposta com
`POST /api/messages/{id}/reply` e o cabeçalho `X-API-Key`.

Se uma execução for interrompida durante o SMTP, a mensagem fica como
`delivery_uncertain` e novos lotes são bloqueados para evitar duplicidade. Consulte
o histórico SMTP da SendPulse e resolva com `POST /api/messages/{id}/resolve-delivery`, enviando
`{"delivered": true}` ou `{"delivered": false}`.

Os runners hospedados pelo GitHub usam IPs de saída dinâmicos. O PostgreSQL precisa
aceitar essas conexões e o bloqueio por IP das credenciais SMTP na SendPulse precisa ser
compatível com esse modelo. Para restringir por um único IP, use um runner próprio
com saída fixa.

## Outros canais

WhatsApp, Instagram, Facebook e LinkedIn não estão ativos nesta versão. Eles serão adicionados por adapters próprios. Não use automação de navegador nem endpoints não oficiais. Instagram/Facebook devem respeitar as janelas e permissões da Meta; LinkedIn deve começar como tarefa manual.

## Produção

- Use secrets do provedor de deploy, nunca `.env` no Git.
- Publique `PUBLIC_BASE_URL` com HTTPS para o descadastro.
- Mantenha `REQUIRE_MANUAL_APPROVAL=true` no início.
- Não envie para endereços suprimidos ou funções fiscal/financeiro.
- Cadastre webhooks da SendPulse numa próxima etapa para delivery, bounce, spam e reply.
- O SMTP confirma aceitação do relay, não entrega final. Até integrar webhooks, o status `sent` significa aceito pela SendPulse.

## Checklist de entregabilidade na SendPulse

1. Aguarde a aprovação do perfil SMTP.
2. Valide `tironitech.com` e o endereço remetente na SendPulse.
3. Publique exatamente os registros SPF e DKIM fornecidos no painel.
4. Comece o DMARC com `p=none`, acompanhe os relatórios e só depois avance a política.
5. Use um CNAME de rastreamento no próprio domínio, se o rastreamento estiver ativo.
6. Aqueça o domínio gradualmente e envie apenas para contatos pertinentes e válidos.
7. Cadastre o webhook SMTP para
   `https://tironi-outreach.vercel.app/api/webhooks/sendpulse?secret=SEU_SEGREDO`,
   marcando entrega, falha, hard/soft bounce, spam e descadastro. Use no endereço o mesmo
   valor secreto configurado em `SENDPULSE_WEBHOOK_SECRET`.
8. Monitore bounce, denúncia, descadastro e resposta; o webhook pausa campanhas ativas
   preventivamente em 5% de bounce ou 0,2% de denúncias, antes dos limites da SendPulse.

Para a porta `587`, use `SMTP_STARTTLS=true` e `SMTP_SSL=false`. Para a porta `465`, use o inverso. Os valores exibidos na sua conta SendPulse prevalecem sobre os exemplos do projeto.

## Testes

```bash
pip install -e '.[dev]'
pytest -q
ruff check src tests
ruff format --check src tests
```

