# Tironi Outreach

Disparador B2B em Python, inicialmente configurado para e-mail via Brevo SMTP. Integra-se ao mesmo PostgreSQL do CNPJ ETL, mas grava somente no schema `outreach`.

## Segurança antes de começar

A chave SMTP nunca deve ser versionada. Gere uma nova credencial na Brevo, configure-a somente no `.env`/secret do servidor e mantenha `DRY_RUN=true` até validar remetente, DNS e campanha. A senha não está incluída neste projeto.

O remetente deve estar validado na Brevo. Configure SPF, DKIM e DMARC no domínio.

## Recursos

- Sincronização de leads das views v1 ou v2 do CNPJ ETL.
- Campanhas e templates personalizados.
- Aprovação obrigatória por padrão.
- Fila PostgreSQL concorrente com `SKIP LOCKED`.
- Limites diário, por hora e por domínio.
- SMTP Brevo com STARTTLS.
- Modo seguro `DRY_RUN`.
- Link assinado de descadastro e lista permanente de supressão.
- API FastAPI e dashboard agregado em JSON.
- Estrutura preparada para adaptadores futuros.

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

Consulte as mensagens no banco ou, futuramente, pelo front-end. Depois de revisar:

```bash
curl -X POST http://localhost:8000/api/campaigns/1/approve -H "X-API-Key: SUA_API_KEY"
```

### 4. Envio real

Somente depois de testes, troque:

```env
DRY_RUN=false
```

O worker envia apenas no horário configurado e respeita todos os limites.

## Envio diário pelo GitHub Actions

O workflow `Envio diário da campanha` conecta ao PostgreSQL definido em
`DATABASE_URL`, sincroniza a base, prepara a campanha e processa até 300 mensagens.
Ele roda diariamente às 09:00 no horário de São Paulo.

Antes de habilitar o envio real:

1. Configure os secrets `DATABASE_URL`, `SMTP_USERNAME`, `SMTP_PASSWORD`,
   `OUTREACH_FROM_EMAIL`, `OUTREACH_REPLY_TO` e `UNSUBSCRIBE_SECRET`.
2. Configure a variável `PUBLIC_BASE_URL` com a URL HTTPS da API.
3. Execute manualmente com `dry_run=true` e limite `1`.
4. Revise o resumo da execução antes de usar `dry_run=false`.
5. Depois da validação, configure `ENABLE_DAILY_OUTREACH=true` nas variables do
   GitHub Actions para liberar o agendamento.

Cada lead recebe no máximo um envio inicial e um follow-up após sete dias. Leads
marcados como `replied` ou presentes em `outreach.suppressions` não recebem o
follow-up. A lista não reinicia depois disso; apenas novos leads entram na campanha.
Enquanto não houver integração com a caixa de entrada, registre uma resposta com
`POST /api/messages/{id}/reply` e o cabeçalho `X-API-Key`.

Se uma execução for interrompida durante o SMTP, a mensagem fica como
`delivery_uncertain` e novos lotes são bloqueados para evitar duplicidade. Consulte
o log da Brevo e resolva com `POST /api/messages/{id}/resolve-delivery`, enviando
`{"delivered": true}` ou `{"delivered": false}`.

Os runners hospedados pelo GitHub usam IPs de saída dinâmicos. O PostgreSQL precisa
aceitar essas conexões e o bloqueio por IP das chaves SMTP na Brevo precisa ser
compatível com esse modelo. Para restringir por um único IP, use um runner próprio
com saída fixa.

## Outros canais

WhatsApp, Instagram, Facebook e LinkedIn não estão ativos nesta versão. Eles serão adicionados por adapters próprios. Não use automação de navegador nem endpoints não oficiais. Instagram/Facebook devem respeitar as janelas e permissões da Meta; LinkedIn deve começar como tarefa manual.

## Produção

- Use secrets do provedor de deploy, nunca `.env` no Git.
- Publique `PUBLIC_BASE_URL` com HTTPS para o descadastro.
- Mantenha `REQUIRE_MANUAL_APPROVAL=true` no início.
- Não envie para endereços suprimidos ou funções fiscal/financeiro.
- Cadastre webhooks da Brevo numa próxima etapa para delivery, bounce, spam e reply.
- O SMTP confirma aceitação do relay, não entrega final. Até integrar webhooks, o status `sent` significa aceito pela Brevo.

## Testes

```bash
pip install -e '.[dev]'
pytest -q
ruff check src tests
ruff format --check src tests
```

