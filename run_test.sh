#!/bin/bash
# ----------------------------------------------------------
# Script: run_test.sh
# Autor: Letícia Soares e Mateus Gonçalves
# Uso: ./run_test.sh <num_peers> <arquivo_teste> <tamanho_em_bytes> <block_size>
# Exemplo: ./run_test.sh 4 FileA.bin 10240 1024
# ----------------------------------------------------------

set -e

# === Parâmetros ===
NUM_PEERS=${1:-2}
FILE_NAME=${2:-FileA.bin}
FILE_SIZE=${3:-10240}     # 10 KB padrão
BLOCK_SIZE=${4:-1024}     # 1 KB padrão

BASE_PORT=9010
STORAGE_DIR="./blocks"
LOG_DIR="./logs"
NEIGH_DIR="./neighbors"
META_FILE="${FILE_NAME%.bin}.meta.json"

rm -rf "$LOG_DIR" "$NEIGH_DIR" "$STORAGE_DIR" "recon"
# === Preparação dos diretórios ===
mkdir -p  "$LOG_DIR" "$NEIGH_DIR" "$STORAGE_DIR"


echo "===================================================="
echo "🧩  Iniciando teste P2P - ${NUM_PEERS} peers"
echo "Arquivo: $FILE_NAME (${FILE_SIZE} bytes)"
echo "Bloco: ${BLOCK_SIZE} bytes"
echo "===================================================="

# === 1. Gera arquivo de teste com conteúdo aleatório ===
echo "📦  Gerando arquivo de teste..."
dd if=/dev/urandom of="$FILE_NAME" bs=1 count=$FILE_SIZE status=none

# === 2. Cria metadados e fragmenta arquivo no Seeder ===
echo "🌱  Preparando Seeder..."
python3 - <<EOF
from utilities import seed_prepare
seed_prepare("${FILE_NAME}", ${BLOCK_SIZE}, "${META_FILE}", "${STORAGE_DIR}", "peer1")
EOF

for ((i=1; i<=NUM_PEERS; i++)); do
  PEER_ID="peer$i"
  PORT=$((BASE_PORT + i - 1))

  NEIGH_FILE="${NEIGH_DIR}/${PEER_ID}_neighbors.json"
  echo "[" > "$NEIGH_FILE"

  FIRST=1
  for ((j=1; j<=NUM_PEERS; j++)); do
    if [ $i -ne $j ]; then
      PORT_J=$((BASE_PORT + j - 1))
      if [ $FIRST -eq 0 ]; then echo "," >> "$NEIGH_FILE"; fi
      echo -n "  {\"peer_id\": \"peer$j\", \"ip\": \"127.0.0.1\", \"port\": $PORT_J}" >> "$NEIGH_FILE"
      FIRST=0
    fi
  done

  echo "]" >> "$NEIGH_FILE"

  echo "✔️  Gerado: $NEIGH_FILE"
done

# === 4. Inicia os  Peers  ===
for ((i=1; i<=NUM_PEERS; i++)); do
  PORT=$((BASE_PORT + i - 1))
  PEER_ID="peer$i"
  echo "➡️  Iniciando  peer${i} na porta ${PORT}..."
  
  NEIGH_JSON="${NEIGH_DIR}/${PEER_ID}_neighbors.json"
  python3 -u p2p_peer.py \
    --peer-id "${i}" \
    --ip 127.0.0.1 \
    --port $PORT \
    --neighbors "$NEIGH_JSON" \
    --meta "$META_FILE" \
    --storage "$STORAGE_DIR"  2>&1 > "${LOG_DIR}/peer${i}.log" &  
  PIDS+=($!)
done


echo "===================================================="
echo "⏳  Todos os peers foram iniciados..."
echo "Logs individuais em ${LOG_DIR}/peerX.log"
echo "===================================================="

# # === 5. Aguardar término (CTRL+C para parar manualmente) ===
trap 'echo "🛑 Encerrando peers..."; kill ${PIDS[@]} 2>/dev/null' SIGINT SIGTERM

echo ${PIDS[@]}
# Monitora os processos até terminarem
wait "${PIDS[@]}"

# === 6. Verifica integridade dos arquivos reconstruídos ===
echo "===================================================="
echo "🔍  Verificando integridade dos arquivos reconstruídos"
echo "===================================================="

ORIG_HASH=$(sha256sum "$FILE_NAME" | cut -d' ' -f1)
for ((i=2; i<=NUM_PEERS; i++)); do
  RECON="peer${i}_RECONSTRUCTED_${FILE_NAME}"
  if [[ -f "$RECON" ]]; then
    HASH=$(sha256sum "$RECON" | cut -d' ' -f1)
    if [[ "$HASH" == "$ORIG_HASH" ]]; then
      echo "✅  peer${i}: Integridade OK!"
    else
      echo "❌  peer${i}: ERRO - Hash diferente!"
    fi
  else
    echo "⚠️  peer${i}: Arquivo reconstruído não encontrado!"
  fi
done

echo "===================================================="
echo "🏁  Teste finalizado!"
echo "===================================================="
