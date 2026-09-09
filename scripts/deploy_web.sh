#!/usr/bin/env bash
# Host the web UI on AWS App Runner, pointed at the deployed AgentCore Runtime.
#
# No local Docker needed: CodeBuild clones the public GitHub repo (GitRef, default main), builds the
# image and pushes it to ECR; App Runner serves it with an HTTPS URL. Everything is one
# CloudFormation stack (infra/web.yaml). Re-run after pushing to rebuild; App Runner auto-deploys
# the new image.
#
# Requires: AWS CLI with credentials for the target account, the AgentCore runtime already
# deployed (scripts/deploy_agentcore.sh), and the commit you want to serve pushed to GitHub.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=pantry-pilot
RUNTIME_NAME=PantryPilot_PantryPilotAgent
STACK="${PROJECT}-web"
REGION="${AWS_REGION:-us-east-1}"
GIT_REF="${GIT_REF:-main}"

ARN="${AGENT_RUNTIME_ARN:-}"
if [ -z "$ARN" ]; then
  ARN=$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
    --query "agentRuntimes[?agentRuntimeName=='${RUNTIME_NAME}'].agentRuntimeArn | [0]" --output text)
fi
if [ -z "$ARN" ] || [ "$ARN" = "None" ]; then
  echo "No AgentCore runtime named ${RUNTIME_NAME} in ${REGION}; deploy it first or set AGENT_RUNTIME_ARN." >&2
  exit 1
fi

deploy() {
  aws cloudformation deploy --region "$REGION" --stack-name "$STACK" \
    --template-file infra/web.yaml --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset \
    --parameter-overrides AgentRuntimeArn="$ARN" GitRef="$GIT_REF" CreateService="$1"
}

echo "1/3  ECR repository and CodeBuild project (stack ${STACK})"
deploy false

echo "2/3  Building the image in CodeBuild from ${GIT_REF}"
BUILD_ID=$(aws codebuild start-build --region "$REGION" --project-name "${PROJECT}-web-build" \
  --query build.id --output text)
while :; do
  STATUS=$(aws codebuild batch-get-builds --region "$REGION" --ids "$BUILD_ID" \
    --query 'builds[0].buildStatus' --output text)
  case "$STATUS" in
    IN_PROGRESS) sleep 15 ;;
    SUCCEEDED) break ;;
    *) echo "CodeBuild ${BUILD_ID}: ${STATUS} (see CloudWatch log group /aws/codebuild/${PROJECT}-web-build)" >&2; exit 1 ;;
  esac
done

echo "3/3  App Runner service"
deploy true

URL=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='ServiceUrl'].OutputValue" --output text)
echo "Web UI: ${URL}"
