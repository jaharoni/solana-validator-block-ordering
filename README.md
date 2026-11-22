# Inside the Block: How Solana Validators Order Transactions (Post-Alpenglow Mainnet Suite)

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
[![Python 3.11+ Zero Deps](https://img.shields.io/badge/Python-3.11%2B%20(Zero%20Deps)-3b82f6.svg)](#1-quick-start-one-command-reproducibility)

Reproducible empirical measurement suite and raw Epoch 1076 dataset investigating how Solana Mainnet validators running distinct client versions (`Agave/Jito 4.3.2`, `4.4.0`, `4.4.0-rc.1`, and `Agave CalVer 26.10.5`) order transactions inside a block following the **October 21, 2026 Alpenglow consensus upgrade**.

Links: [Empirical Findings Report (`REPORT.md`)](./REPORT.md) · [Raw Dataset](./data/mainnet_validator_blocks_raw.json) · [Contributing](./CONTRIBUTING.md) · [License (MIT)](./LICENSE)

---

## 1. Quick Start (One-Command Reproducibility)

Requirements: Python 3.11+ (zero external dependencies) or Docker. By default, **`--dry-run` mode** spends zero SOL and requires no private key. Write output to `/tmp` so you do not overwrite the committed Epoch 1076 reference dataset:

```bash
git clone https://github.com/jaharoni/solana-validator-block-ordering.git
cd solana-validator-block-ordering

# Verify CLI and inspect committed Epoch 1076 dataset offline
python3 analyzer.py --help

# Run a fresh Mainnet dry-run sample without overwriting data/mainnet_validator_blocks_raw.json
python3 analyzer.py --dry-run --validators 8 --output /tmp/mainnet_sample.json
```

Or run in Docker:

```bash
docker compose up --build
```

### Troubleshooting the First Run

- **`HTTP 429 Too Many Requests` or fewer than 8 validators analyzed**: Public Solana Mainnet RPC (`https://api.mainnet-beta.solana.com`) rate-limits full `getBlock` queries. Set `SOLANA_RPC_URL="https://<your-rpc-endpoint>"` before running `analyzer.py --dry-run`.
- **Avoiding dataset overwrites**: Pass `--output /tmp/mainnet_sample.json` if you want to keep `data/mainnet_validator_blocks_raw.json` clean in `git status`.

### Live Probe Mode (Behind Explicit Flag & Hard `0.05 SOL` Spend Cap)

Keys are **never** stored in the repository and are read strictly from environment variables (`SOLANA_KEYPAIR_PATH` or `SOLANA_PRIVATE_KEY_B58`) mounted at runtime. `SpendCapEnforcer` enforces a hard pre-broadcast ceiling of `<= 0.05 SOL` (`50,000,000 lamports`):

```bash
SOLANA_KEYPAIR_PATH=/run/secrets/id.json \
  python3 analyzer.py --live --max-spend-sol 0.05 --validators 8
```

---

## 2. Experimental Design & How Each Confounding Variable Is Handled

| Consideration | How This Experimental Design Handles It |
| :--- | :--- |
| **1. Leader Schedule & Leader Types** | Queries `getClusterNodes` to map validator identity pubkeys to live gossip software version strings (`4.3.2`, `4.4.0`, `4.4.0-rc.1`, `26.10.5`, `0.1.1`) and correlates against `getSlotLeaders` across the active Mainnet epoch (`Epoch 1076`). |
| **2. Transaction Path (Backend -> QUIC TPU / RPC -> Banking Stage)** | Transactions traverse RPC/TPU forwarders over QUIC streams subject to stake-weighted QoS (SWQoS) and per-connection stream throttling. To prevent network transit jitter from masquerading as a scheduler rule, probes are dispatched in reverse priority order (`1,000` -> `500,000` $\mu\text{lamports/CU}$) within the same burst window so pure FIFO arrival order (`expected_rank: 1..6`) is orthogonal to pure priority-queue order (`expected_rank: 6..1`). |
| **3. Interaction of Own Probe Transactions With Each Other** | If two probe transactions write-lock the same account (even the same fee-payer or state PDA), the leader's `ReadWriteAccountSet` serializes them into a dependency chain or defers the second transaction to a later banking wave. Our probe generator assigns a distinct deterministic writable seed (`probe-slot-<S>-seq-<K>`) per probe so all 6 probes in a burst are mutually non-conflicting and eligible for parallel/priority scheduling. |
| **4. Competition Inside the Block** | Public Mainnet blocks contain `297-783` competing user transactions per slot. We parse every non-vote transaction's `ComputeBudget111111111111111111111111111111` instructions (`SetComputeUnitLimit` discriminant `2` and `SetComputeUnitPrice` discriminant `3`) to measure where any priority rate sits relative to the rest of the slot's competing order flow. |
| **5. Jito Bundles vs. Native Leader Scheduler** | Validators running Jito-Solana process off-protocol bundles via `jito-relayer`/`block-engine`. As required by the specification, we explicitly detect all 8 canonical Jito tip payment accounts (`96gYZGLn...` through `3AVi9Tg9...`), flag them (`is_jito_tip_tx: true`), and exclude them from the pure scheduler correlation metrics (`scheduler_prioritized_transactions`). |
| **6. Send Order vs. Priority Rule vs. Noise** | We compute two complementary metrics on every block: (a) **Global Slot Spearman Rank Correlation ($\rho$)** between priority rate and `tx_index_in_block`, and (b) **Local Adjacent Descending Fee Ratio** + **Banking Thread Wave Resets**, which separates *local priority-queue ordering inside a banking micro-batch* from *continuous arrival-time streaming across the 400ms slot*. |
| **7. Alpenglow Regime Consistency** | All measurements are taken strictly **post-Alpenglow** (November 3, 2026 UTC, Epoch `1076`, Slots `465056847`-`465056896`), where consensus votes have migrated off-ledger (`vote_transactions == 0` in block payloads) and Transaction Version `1` (`maxSupportedTransactionVersion: 1`) is active on Mainnet. |

---

## 3. What This Design Deliberately Does NOT Separate

1. **Multi-Region TPU Ingress Jitter vs. SWQoS Packet Buffering**: From a single external vantage point (or standard RPC ingress), we cannot observe the exact microsecond a QUIC packet crosses the validator's kernel NIC (`sigverify` ingress timestamp) versus when the banking thread pops it. Therefore, when a higher-fee transaction lands at `tx_index = 13` after a lower-fee transaction at `tx_index = 4`, external block inspection alone cannot distinguish between *(a) the higher-fee transaction arrived 15ms later over QUIC* vs. *(b) it was assigned to a slower banking worker thread*.
2. **Private Stake-Weighted QoS Peering Agreements**: Validators running custom SWQoS overrides or private RPC forwarder whitelists prioritize ingress bandwidth before packets reach the central scheduler priority queue.

---

## 4. Repository Contents

- [`analyzer.py`](analyzer.py), Mainnet slot leader discovery, `getBlock` v1 transaction parser, `ComputeBudget` binary decoder, Jito tip separator, Spearman rank correlation engine, and `SpendCapEnforcer` (`<= 0.05 SOL`).
- [`data/mainnet_validator_blocks_raw.json`](data/mainnet_validator_blocks_raw.json), Full verifiable raw Mainnet dataset (`7` validators across `4` client versions, slot numbers, blockhashes, timestamps, and `175` verifiable transaction signatures with decoded fees and CU prices).
- [`REPORT.md`](REPORT.md), Empirical findings report with per-validator breakdown, verifiable `getBlock` reproduction commands, and the **"What Went Wrong"** engineering post-mortem.
- [`Dockerfile`](Dockerfile) & [`docker-compose.yml`](docker-compose.yml), Reproducible container environment.
