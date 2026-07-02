#!/usr/bin/env bash
# SensorChain Pune-shard Fabric network: bring up, deploy chaincode, tear down.
#
#   ./network.sh up        generate crypto material + genesis block, start
#                          orderer + 3 peers, create the "pune" channel
#   ./network.sh deploy    package/install/approve/commit the chaincode
#                          (chaincode-as-a-service) on all three orgs
#   ./network.sh down      stop everything and wipe generated material
#
# Requires: docker + docker compose. All Fabric tooling runs inside the
# hyperledger/fabric-tools image — nothing to install on the host.
set -euo pipefail
cd "$(dirname "$0")"

CHANNEL=pune
CC_NAME=sensorchain
CC_VERSION=1.0
CC_SEQUENCE=1
TOOLS_IMG=hyperledger/fabric-tools:2.5

ORDERER_CA=crypto-config/ordererOrganizations/sensorchain.local/orderers/orderer.sensorchain.local/tls/ca.crt
ORDERER_ADMIN_CERT=crypto-config/ordererOrganizations/sensorchain.local/orderers/orderer.sensorchain.local/tls/server.crt
ORDERER_ADMIN_KEY=crypto-config/ordererOrganizations/sensorchain.local/orderers/orderer.sensorchain.local/tls/server.key

# org name | msp id | peer host | port
ORGS=(
  "cityspv|CitySPVMSP|peer0.cityspv.sensorchain.local|7051"
  "integrator|IntegratorMSP|peer0.integrator.sensorchain.local|8051"
  "cpcb|CPCBMSP|peer0.cpcb.sensorchain.local|9051"
)

tools() {  # run a fabric CLI inside the tools image, on the docker network
  docker run --rm --network sensorchain \
    -v "$PWD":/work -w /work \
    -e FABRIC_CFG_PATH=/etc/hyperledger/fabric \
    "$@"
}

as_org() {  # as_org <org> <mspid> <peerhost> <port> -- peer CLI env
  local org=$1 msp=$2 host=$3 port=$4; shift 4
  tools \
    -e CORE_PEER_TLS_ENABLED=true \
    -e CORE_PEER_LOCALMSPID="$msp" \
    -e CORE_PEER_ADDRESS="$host:$port" \
    -e CORE_PEER_MSPCONFIGPATH="/work/crypto-config/peerOrganizations/$org.sensorchain.local/users/Admin@$org.sensorchain.local/msp" \
    -e CORE_PEER_TLS_ROOTCERT_FILE="/work/crypto-config/peerOrganizations/$org.sensorchain.local/peers/$host/tls/ca.crt" \
    "$TOOLS_IMG" "$@"
}

cmd_up() {
  mkdir -p channel-artifacts
  docker network inspect sensorchain >/dev/null 2>&1 || docker network create sensorchain
  echo ">> generating identities (cryptogen)"
  tools "$TOOLS_IMG" cryptogen generate --config=crypto-config.yaml --output=crypto-config

  echo ">> generating genesis block for channel '$CHANNEL'"
  tools -e FABRIC_CFG_PATH=/work "$TOOLS_IMG" \
    configtxgen -profile PuneShard -outputBlock "channel-artifacts/${CHANNEL}.block" -channelID "$CHANNEL"

  echo ">> starting orderer + peers"
  docker compose up -d orderer.sensorchain.local \
    peer0.cityspv.sensorchain.local peer0.integrator.sensorchain.local peer0.cpcb.sensorchain.local
  sleep 5

  echo ">> joining orderer to channel (channel participation API)"
  tools "$TOOLS_IMG" osnadmin channel join \
    --channelID "$CHANNEL" --config-block "channel-artifacts/${CHANNEL}.block" \
    -o orderer.sensorchain.local:7053 \
    --ca-file "/work/$ORDERER_CA" \
    --client-cert "/work/$ORDERER_ADMIN_CERT" \
    --client-key "/work/$ORDERER_ADMIN_KEY"

  for entry in "${ORGS[@]}"; do
    IFS="|" read -r org msp host port <<<"$entry"
    echo ">> joining $host to channel"
    as_org "$org" "$msp" "$host" "$port" \
      peer channel join -b "channel-artifacts/${CHANNEL}.block"
  done
  echo ">> network up: channel '$CHANNEL' with ${#ORGS[@]} orgs"
}

