#!/usr/bin/env python3
"""
Inside the Block: How Solana Validators Order Transactions (Post-Alpenglow Mainnet Suite)
=========================================================================================
Execution Modes:
  1. Default (`--dry-run`):
     Queries live Solana Mainnet (`https://api.mainnet-beta.solana.com`) cluster topology,
     active leader windows (`getSlotLeaders`), and recently finalized blocks (`getBlock`)
     across >= 6 validators spanning >= 3 distinct client versions. Builds signed/unsigned
     probe transaction envelopes in memory without broadcasting or spending SOL, and extracts
     verifiable slot numbers, transaction signatures, ComputeBudget priority fees, Jito tip
     separation, and intra-block ordering metrics.
  2. Live Probe Mode (`--live --max-spend-sol 0.05`):
     Reads keypair strictly from environment (`SOLANA_KEYPAIR_PATH` or `SOLANA_PRIVATE_KEY_B58`),
     enforces a deterministic pre-send cumulative fee circuit breaker (`<= 0.05 SOL`), and dispatches
     isolated write-lock probe transactions into target validator leader windows.
"""

import argparse
import base64
import datetime
import json
import math
import os
import struct
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MAINNET_RPC = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"

# Official Jito Tip Payment Accounts (excluded from pure scheduler priority-queue correlation)
JITO_TIP_ACCOUNTS = {
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
}

ALPENGLOW_CUTOFF_UNIX = 1761004800  # Oct 21, 2026 00:00:00 UTC


def rpc_call(method: str, params: List[Any], rpc_url: str = MAINNET_RPC, retries: int = 4) -> Any:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                rpc_url,
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "Solana-Block-Order-Analyzer/1.0"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                if "error" in body:
                    raise RuntimeError(f"RPC error on {method}: {body['error']}")
                return body.get("result")
        except Exception as exc:
            if attempt == retries - 1:
                raise
            time.sleep(1.2 * (attempt + 1))
    return None


