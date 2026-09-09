#!/usr/bin/env bash
# Deploy Pantry Pilot to Amazon Bedrock AgentCore Runtime with the AgentCore CLI.
#
# Prerequisites:
#   npm i -g @aws/agentcore
#   aws login   (or any credentials in the default chain) for the account in agentcore/aws-targets.json
#   Replace <ACCOUNT_ID> in agentcore/aws-targets.json with your 12-digit account id.
#   Enable Anthropic Claude model access in the Bedrock console for us-east-1.
set -euo pipefail

cd "$(dirname "$0")/../agentcore"

if grep -q '<ACCOUNT_ID>' aws-targets.json; then
  echo "edit agentcore/aws-targets.json: replace <ACCOUNT_ID> with your AWS account id" >&2
  exit 1
fi

agentcore validate
agentcore deploy -y

cat <<'EOF'

Deployed. Next:
  1. Copy the runtime ARN printed above (also in agentcore/.cli/).
  2. Smoke test:   agentcore invoke '{"action": "status"}'
  3. Point the console at it:
       AGENT_BACKEND=agentcore AGENT_RUNTIME_ARN=<arn> make serve
EOF
