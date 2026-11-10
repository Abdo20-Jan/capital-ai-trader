# Capital AI Trader — bot com IA para a conta DEMO da Capital.com

Plataforma inspirada no vídeo “Self-Improving AI Trading Agent”: lê notícias em tempo
real, mede o sentimento com IA, combina com um modelo de Machine Learning e envia ordens
(com stop e alvo) para a **conta demo** da Capital.com. Meta configurável: **US$60 → US$100**.

> ⚠️ Projeto educacional. O cliente da Capital.com **recusa a URL da conta real** de propósito.
> CFDs são alavancados e a maioria das contas de varejo perde dinheiro. Nenhum modelo garante lucro.

## Como funciona

```
Notícias (Yahoo RSS) → dedup → Sentimento + tema (Claude ou dicionário)
                                         │
Preços (API Capital.com) → indicadores ──┼→ Modelo ML (prob. de o alvo vir antes do stop)
                                         │
                              Gestão de risco (2% por trade, stop diário, meta)
                                         │
                              Ordem na conta demo (stop + alvo)  →  Painel web
```

| Módulo | O que faz |
|---|---|
| `trader/capital.py` | Cliente REST da Capital.com (sessão, preços, regras do ativo, ordens, confirmação). Só demo. |
| `trader/paper.py` | Simulador offline (preços e manchetes sintéticas) para testar sem conta. |
| `trader/news.py` | Busca manchetes por ticker e remove duplicatas. |
| `trader/sentiment.py` | Sentimento (−1 a +1), relevância e tema de cada manchete. Usa o Claude se houver `ANTHROPIC_API_KEY`; senão, dicionário financeiro. |
| `trader/features.py` | RSI, médias, ATR, retornos, volatilidade e rótulos “triple barrier”. |
| `trader/model.py` | Dois classificadores (compra/venda) com gradient boosting. **Auto-aperfeiçoamento:** retreina toda semana e só troca o modelo se o novo for melhor em dados que nenhum dos dois viu. |
| `trader/risk.py` | Tamanho da posição pelo risco, limite de margem, perda diária máxima, pausa ao atingir a meta, parada se a conta cair 50%. |
| `trader/engine.py` | O ciclo do bot. Decide uma vez por barra, por ativo. |
| `trader/backtest.py` | Backtest fora da amostra + Monte Carlo: “qual a chance de ir de $60 a $100 antes de cair a $30?”. |
| `dashboard/` | Painel web (patrimônio, meta, notícias, sentimento por ativo, sinais, trades, modelo, eventos). |

## Instalação

Requer Python 3.10+.

