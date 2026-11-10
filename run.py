#!/usr/bin/env python3
"""Ponto de entrada.

  python run.py check       testa a conexão (Capital.com demo, notícias, IA)
  python run.py train       treina o modelo de ML e promove se for melhor
  python run.py backtest    backtest walk-forward + chance de $60 → $100
  python run.py bot         roda o bot
  python run.py dashboard   abre o painel em http://localhost:8000
  python run.py all         bot + painel juntos
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading

from trader.config import get_settings
from trader.store import Store

log = logging.getLogger("run")


def make_broker(s):
    if s.broker == "paper":
        from trader.paper import PaperBroker
        return PaperBroker([w.epic for w in s.watchlist], start_balance=s.start_balance, leverage=s.max_leverage)
    from trader.capital import CapitalClient
    return CapitalClient(s.capital_api_key, s.capital_identifier, s.capital_password)


def make_engine(s):
    from trader.engine import Engine
    from trader.sentiment import make_scorer
    scorer = make_scorer(s.anthropic_api_key, s.claude_model, [w.ticker for w in s.watchlist])
    return Engine(s, make_broker(s), Store(s.db_path), scorer)


def cmd_check(s) -> int:
    ok = True
    print(f"Corretora: {s.broker}")
    broker = make_broker(s)
    try:
        acct = broker.account()
        print(f"  ✔ conta: saldo {acct.balance:.2f} {acct.currency}, disponível {acct.available:.2f}")
    except Exception as e:  # noqa: BLE001
        print(f"  ✘ conta: {e}")
        return 1
    for w in s.watchlist:
        try:
            r = broker.rules(w.epic)
            q = broker.quote(w.epic)
            c = broker.candles(w.epic, s.resolution, 50)
            lev = 1 / r.margin_factor if r.margin_factor else 0
            print(f"  ✔ {w.epic:<8} {r.name[:28]:<28} bid {q.bid:>10.2f}  ask {q.ask:>10.2f}  "
                  f"{'ABERTO' if q.tradeable else 'fechado':<7} mín {r.min_size:g}  alav 1:{lev:.0f}  "
                  f"margem mín ${r.min_size * q.mid * r.margin_factor:,.2f}  candles {len(c)}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  ✘ {w.epic}: {e}  (dica: python run.py search {w.epic})")
    if s.broker == "capital_demo":
        from trader.news import fetch_rss
        items = fetch_rss([s.watchlist[0].ticker]) if s.watchlist else []
        print(f"  {'✔' if items else '✘'} notícias RSS: {len(items)} manchetes para {s.watchlist[0].ticker}")
    print(f"IA de sentimento: {'Claude (' + s.claude_model + ')' if s.use_claude else 'dicionário (sem ANTHROPIC_API_KEY)'}")
    return 0 if ok else 1


def cmd_search(s, term: str) -> int:
    if s.broker != "capital_demo":
        print("A busca de mercados só funciona com BROKER=capital_demo")
        return 1
    for m in make_broker(s).search_markets(term)[:20]:
        print(f"{m.get('epic', ''):<16} {m.get('instrumentName', '')[:40]:<40} {m.get('instrumentType', '')}")
    return 0


def cmd_train(s) -> int:
    eng = make_engine(s)
    r = eng.retrain()
    print(f"Modelo {r.version}: {r.rows} linhas | AUC long {r.auc_long:.3f} short {r.auc_short:.3f} | "
          f"expectativa {r.expectancy_r:+.3f}R em {r.trades} trades de teste (acerto {r.win_rate:.0%})")
    print(r.note)
    return 0


def cmd_backtest(s) -> int:
    from trader.backtest import monte_carlo, run_backtest
    from trader.features import to_frame
    broker = make_broker(s)
    frames = {w.epic: to_frame(broker.candles(w.epic, s.resolution, 1000)) for w in s.watchlist}
    rules = {w.epic: broker.rules(w.epic) for w in s.watchlist}
    res = run_backtest(frames, rules, s)
    print("=== Backtest walk-forward (período fora da amostra) ===")
    print(f"Saldo: ${res.start:.2f} → ${res.final:.2f} ({(res.final / res.start - 1):+.1%})")
    print(f"Trades: {res.trades} | acerto {res.win_rate:.0%} | expectativa {res.expectancy_r:+.3f}R | "
          f"drawdown máx {res.max_drawdown:.1%}")
    if res.leverage_used:
        import statistics
        lv = res.leverage_used
        print(f"Exposição por trade: média {statistics.mean(lv):.1f}× o patrimônio, máx {max(lv):.1f}× "
              f"(teto {s.max_exposure:g}×, conta permite até {s.max_leverage:g}×)")
    if res.skipped:
        print(f"Sinais não executados: {res.skipped}")
    mc = monte_carlo(res.r_multiples, s.start_balance, s.target_balance, s.start_balance * 0.5, s.risk_per_trade)
    print("\n=== Monte Carlo: chance de sair de "
          f"${s.start_balance:.0f} para ${s.target_balance:.0f} antes de cair para ${s.start_balance * 0.5:.0f} ===")
    print(f"Atinge a meta: {mc['p_target']:.1%} | quebra: {mc['p_ruin']:.1%} | nenhum em 500 trades: {mc['p_neither']:.1%}")
    if mc["median_trades_to_target"]:
        print(f"Mediana de trades até a meta (quando atinge): {mc['median_trades_to_target']}")
    if res.r_multiples:
        print("\n=== E se arriscar mais por trade (usar mais da alavancagem)? ===")
        print(f"{'risco/trade':>12} {'meta $' + format(s.target_balance, 'g'):>10} {'quebra $' + format(s.start_balance * 0.5, 'g'):>12} {'trades até a meta':>18}")
        for rk in (0.01, 0.02, 0.05, 0.10, 0.25):
            m = monte_carlo(res.r_multiples, s.start_balance, s.target_balance, s.start_balance * 0.5, rk)
            print(f"{rk:>12.0%} {m['p_target']:>10.1%} {m['p_ruin']:>12.1%} {str(m['median_trades_to_target'] or '—'):>18}")
        print("Mais risco por trade NÃO melhora a vantagem da estratégia; só acelera o resultado — para cima ou para baixo.")
    if s.broker == "paper":
        print("\n⚠ Dados SINTÉTICOS do simulador — isto testa o código, não prova nada sobre o mercado real.")
    return 0


def cmd_dashboard(s, port: int) -> None:
    import uvicorn
    from dashboard.app import create_app
    uvicorn.run(create_app(s), host="0.0.0.0", port=port, log_level="warning")


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot de trading com IA para a conta DEMO da Capital.com")
    ap.add_argument("command", choices=["check", "search", "train", "backtest", "bot", "dashboard", "all"])
    ap.add_argument("term", nargs="?", help="termo de busca (para 'search')")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--steps", type=int, default=None, help="número de ciclos (padrão: infinito)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = get_settings()

    if a.command == "check":
        return cmd_check(s)
    if a.command == "search":
        return cmd_search(s, a.term or "")
    if a.command == "train":
        return cmd_train(s)
    if a.command == "backtest":
        return cmd_backtest(s)
    if a.command == "dashboard":
        cmd_dashboard(s, a.port)
        return 0
    eng = make_engine(s)
    if a.command == "all":
        threading.Thread(target=cmd_dashboard, args=(s, a.port), daemon=True).start()
        print(f"Painel: http://localhost:{a.port}")
    try:
        eng.run_forever(max_steps=a.steps)
    except KeyboardInterrupt:
        print("\nBot parado. Posições abertas continuam na conta demo com stop e alvo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
