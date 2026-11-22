# Contributing to solana-validator-block-ordering

Thank you for helping improve this Solana Mainnet transaction-ordering measurement suite.

## Local Verification

Requires Python 3.11 or newer (Python standard library only, no third-party dependencies):

```bash
git clone https://github.com/jaharoni/solana-validator-block-ordering.git
cd solana-validator-block-ordering
python3 -m py_compile analyzer.py
python3 analyzer.py --help
```

To test against live Mainnet RPC without modifying the committed Epoch 1076 dataset, write output to `/tmp`:

```bash
python3 analyzer.py --dry-run --validators 8 --output /tmp/solana_test.json
```

If public Mainnet RPC returns `HTTP 429`, set `SOLANA_RPC_URL` to a dedicated RPC endpoint.

## Pull Request Rules

1. Never commit Solana keypairs, private keys, or RPC API keys to this repository.
2. Do not overwrite `data/mainnet_validator_blocks_raw.json` unless intentionally submitting a new documented epoch snapshot.
3. Preserve the `SpendCapEnforcer` ceiling (`<= 0.05 SOL`) for `--live` mode.
4. Keep PR descriptions brief and include how you verified your change.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](./LICENSE).