cmd_deploy() {
  echo ">> packaging chaincode (ccaas)"
  mkdir -p ccpackage
  cat > ccpackage/connection.json <<EOF
{"address": "sensorchain-cc:9999", "dial_timeout": "10s", "tls_required": false}
EOF
  cat > ccpackage/metadata.json <<EOF
{"type": "ccaas", "label": "${CC_NAME}_${CC_VERSION}"}
EOF
  tar -czf ccpackage/code.tar.gz -C ccpackage connection.json
  tar -czf "ccpackage/${CC_NAME}.tar.gz" -C ccpackage metadata.json code.tar.gz

  PACKAGE_ID=$(tools "$TOOLS_IMG" peer lifecycle chaincode calculatepackageid "ccpackage/${CC_NAME}.tar.gz")
  echo ">> package id: $PACKAGE_ID"

  echo ">> starting chaincode service"
  CHAINCODE_ID="$PACKAGE_ID" docker compose up -d sensorchain-cc

  for entry in "${ORGS[@]}"; do
    IFS="|" read -r org msp host port <<<"$entry"
    echo ">> installing on $host"
    as_org "$org" "$msp" "$host" "$port" \
      peer lifecycle chaincode install "ccpackage/${CC_NAME}.tar.gz"
    echo ">> approving for $msp"
    as_org "$org" "$msp" "$host" "$port" \
      peer lifecycle chaincode approveformyorg \
      -o orderer.sensorchain.local:7050 --tls --cafile "/work/$ORDERER_CA" \
      --channelID "$CHANNEL" --name "$CC_NAME" --version "$CC_VERSION" \
      --sequence "$CC_SEQUENCE" --package-id "$PACKAGE_ID"
  done

  echo ">> committing chaincode definition (majority endorsement)"
  IFS="|" read -r org msp host port <<<"${ORGS[0]}"
  as_org "$org" "$msp" "$host" "$port" \
    peer lifecycle chaincode commit \
    -o orderer.sensorchain.local:7050 --tls --cafile "/work/$ORDERER_CA" \
    --channelID "$CHANNEL" --name "$CC_NAME" --version "$CC_VERSION" --sequence "$CC_SEQUENCE" \
    --peerAddresses peer0.cityspv.sensorchain.local:7051 \
    --tlsRootCertFiles /work/crypto-config/peerOrganizations/cityspv.sensorchain.local/peers/peer0.cityspv.sensorchain.local/tls/ca.crt \
    --peerAddresses peer0.integrator.sensorchain.local:8051 \
    --tlsRootCertFiles /work/crypto-config/peerOrganizations/integrator.sensorchain.local/peers/peer0.integrator.sensorchain.local/tls/ca.crt \
    --peerAddresses peer0.cpcb.sensorchain.local:9051 \
    --tlsRootCertFiles /work/crypto-config/peerOrganizations/cpcb.sensorchain.local/peers/peer0.cpcb.sensorchain.local/tls/ca.crt

  echo ">> chaincode '$CC_NAME' committed on '$CHANNEL'"
  IFS="|" read -r org msp host port <<<"${ORGS[0]}"
  as_org "$org" "$msp" "$host" "$port" \
    peer lifecycle chaincode querycommitted --channelID "$CHANNEL" --name "$CC_NAME"
}

cmd_down() {
  docker compose down -v --remove-orphans || true
  rm -rf crypto-config channel-artifacts ccpackage
  echo ">> network down, artifacts removed"
}

case "${1:-}" in
  up) cmd_up ;;
  deploy) cmd_deploy ;;
  down) cmd_down ;;
  *) echo "usage: $0 {up|deploy|down}"; exit 1 ;;
esac
