FROM python:3.11-slim

WORKDIR /app

COPY analyzer.py /app/analyzer.py
RUN mkdir -p /app/data

# Keys only via environment/volume; never baked into image
ENV SOLANA_RPC_URL="https://api.mainnet-beta.solana.com"

ENTRYPOINT ["python3", "/app/analyzer.py"]
CMD ["--dry-run", "--validators", "8", "--output", "/app/data/mainnet_validator_blocks_raw.json"]