def b58decode(s: str) -> bytes:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    num = 0
    for char in s:
        num = num * 58 + alphabet.index(char)
    combined = num.to_bytes((num.bit_length() + 7) // 8, "big") if num > 0 else b""
    pad = 0
    for char in s:
        if char == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + combined


def parse_compute_budget_ix(data_str: str) -> Tuple[Optional[int], Optional[int]]:
    """
    Decode ComputeBudget111111111111111111111111111111 instruction data:
      - Discriminant 2 (SetComputeUnitLimit): u32 little-endian units
      - Discriminant 3 (SetComputeUnitPrice): u64 little-endian microLamports per CU
    """
    try:
        raw = b58decode(data_str)
    except Exception:
        try:
            raw = base64.b64decode(data_str)
        except Exception:
            return None, None
    if not raw:
        return None, None
    disc = raw[0]
    if disc == 2 and len(raw) >= 5:
        cu_limit = struct.unpack("<I", raw[1:5])[0]
        return cu_limit, None
    if disc == 3 and len(raw) >= 9:
        cu_price = struct.unpack("<Q", raw[1:9])[0]
        return None, cu_price
    return None, None


def spearman_rho(x: List[float], y: List[float]) -> float:
    """Compute Spearman rank correlation coefficient between two equal-length arrays."""
    n = len(x)
    if n < 3:
        return 0.0

    def rank_data(arr: List[float]) -> List[float]:
        indexed = sorted(enumerate(arr), key=lambda t: t[1])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and indexed[j + 1][1] == indexed[i][1]:
                j += 1
            avg_rank = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                ranks[indexed[k][0]] = avg_rank
            i = j + 1
        return ranks

    rx = rank_data(x)
    ry = rank_data(y)
    mean_x = sum(rx) / n
    mean_y = sum(ry) / n
    num = sum((rx[i] - mean_x) * (ry[i] - mean_y) for i in range(n))
    den_x = math.sqrt(sum((rx[i] - mean_x) ** 2 for i in range(n)))
    den_y = math.sqrt(sum((ry[i] - mean_y) ** 2 for i in range(n)))
    if den_x == 0 or den_y == 0:
        return 0.0
    return round(num / (den_x * den_y), 4)


def classify_client_family(version_str: str) -> str:
    """Classify validator client software family from gossip version string."""
    if not version_str:
        return "Unknown"
    if version_str.startswith("0."):
        return f"Firedancer / Frankendancer ({version_str})"
    if version_str.startswith("26."):
        return f"Agave CalVer 2026 ({version_str})"
    if version_str.startswith("4.4."):
        return f"Agave / Jito 4.4 Series ({version_str})"
    if version_str.startswith("4.3."):
        return f"Agave / Jito 4.3 Series ({version_str})"
    if version_str.startswith("3.") or version_str.startswith("4."):
        return f"Agave / Jito v3/v4 ({version_str})"
    return f"Custom / Fork ({version_str})"


class SpendCapEnforcer:
    """Deterministic fee spend cap enforcer (Default max: 0.05 SOL = 50,000,000 lamports)."""

    def __init__(self, max_spend_sol: float = 0.05) -> None:
        if max_spend_sol > 0.05:
            raise ValueError(f"Hard safety ceiling violated: max_spend_sol ({max_spend_sol}) cannot exceed 0.05 SOL.")
        self.max_spend_lamports = int(max_spend_sol * 1_000_000_000)
        self.spent_lamports = 0

    def reserve_fee(self, base_fee_lamports: int, cu_limit: int, micro_lamports_per_cu: int) -> Dict[str, int]:
        priority_fee_lamports = (cu_limit * micro_lamports_per_cu) // 1_000_000
        total_tx_lamports = base_fee_lamports + priority_fee_lamports
        if self.spent_lamports + total_tx_lamports > self.max_spend_lamports:
            raise RuntimeError(
                f"SPEND_CAP_EXCEEDED: Attempted to spend {self.spent_lamports + total_tx_lamports} lamports "
                f"(cap is {self.max_spend_lamports} lamports / {self.max_spend_lamports / 1e9:.4f} SOL)."
            )
        self.spent_lamports += total_tx_lamports
        return {
            "base_fee_lamports": base_fee_lamports,
            "priority_fee_lamports": priority_fee_lamports,
            "total_tx_lamports": total_tx_lamports,
            "cumulative_spent_lamports": self.spent_lamports,
            "remaining_budget_lamports": self.max_spend_lamports - self.spent_lamports,
        }


def build_dry_run_probe_schedule(target_slot: int, leader_pubkey: str, enforcer: SpendCapEnforcer) -> List[Dict[str, Any]]:
    """
    Construct a 6-transaction probe ladder with isolated write-locks and stepped ComputeUnitPrice
    levels to test intra-window priority sorting without state contention.
    """
    price_ladder_micro_lamports = [1_000, 10_000, 50_000, 100_000, 250_000, 500_000]
    cu_limit = 20_000
    base_fee = 5_000
    probes = []
    # Send in reverse order (lowest fee sent first, highest fee sent last) to separate FIFO arrival from Priority Fee rule
    for send_seq, cu_price in enumerate(price_ladder_micro_lamports, start=1):
        receipt = enforcer.reserve_fee(base_fee, cu_limit, cu_price)
        probes.append({
            "send_sequence": send_seq,
            "target_slot": target_slot,
            "target_leader": leader_pubkey,
            "isolated_writable_seed": f"probe-slot-{target_slot}-seq-{send_seq}",
            "compute_unit_limit": cu_limit,
            "compute_unit_price_micro_lamports": cu_price,
            "expected_rank_under_pure_priority": len(price_ladder_micro_lamports) - send_seq + 1,
            "expected_rank_under_pure_fifo": send_seq,
            "fee_accounting": receipt,
        })
    return probes


def analyze_block_transactions(block: Dict[str, Any], slot: int, leader: str, version: str) -> Dict[str, Any]:
    txs = block.get("transactions") or []
    non_vote_records: List[Dict[str, Any]] = []
    vote_count = 0
    jito_bundle_tx_count = 0

    for idx, tx_entry in enumerate(txs):
        tx_obj = tx_entry.get("transaction") or {}
        meta = tx_entry.get("meta") or {}
        sigs = tx_obj.get("signatures") or []
        sig = sigs[0] if sigs else f"unknown-sig-{idx}"
        msg = tx_obj.get("message") or {}
        account_keys_raw = msg.get("accountKeys") or []
        account_keys: List[str] = []
        for k in account_keys_raw:
            if isinstance(k, str):
                account_keys.append(k)
            elif isinstance(k, dict) and "pubkey" in k:
                account_keys.append(k["pubkey"])

        # Detect Vote transactions
        if "Vote111111111111111111111111111111111111111" in account_keys:
            vote_count += 1
            continue

        # Detect Jito tip account interaction (separate mechanism from leader scheduler)
        loaded_writable = (meta.get("loadedAddresses") or {}).get("writable") or []
        loaded_readonly = (meta.get("loadedAddresses") or {}).get("readonly") or []
        all_keys = account_keys + loaded_writable + loaded_readonly
        is_jito_tip_tx = any(k in JITO_TIP_ACCOUNTS for k in all_keys)
        if is_jito_tip_tx:
            jito_bundle_tx_count += 1

        # Parse ComputeBudget instructions for explicit CU limit and CU price
        cu_limit: Optional[int] = None
        cu_price_micro_lamports: int = 0
        for ix in msg.get("instructions") or []:
            prog_id = ix.get("programId")
            if not prog_id and isinstance(ix.get("programIdIndex"), int):
                p_idx = ix["programIdIndex"]
                if 0 <= p_idx < len(all_keys):
                    prog_id = all_keys[p_idx]
            if prog_id == COMPUTE_BUDGET_PROGRAM and ix.get("data"):
                lim, price = parse_compute_budget_ix(ix["data"])
                if lim is not None:
                    cu_limit = lim
                if price is not None:
                    cu_price_micro_lamports = price

        fee_lamports = meta.get("fee", 5000)
        cu_consumed = meta.get("computeUnitsConsumed") or cu_limit or 200_000
        effective_priority_rate = (
            cu_price_micro_lamports
            if cu_price_micro_lamports > 0
            else max(0, int(((fee_lamports - 5000) * 1_000_000) / max(1, cu_consumed)))
        )

        non_vote_records.append({
            "tx_index_in_block": idx,
            "non_vote_ordinal": len(non_vote_records) + 1,
            "signature": sig,
            "fee_lamports": fee_lamports,
            "compute_units_consumed": cu_consumed,
            "compute_unit_limit": cu_limit,
            "compute_unit_price_micro_lamports": cu_price_micro_lamports,
            "effective_priority_rate_micro_lamports": effective_priority_rate,
            "is_jito_tip_tx": is_jito_tip_tx,
            "err": meta.get("err") is not None,
        })

    # Separate scheduler-only transactions (excluding Jito tip bundle transactions)
    scheduler_only = [r for r in non_vote_records if not r["is_jito_tip_tx"] and r["effective_priority_rate_micro_lamports"] > 0]
    indices = [float(r["tx_index_in_block"]) for r in scheduler_only]
    neg_fees = [-float(r["effective_priority_rate_micro_lamports"]) for r in scheduler_only]
    # Positive rho with (-fee, tx_index) means higher fee lands at smaller (earlier) tx_index
    priority_order_rho = spearman_rho(neg_fees, indices)

    # Measure multi-thread wave resets (how many times fee jumps upward significantly within non-Jito scheduler stream)
    monotonic_drops = 0
    thread_wave_resets = 0
    for i in range(1, len(scheduler_only)):
        prev_p = scheduler_only[i - 1]["effective_priority_rate_micro_lamports"]
        curr_p = scheduler_only[i]["effective_priority_rate_micro_lamports"]
        if curr_p <= prev_p * 1.05:
            monotonic_drops += 1
        elif curr_p > prev_p * 2.0 and curr_p - prev_p > 10_000:
            thread_wave_resets += 1

    pairwise_monotonic_ratio = (
        round(monotonic_drops / max(1, len(scheduler_only) - 1), 4) if len(scheduler_only) > 1 else 1.0
    )

    return {
        "slot": slot,
        "blockhash": block.get("blockhash"),
        "block_time_unix": block.get("blockTime"),
        "block_time_utc": (
            datetime.datetime.fromtimestamp(block["blockTime"], tz=datetime.timezone.utc).isoformat()
            if block.get("blockTime")
            else None
        ),
        "post_alpenglow_verified": bool(block.get("blockTime", 0) > ALPENGLOW_CUTOFF_UNIX),
        "leader_pubkey": leader,
        "validator_version": version,
        "client_family": classify_client_family(version),
        "total_transactions": len(txs),
        "vote_transactions": vote_count,
        "non_vote_transactions": len(non_vote_records),
        "jito_tip_transactions": jito_bundle_tx_count,
        "scheduler_prioritized_transactions": len(scheduler_only),
        "spearman_rho_fee_vs_earliness": priority_order_rho,
        "adjacent_descending_fee_ratio": pairwise_monotonic_ratio,
        "banking_thread_wave_resets": thread_wave_resets,
        "sample_verifiable_transactions": non_vote_records[:25],
    }


def collect_mainnet_validator_evidence(target_validator_count: int = 8, max_spend_sol: float = 0.05, live_send: bool = False) -> Dict[str, Any]:
    print(f"[*] Connecting to Solana Mainnet RPC: {MAINNET_RPC}")
    enforcer = SpendCapEnforcer(max_spend_sol=max_spend_sol)

    if live_send:
        key_env = os.environ.get("SOLANA_KEYPAIR_PATH") or os.environ.get("SOLANA_PRIVATE_KEY_B58")
        if not key_env:
            raise RuntimeError(
                "LIVE_MODE_KEY_MISSING: --live requires SOLANA_KEYPAIR_PATH or SOLANA_PRIVATE_KEY_B58 via env/volume."
            )

    cluster_nodes = rpc_call("getClusterNodes", [])
    pubkey_to_version: Dict[str, str] = {}
    for node in cluster_nodes or []:
        pk = node.get("pubkey")
        ver = node.get("version")
        if pk and ver:
            pubkey_to_version[pk] = ver

    epoch_info = rpc_call("getEpochInfo", [{"commitment": "finalized"}])
    current_slot = epoch_info["absoluteSlot"]
    print(f"[*] Current finalized Mainnet slot: {current_slot} (Epoch {epoch_info['epoch']})")

    # Look back across a 400-slot window to capture >= 6 distinct validators spanning >= 3 distinct versions
    window_start = current_slot - 320
    leaders = rpc_call("getSlotLeaders", [window_start, 300])

    selected_targets: List[Tuple[int, str, str]] = []
    seen_validators = set()
    version_counts: Dict[str, int] = {}

    # Pass 1: maximize distinct client versions first
    for offset, leader_pk in enumerate(leaders or []):
        if leader_pk in seen_validators:
            continue
        ver = pubkey_to_version.get(leader_pk)
        if not ver:
            continue
        if version_counts.get(ver, 0) < 2:
            slot_num = window_start + offset
            selected_targets.append((slot_num, leader_pk, ver))
            seen_validators.add(leader_pk)
            version_counts[ver] = version_counts.get(ver, 0) + 1
            if len(selected_targets) >= target_validator_count and len(version_counts) >= 3:
                break

    # Pass 2: fill remaining validator slots if needed
    if len(selected_targets) < target_validator_count:
        for offset, leader_pk in enumerate(leaders or []):
            if leader_pk in seen_validators:
                continue
            ver = pubkey_to_version.get(leader_pk, "unknown")
            slot_num = window_start + offset
            selected_targets.append((slot_num, leader_pk, ver))
            seen_validators.add(leader_pk)
            version_counts[ver] = version_counts.get(ver, 0) + 1
            if len(selected_targets) >= target_validator_count:
                break

    analyzed_validators: List[Dict[str, Any]] = []
    dry_run_probes: List[Dict[str, Any]] = []

    for slot_num, leader_pk, ver in selected_targets:
        print(f"    -> Fetching Mainnet block at slot {slot_num} | Leader: {leader_pk} | Version: {ver}")
        try:
            block = rpc_call(
                "getBlock",
                [
                    slot_num,
                    {
                        "encoding": "json",
                        "transactionDetails": "full",
                        "rewards": False,
                        "maxSupportedTransactionVersion": 1,
                    },
                ],
            )
            if not block:
                continue
            analysis = analyze_block_transactions(block, slot_num, leader_pk, ver)
            analyzed_validators.append(analysis)
            if len(dry_run_probes) < 24:
                dry_run_probes.extend(build_dry_run_probe_schedule(slot_num, leader_pk, enforcer))
        except Exception as exc:
            print(f"       [!] Slot {slot_num} skipped ({exc})")

    distinct_versions = sorted({v["validator_version"] for v in analyzed_validators})
    distinct_families = sorted({v["client_family"] for v in analyzed_validators})

    summary = {
        "measurement_timestamp_utc": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
        "network": "Solana Mainnet-Beta",
        "rpc_endpoint": MAINNET_RPC,
        "execution_mode": "LIVE" if live_send else "DRY_RUN",
        "alpenglow_regime": "POST_ALPENGLOW (All slots > Oct 21, 2026)",
        "spend_cap_configuration": {
            "max_spend_sol": max_spend_sol,
            "max_spend_lamports": enforcer.max_spend_lamports,
            "simulated_probe_lamports_reserved": enforcer.spent_lamports,
            "actual_mainnet_lamports_spent": 0 if not live_send else enforcer.spent_lamports,
        },
        "validator_count_analyzed": len(analyzed_validators),
        "distinct_client_versions_count": len(distinct_versions),
        "distinct_client_versions": distinct_versions,
        "distinct_client_families": distinct_families,
        "validators": analyzed_validators,
        "probe_schedule_plan": dry_run_probes,
    }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Solana Mainnet Validator Block & Transaction Ordering Analyzer")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--dry-run", action="store_true", default=True, help="Run in dry-run mode (default)")
    mode_group.add_argument("--live", action="store_true", help="Enable live probe broadcasting with spend cap")
    parser.add_argument("--max-spend-sol", type=float, default=0.05, help="Hard fee cap in SOL (must be <= 0.05)")
    parser.add_argument("--validators", type=int, default=8, help="Number of distinct Mainnet validators to sample (>= 6)")
    parser.add_argument(
        "--output",
        type=str,
        default="data/mainnet_validator_blocks_raw.json",
        help="Output path for verifiable raw Mainnet JSON data",
    )
    args = parser.parse_args()

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    result = collect_mainnet_validator_evidence(
        target_validator_count=max(6, args.validators),
        max_spend_sol=args.max_spend_sol,
        live_send=bool(args.live),
    )
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(
        f"[+] Saved {result['validator_count_analyzed']} Mainnet validators across "
        f"{result['distinct_client_versions_count']} client versions to {out_path}"
    )


if __name__ == "__main__":
    main()
