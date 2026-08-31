#!/usr/bin/env bash
# Oracle Cloud one-shot deploy for pm-recorder via OCI CLI.
# Prereqs (one-time): oci CLI installed + `~/.oci/config` working.
# Usage:
#   COMPARTMENT_ID=ocid1.tenancy.oc1..xxx ./deploy.sh
set -euo pipefail

REGION="${REGION:-eu-amsterdam-1}"
COMP="${COMPARTMENT_ID:?set COMPARTMENT_ID=<tenancy or compartment OCID>}"
SHAPE="${SHAPE:-VM.Standard.A1.Flex}"          # fallback: VM.Standard.E2.1.Micro
OCPUS="${OCPUS:-2}"
MEMGB="${MEMGB:-12}"
NAME="${NAME:-pm-recorder}"
SSH_PUB="$(cat "${SSH_PUB:-$HOME/.ssh/id_ed25519.pub}")"
HERE="$(cd "$(dirname "$0")/.." && pwd)"       # up_down project root

command -v oci >/dev/null || { echo "oci CLI not installed: uv tool install oci-cli"; exit 1; }

echo "== image =="
IMAGE=$(oci compute image list --region "$REGION" --compartment-id "$COMP" \
  --operating-system "Canonical Ubuntu" --operating-system-version "24.04" \
  --shape "$SHAPE" --sort-by TIMECREATED --query 'data[0].id' --raw-output)
echo "image: $IMAGE"

echo "== AD + subnet =="
AD=$(oci iam availability-domain list --region "$REGION" --compartment-id "$COMP" \
  --query 'data[0].name' --raw-output)
SUBNET=$(oci network subnet list --region "$REGION" --compartment-id "$COMP" \
  --query 'data[0].id' --raw-output)
if [ -z "$SUBNET" ] || [ "$SUBNET" = "null" ]; then
  echo "no subnet found -> creating minimal VCN (internet gateway + 0.0.0.0/0 route)"
  VCN=$(oci network vcn create --region "$REGION" --compartment-id "$COMP" \
    --display-name rec-vcn --cidr-blocks '["10.0.0.0/16"]' \
    --query 'data.id' --raw-output)
  IGW=$(oci network internet-gateway create --region "$REGION" --compartment-id "$COMP" \
    --vcn-id "$VCN" --display-name rec-igw --is-enabled true --query 'data.id' --raw-output)
  RT=$(oci network route-table list --region "$REGION" --compartment-id "$COMP" \
    --vcn-id "$VCN" --query 'data[0].id' --raw-output)
  oci network route-table update --region "$REGION" --rt-id "$RT" \
    --route-rules "[{\"destination\":\"0.0.0.0/0\",\"networkEntityId\":\"$IGW\"}]" >/dev/null
  SUBNET=$(oci network subnet create --region "$REGION" --compartment-id "$COMP" \
    --vcn-id "$VCN" --display-name rec-sub --cidr-block "10.0.1.0/24" \
    --route-table-id "$RT" --query 'data.id' --raw-output)
fi
echo "AD=$AD subnet=$SUBNET"

# open SSH if not already open (best effort)
SL=$(oci network subnet get --region "$REGION" --subnet-id "$SUBNET" \
  --query 'data."security-list-id"' --raw-output)
oci network security-list update --region "$REGION" --security-list-id "$SL" \
  --ingress-security-rules '[
    {"protocol":"6","source":"0.0.0.0/0","tcpOptions":{"destinationPortRange":{"min":22,"max":22}}},
    {"protocol":"1","source":"0.0.0.0/0","icmpOptions":{"type":8}}]' \
  --force >/dev/null 2>&1 || echo "(security list update skipped)"

echo "== launch instance ($SHAPE) =="
LAUNCH_ARGS=(
  --availability-domain "$AD" --compartment-id "$COMP"
  --shape "$SHAPE" --image-id "$IMAGE" --subnet-id "$SUBNET"
  --assign-public-ip true --display-name "$NAME"
  --ssh-authorized-keys-file "${SSH_PUB_FILE:-$HOME/.ssh/id_ed25519.pub}"
  --user-data-file "$HERE/oracle/cloud-init.yaml"
  --wait-for-state RUNNING
)
if [ "$SHAPE" = "VM.Standard.A1.Flex" ]; then
  LAUNCH_ARGS+=(--shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEMGB}")
fi
INSTANCE=$(oci compute instance launch --region "$REGION" "${LAUNCH_ARGS[@]}" \
  --query 'data.id' --raw-output)
echo "instance: $INSTANCE"

sleep 20
VNIC=$(oci compute instance list-vnics --region "$REGION" --compartment-id "$COMP" \
  --instance-id "$INSTANCE" --query 'data[0]."public-ip"' --raw-output)
echo ""
echo "==============================================="
echo " Instance UP. Public IP: $VNIC"
echo " Next steps:"
echo "   ssh ubuntu@$VNIC 'sudo usermod -aG docker ubuntu'  # then re-login"
echo "   rsync -av --exclude data $HERE/recorder/ ubuntu@$VNIC:/opt/recorder/"
echo "   ssh ubuntu@$VNIC 'cd /opt/recorder && sudo docker compose up -d --build'"
echo "==============================================="
