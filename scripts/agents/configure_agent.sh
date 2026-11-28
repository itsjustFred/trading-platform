#!/usr/bin/env bash
# Write the global opencode config and the stage's agent definition. The
# agent lives in the global config, not in the checkout; its system prompt is
# the skill body (the skill's own frontmatter is dropped).
#
# Usage: configure_agent.sh <review|mutation|premortem>
set -euo pipefail

stage="${1:?usage: configure_agent.sh <review|mutation|premortem>}"
case "$stage" in
  review)
    agent_id="pr-review"
    description="Read-only automated PR reviewer (CI only)"
    edit_effect="deny"
    empty_noun="Review agent"
    ;;
  mutation)
    agent_id="pr-mutation"
    description="Automated PR mutation tester (CI only)"
    edit_effect="allow"
    empty_noun="Mutation agent"
    ;;
  premortem)
    agent_id="pr-premortem"
    description="Read-only automated PR pre-mortem (CI only)"
    edit_effect="deny"
    empty_noun="Pre-mortem agent"
    ;;
  *)
    echo "::error::configure_agent.sh: unknown stage '$stage'"
    exit 1
    ;;
esac

conf="$HOME/.config/opencode"
mkdir -p "$conf/agents"
# A `kilo/...` model runs on Kilo's anonymous gateway (ai-gateway.kilo.ai, no key,
# like the Zen free models) and an `nvidia/...` model on NVIDIA's API (below);
# every other model gets the config it always had.
# The gateway URL and the model itself are declared, not left to opencode's
# catalog: with only `"kilo": {}` opencode 2.2.7 resolved the model in some fresh
# homes and in others silently ran its default model instead (run 45239957263
# pinged `nex-agi/nex-n2.6`, "You need to sign in"). Declared, 5 of 5 fresh homes
# reached the configured model.
# MODEL is provider/model; a bare or empty model id would only reach
# opencode as a model it cannot resolve, so it stops here with its name.
if [[ "${MODEL%%/*}" = "$MODEL" || -z "${MODEL#*/}" ]]; then
  echo "::error::MODEL '${MODEL}' is not provider/model (e.g. nvidia/stepfun-ai/step-5-preview)."
  exit 1
fi
if [[ "${MODEL%%/*}" = "kilo" ]]; then
  jq -cn --arg model "${MODEL#kilo/}" '{"$schema": "https://opencode.ai/config.json", update: "disable", providers: {kilo: {settings: {baseURL: "https://ai-gateway.kilo.ai/api/v1"}, models: {($model): {}}}}}' > "$conf/opencode.json"
elif [[ "${MODEL%%/*}" = "nvidia" ]]; then
  # An `nvidia/...` model runs on NVIDIA's API with the key from the
  # ACTIONS_NVIDIA_API_KEY secret, which only the probe and agent steps
  # receive (as NVIDIA_API_KEY, the variable opencode's built-in provider
  # reads). The model is declared for the same reason as Kilo's.
  jq -cn --arg model "${MODEL#nvidia/}" '{"$schema": "https://opencode.ai/config.json", update: "disable", providers: {nvidia: {models: {($model): {}}}}}' > "$conf/opencode.json"
else
  # shellcheck disable=SC2016 # `$schema` is a literal JSON key
  printf '{"$schema":"https://opencode.ai/config.json","update":"disable"}\n' > "$conf/opencode.json"
fi
model="$MODEL"
if [[ -n "$VARIANT" ]]; then model="${MODEL}#${VARIANT}"; fi
{
  printf -- '---\n'
  printf 'description: %s\n' "$description"
  printf 'mode: primary\n'
  printf 'model: "%s"\n' "$model"
  printf 'permissions:\n'
  printf '  - { action: "*", resource: "*", effect: deny }\n'
  printf '  - { action: read, resource: "*", effect: allow }\n'
  printf '  - { action: glob, resource: "*", effect: allow }\n'
  printf '  - { action: grep, resource: "*", effect: allow }\n'
  printf '  - { action: shell, resource: "*", effect: allow }\n'
  printf '  - { action: external_directory, resource: "*", effect: allow }\n'
  printf '  - { action: edit, resource: "*", effect: %s }\n' "$edit_effect"
  printf '  - { action: read, resource: "*.env", effect: deny }\n'
  printf '  - { action: read, resource: "*.env.*", effect: deny }\n'
  printf -- '---\n'
  awk 'BEGIN { n = 0 } /^---$/ && n < 2 { n++; next } n >= 2' "$RUNNER_TEMP/$stage/skill.md"
} > "$conf/agents/$agent_id.md"
# The system prompt must not be empty (e.g. a skill without frontmatter).
if [[ "$(awk 'BEGIN{n=0} /^---$/ && n<2 {n++; next} n>=2' "$conf/agents/$agent_id.md" | tr -d '[:space:]' | wc -c)" -eq 0 ]]; then
  echo "::error::${empty_noun} has an empty system prompt."
  exit 1
fi
OPENCODE_DISABLE_PROJECT_CONFIG=1 opencode debug agents --standalone \
  | jq -e --arg id "$agent_id" '.[] | select(.id == $id) | .model' > /dev/null
