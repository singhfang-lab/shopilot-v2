#!/usr/bin/env bash
# deploy.sh — Build and deploy usb-assistant to Cloud Run (asia-southeast2)
#
# Prerequisites:
#   gcloud auth login && gcloud auth configure-docker asia-southeast2-docker.pkg.dev
#
# Usage:
#   ./deploy.sh [--skip-build]

set -euo pipefail

PROJECT_ID="usb-assistant-prod"
REGION="asia-southeast2"
SERVICE="usb-assistant"
REPO="usb-assistant"
IMAGE_BASE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/app"
TAG=$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M)

SKIP_BUILD=false
for arg in "$@"; do
  [[ "$arg" == "--skip-build" ]] && SKIP_BUILD=true
done

echo "▶ Project : $PROJECT_ID"
echo "▶ Region  : $REGION"
echo "▶ Image   : ${IMAGE_BASE}:${TAG}"
echo ""

# ── 1. Build via Cloud Build (no local Docker needed) ────────────────────────
if [[ "$SKIP_BUILD" == false ]]; then
  echo "── Building image via Cloud Build ──"
  gcloud builds submit . \
    --project="$PROJECT_ID" \
    --tag="${IMAGE_BASE}:${TAG}"
  # Also tag as latest
  gcloud container images add-tag "${IMAGE_BASE}:${TAG}" "${IMAGE_BASE}:latest" --quiet
fi

# ── 2. Deploy to Cloud Run ────────────────────────────────────────────────────
echo "── Deploying to Cloud Run ──"
gcloud run deploy "$SERVICE" \
  --project="$PROJECT_ID" \
  --image="${IMAGE_BASE}:${TAG}" \
  --region="$REGION" \
  --platform=managed \
  --allow-unauthenticated \
  --memory=2Gi \
  --cpu=2 \
  --min-instances=0 \
  --max-instances=10 \
  --concurrency=80 \
  --add-cloudsql-instances="${PROJECT_ID}:${REGION}:usb-assistant-db" \
  --set-secrets="\
DATABASE_URL=usb-assistant-db-url:latest,\
GEMINI_API_KEY=usb-assistant-gemini-key:latest,\
GOOGLE_MAPS_KEY=usb-assistant-maps-key:latest,\
JWT_SECRET=usb-assistant-jwt-secret:latest,\
ANTHROPIC_API_KEY=usb-assistant-anthropic-key:latest,\
OPENAI_API_KEY=usb-assistant-openai-key:latest" \
  --set-env-vars="USE_PGVECTOR=true"

echo ""
echo "✓ Deploy complete"
gcloud run services describe "$SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format="value(status.url)"
