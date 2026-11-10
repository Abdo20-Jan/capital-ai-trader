# Capital AI Trader

App de trading com agentes de IA: **dados de mercado reais** (preços, candles, volume
consolidado das bolsas dos EUA, notícias, documentos da SEC, redes sociais), agentes de
scalp / day trade / swing / longo prazo / notícias, agente de pesquisa com IA (local ou
Claude) e execução na **conta demo da Capital.com** ou em papel. Só o saldo é virtual.

> ⚠️ Educacional. O app recusa a conta real da Capital.com. CFDs são alavancados e a maioria
> das contas de varejo perde dinheiro. Nenhum modelo garante lucro.

## O que é real e de onde vem

| Dado | Fonte | Observação |
|---|---|---|
| Preço das ações (tempo real) | **Alpaca** (API oficial) — ou **Capital.com** quando conectada | Plano grátis da Alpaca: bolsa IEX em tempo real |
| Volume **consolidado** (todas as bolsas dos EUA) | **Alpaca SIP** | Grátis: 15 min de atraso · Plano pago ($99/mês): tempo real |
| Candles históricos | Alpaca (papel) / Capital.com (conectado) | |
| Cripto (BTC, ETH…) | Alpaca, sem chave | Volume = só da própria Alpaca, não o global |
| Notícias | Alpaca News (agências), Yahoo Finance, Google News, MarketWatch, Investing.com, Seeking Alpha | Uma fonte a cada 6 s, em rodízio |
| Documentos oficiais | **SEC EDGAR** (8-K, 10-Q, Form 4…) | Exige `SEC_CONTACT_EMAIL` |
| Sentimento social | **StockTwits** (API pública) | |
| Ordens | **Capital.com DEMO** (conectada) ou papel com preço real | |

Só usamos fontes oficiais ou que permitem acesso automatizado — nada de contornar bloqueios.

## Agentes

| Agente | Gráfico | Estratégia | Filtros |
|---|---|---|---|
| Scalper | 1 min | recuos na micro-tendência, máx. 20 min | spread, volume relativo (RVOL) |
| Day Trader (ML) | 15 min | modelo de Machine Learning + sentimento, zera no fim do dia | viés da pesquisa |
| Swing | 4 h | tendência EMA 50/200, entra no fim do recuo | viés da pesquisa veta entradas contrárias |
| Longo Prazo | diário | tendência longa, sai abaixo da média de 200 | viés da pesquisa |
| Notícias (IA) | 5 min | reage em segundos a notícia relevante | volume tem que confirmar (RVOL) |
| **Pesquisa** | — | dossiê por ativo → tese da IA (viés, confiança, motivos, riscos) | usado pelos outros |

O **gestor de risco** decide se cada ordem é enviada: banca virtual, risco por trade,
margem, exposição, perda diária, meta, sem posições opostas no mesmo ativo, limite de
ordens/hora, pausa geral. Agentes que vão bem ganham um pouco mais de risco; os que vão
mal perdem — e são pausados se o resultado recente for claramente ruim.

## IA: local (Qwen etc.) → Claude → dicionário

Em **Configurações → Inteligência artificial**:
- **Ollama** (`LOCAL_LLM_KIND=ollama`, ex.: `http://localhost:11434`, modelo `qwen3:32b`) ou
- **OpenAI-compatível** (`LOCAL_LLM_KIND=openai`: LM Studio, vLLM, llama.cpp server).

Modelos de ~27–32 B parâmetros precisam de ~20 GB de memória (quantização Q4): um Mac com
32 GB+ de memória unificada ou uma GPU de 24 GB. Se a IA local cair, o app usa o Claude (se
houver `ANTHROPIC_API_KEY`) e, por último, o dicionário.

**App na nuvem + IA no seu Mac:** o Railway precisa alcançar o seu Mac. Opções:
1. **vLLM numa GPU na nuvem** (RunPod, Vast.ai…) com `--api-key SUA_CHAVE` → informe o
   endereço e a chave no app (`LOCAL_LLM_KIND=openai`). O jeito mais simples e seguro.
2. **Túnel** do seu Mac (ex.: Cloudflare Tunnel nomeado, protegido por Cloudflare Access).
   Não exponha o Ollama sem proteção: ele não tem senha e qualquer um usaria sua máquina.

## Rodar no seu computador

```bash
python3.12 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
python run.py app            # → http://localhost:8000
```

## Publicar com link fixo (Railway + Vercel)

### 1. Railway (backend + o app completo) — obrigatório
1. Suba este projeto para um repositório **privado** no GitHub.
2. railway.com → **New Project → Deploy from GitHub repo** → escolha o repositório.
   O Railway usa o `Dockerfile` e o `railway.json` daqui.
3. No serviço: **+ New → Volume**, monte em **`/data`** (banco, credenciais criptografadas
   e modelos sobrevivem a novos deploys).
4. **Variables** (mínimo):
   ```
   APP_PASSWORD=uma-senha-forte
   APP_SECRET=uma-sequencia-aleatoria-longa
   ```
   Opcionais (também podem ser preenchidas depois pelo painel):
   `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY`, `ALPACA_FEED`, `ANTHROPIC_API_KEY`,
   `SEC_CONTACT_EMAIL`, `WATCHLIST`, `LOCAL_LLM_URL`, `LOCAL_LLM_KIND`, `LOCAL_LLM_MODEL`,
   `LOCAL_LLM_API_KEY`, `START_BALANCE`, `TARGET_BALANCE`.
5. **Settings → Networking → Generate Domain** → seu link fixo, ex.:
   `https://capital-ai-trader.up.railway.app`. Pronto: esse link já é o app inteiro.

Mantenha **1 réplica** (os agentes rodam dentro do processo). Sem `APP_PASSWORD` o servidor
recusa acesso — de propósito, para ninguém operar a sua conta.

### 2. Vercel (só o painel) — opcional
1. vercel.com → **Add New Project** → mesmo repositório.
2. **Root Directory:** `app/static` · **Framework:** Other.
3. **Environment Variable:** `API_URL=https://SEU-APP.up.railway.app` → Deploy.
4. No Railway, adicione `FRONTEND_ORIGINS=https://SEU-PROJETO.vercel.app`.

### 3. Depois do primeiro acesso
Entre com a senha → **Configurações**: chaves da Alpaca (dados reais), Capital.com (ordens
na demo, com botão de ordem de teste), IA (local/Claude), e-mail para a SEC e lista de ativos.

## Testes

```bash
python -m pytest -q
```

## Linha de comando (versão antiga, ainda funciona)
`python run.py check | train | backtest | bot | dashboard | test-order EPIC`