```bash
cd capital-ai-trader
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

## 1) Testar no simulador (sem conta)

Com `BROKER=paper` no `.env`:

```bash
python run.py train      # treina o modelo
python run.py backtest   # backtest + chance de bater a meta
python run.py all        # bot + painel em http://localhost:8000
```

## 2) Conectar na conta DEMO da Capital.com

1. Crie/acesse sua conta em capital.com e ative a **autenticação em dois fatores**.
2. Em **Configurações → API integrations → Generate new key**, crie a chave e defina uma
   **senha personalizada** para ela.
3. Garanta que a conta selecionada é a **Demo**.
4. No `.env`:
   ```
   BROKER=capital_demo
   CAPITAL_API_KEY=sua-chave
   CAPITAL_IDENTIFIER=seu-email-de-login
   CAPITAL_PASSWORD=senha-personalizada-da-chave
   ```
5. Teste e confira o tamanho mínimo e a margem de cada ativo:
   ```bash
   python run.py check
   python run.py search nasdaq    # descobre o EPIC de outros mercados
   ```
6. Rode:
   ```bash
   python run.py train
   python run.py backtest
   python run.py all
   ```

### IA (opcional)

Coloque `ANTHROPIC_API_KEY` no `.env` para o Claude analisar as manchetes (tema,
sentimento e relevância, com saída JSON validada). Sem a chave, o bot usa o dicionário,
que é bem mais fraco: a maioria das manchetes reais sai como neutra.

## A realidade dos US$60

- Com 2% de risco, cada trade arrisca ~US$1,20. Muitos ativos têm **tamanho mínimo**
  que obriga a arriscar mais que isso, ou exigem mais margem do que a conta tem. O bot
  **não opera** nesses casos e explica o motivo no painel. Use `python run.py check`
  e ajuste a `WATCHLIST` para ativos com mínimo compatível (índices e forex costumam ter
  mínimos menores; confira na sua conta).
- Pela documentação, **stop e alvo não podem ser usados em ações “reais”**; o bot foi
  feito para CFDs (o padrão da conta demo).
- O backtest usa sentimento = 0, porque a API não fornece notícias históricas. O bot
  grava o sentimento de cada barra; com o tempo, os retreinos passam a aprender com ele.
- Leia o Monte Carlo do `backtest` com atenção: se a expectativa for ≈ 0, a chance de
  bater a meta é parecida com a de perder metade da conta.
- Rode **meses** na demo antes de pensar em dinheiro real.

## Banca virtual e modo aprendizado

- **Banca virtual:** a conta demo costuma ter milhares de dólares. O bot opera como se
  tivesse só `START_BALANCE` (ex.: $60): banca = $60 + lucro/prejuízo desde que começou.
  `python run.py reset` recomeça a banca sem apagar notícias, sinais nem o modelo.
- **Modo aprendizado:** `MAX_DAILY_LOSS=0` desliga a pausa diária e `STOP_AT_TARGET=0`
  faz o bot continuar depois da meta. Gera mais trades executados para avaliar o bot,
  mas o modelo aprende com todas as barras de preço, operando ou não; esses ajustes
  não deixam o modelo mais inteligente.
- **O que realmente melhora o modelo é mais histórico** (`HISTORY_BARS`). No simulador,
  com o mesmo período de teste: 1000 barras → +0,13R por trade; 3000 → +0,22R;
  6000 → +0,28R. Uma banca maior ($1000) deu exatamente o mesmo resultado em R.

## Alavancagem 1:200

A alavancagem define quanto de **margem** cada posição prende, não quanto você arrisca.
Com 1:200, US$60 podem controlar até US$12.000, e uma oscilação de 0,5% contra
zeraria a conta. Por isso o bot calcula o tamanho de cada posição pelo **stop**
(2% do patrimônio por trade). A alavancagem alta só ajuda a destravar ativos que antes
exigiam margem demais, e o `MAX_EXPOSURE` impede que um stop muito curto gere uma
posição gigantesca (gaps e derrapagem passam por cima do stop).

O `backtest` mostra uma tabela com a chance de bater a meta para 1%, 2%, 5%, 10% e 25% de
risco por trade. Arriscar mais não aumenta a vantagem da estratégia; só faz o resultado,
bom ou ruim, chegar mais rápido.

## Configuração (`.env`)

| Variável | Padrão | Significado |
|---|---|---|
| `WATCHLIST` | AAPL,…,TSLA | EPICs da Capital.com (`EPIC:TICKER` se o ticker de notícias for outro) |
| `RESOLUTION` | MINUTE_15 | Tamanho da barra (MINUTE, MINUTE_5, MINUTE_15, MINUTE_30, HOUR, HOUR_4, DAY) |
| `RISK_PER_TRADE` | 0.02 | Fração do patrimônio arriscada por trade |
| `MAX_RISK_PER_TRADE` | 0.05 | Teto quando o tamanho mínimo força risco maior |
| `MAX_DAILY_LOSS` | 0.06 | Perda diária que pausa o bot |
| `MAX_OPEN_POSITIONS` | 2 | Posições simultâneas |
| `MAX_MARGIN_USAGE` | 0.5 | Fração máxima do patrimônio usada como margem |
| `MAX_LEVERAGE` | 200 | Alavancagem da conta (1:200). Na demo, a margem real de cada ativo vem da API; aqui só vale para o simulador |
| `MAX_EXPOSURE` | 30 | Valor total das posições ≤ 30× o patrimônio (com $60: no máximo $1.800) |
| `STOP_ATR` / `TAKE_PROFIT_ATR` | 1.5 / 2.25 | Stop e alvo em múltiplos do ATR |
| `PROB_THRESHOLD` | 0.50 | Probabilidade mínima do modelo para entrar |
| `SENTIMENT_WEIGHT` | 0.10 | Quanto o sentimento desloca a probabilidade |

## Testes

```bash
python -m pytest -q
```

Incluem o cliente da Capital.com com HTTP simulado (sessão, ordem, confirmação,
recusa da URL real), gestão de risco, rótulos, sentimento e um ciclo completo no simulador.
