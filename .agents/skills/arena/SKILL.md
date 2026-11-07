---
name: arena
description: Compare three independent approaches to a coding, research, or backtesting problem in this repository, evaluate the same evidence and constraints, and recommend the most defensible option. Use when the user invokes $arena or asks for an Arena-style comparison.
---

# Arena: three-approach comparison

Run an evidence-first comparison of **three genuinely different approaches** to the user's stated task. The purpose is to make a better decision, **not** to produce three answers and declare a winner without testing.

## 1. Scope and baseline

1. Restate the user's task, constraints, and success/failure criteria. If a task is underspecified, adopt explicit reversible assumptions and report them.
2. Inspect the existing repository, README, relevant code, tests, and data provenance before proposing changes.
3. Record the baseline and a common evaluation plan *before* seeing candidate results. Keep inputs, sample periods, costs, budgets, and metrics comparable.
4. Default to analysis only. Do not modify application code, place orders, publish results, merge branches, or connect accounts unless the user explicitly asks.
5. Do not request or expose credentials. Never place real securities orders; this repository is research-only.

## 2. Generate candidates independently

Use three isolated subagents **only when the Codex session exposes supported delegation tools**. Give each the same task description, baseline, data limitations, and scoring rubric. Have each return a separately identifiable proposal with: mechanism, assumptions, implementation impact, verification commands, failure modes, and evidence.

If subagent tools are unavailable, create the three alternatives sequentially in this session, without carrying evaluations from earlier alternatives into later generation. **Say explicitly that this was a single-model sequential comparison, not three independent models/agents.** Never claim to have called Claude, Gemini, separate LLMs, or external APIs unless tools actually did so.

Encourage meaningful diversity rather than superficial parameter differences. For a trading-strategy question, consider differentiated hypotheses (e.g. relative-volume/VWAP conditions, breakout confirmation, reversal/exhaustion), but choose hypotheses relevant to the user's actual request. A control/baseline can be one candidate where appropriate.

Do not let multiple agents write to the same worktree or files concurrently. For code implementation, propose isolated branches/worktrees or compare designs before editing; obtain explicit user direction before merging candidate code.

## 3. Validate fairly

For each proposal, report:

- **Evidence (0-30):** test or measurement strength, data quality, grounded observations; no invented backtests or performance.
- **Technical quality (0-30):** correctness, fit to requirements, simplicity, maintainability.
- **Reproducibility (0-20):** fixed inputs/configs, deterministic tests, traceable results.
- **Risk (0-20):** failure modes, security, operational risk, overfitting and unsupported assumptions.

A score is an editorial aid, not a measured return. Use "not measured" instead of making up values. Separate *proposed*, *implemented*, *tested*, and *verified on independent data*. Execute available local tests only when the environment permits and the action is safe; otherwise provide exact commands and mark results **not run**.

For trading/backtests in this repo, check specifically:

- data timestamps, look-ahead, retrospective exclusions, leakage, and missing 5-minute bars;
- frozen baseline vs truly disjoint out-of-sample data; stock-selection and multiple-testing bias;
- next-bar execution, volume/liquidity, limit/price guards, borrowability, slippage, fees, financing and short-sale constraints;
- insufficient observation days, unsettled positions, and separation of realized vs unconfirmed P&L;
- maximum drawdown, trade count, sample coverage, and uncertainty (not just headline profit);
- per-symbol 1,000,000 JPY independent simulations **are not** a shared 1,000,000 JPY portfolio.

Do not rank a strategy as profitable based on untested ideas, a selected sample, or synthetic test passes. If evidence is weak, say "winner undecided" and specify the next falsifiable test.

## 4. Present the decision in Japanese

Use this compact report format:

1. **結論** — recommended option or 保留, confidence, and why.
2. **共通条件** — scope, fixed evaluation criteria, available evidence.
3. **3案比較** — A/B/C, mechanism, pros/cons, measured results (or 未測定), risk, rubric score (or 採点保留).
4. **検証状況** — exact tests and whether *run/pass/fail/not run*; distinguish actual output from proposals.
5. **次のアクション** — one prioritized, safe, reversible next step. No production trading.
6. **限界** — data gaps, assumptions, bias, and whether real subagents or sequential evaluation were used.

Do not present the highest score as a reliable winner when critical criteria are unverified. Ask for review before any material change to research methodology.
