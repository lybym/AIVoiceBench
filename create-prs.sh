#!/bin/bash
# PR Creation Script — creates pull requests for all AIVoiceBench branches
#
# PREREQUISITES:
#   1. Install GitHub CLI: https://cli.github.com/
#   2. Run: gh auth login
#   3. Then run this script: bash create-prs.sh
#
# If gh is not available, you can create PRs manually via the URLs printed below.

set -e

REPO="lybym/AIVoiceBench"
BRANCHES=(
  "issue-23-acoustic-segmentation:integration/import-analysis-foundation:feat: add acoustic speech segmentation with energy VAD (#23)"
  "issue-24-fusion-turns-events:issue-23-acoustic-segmentation:feat: add segment fusion, turn builder, automatic event detection (#24)"
  "issue-25-expanded-metrics:issue-24-fusion-turns-events:feat: add expanded latency metrics and timeline metric computation (#25)"
  "docker-packaging:issue-25-expanded-metrics:feat: package analysis pipeline as Docker container with Web API"
  "issue-10-llm-harness:docker-packaging:feat: add LLM Harness with structured semantic evaluation (#10)"
  "issue-11-findings-report:issue-10-llm-harness:feat: add finding generation and evidence-linked report rendering (#11)"
  "issue-26-human-revision:issue-11-findings-report:feat: complete pipeline with human revision, LLM integration, Docker rebuild (#26)"
)

echo "=== AIVoiceBench PR Creation ==="
echo ""

if command -v gh &> /dev/null; then
  echo "✓ GitHub CLI found"
  for entry in "${BRANCHES[@]}"; do
    IFS=':' read -r head base title <<< "$entry"
    echo ""
    echo "Creating PR: $head → $base"
    echo "  Title: $title"
    gh pr create --repo "$REPO" --head "$head" --base "$base" --title "$title" --body "Auto-generated PR for AIVoiceBench pipeline stage. See docs/ for details." 2>&1 || echo "  (may already exist or needs auth)"
  done
else
  echo "✗ GitHub CLI not found. Create PRs manually:"
  echo ""
  for entry in "${BRANCHES[@]}"; do
    IFS=':' read -r head base title <<< "$entry"
    echo "  $head → $base"
    echo "    URL: https://github.com/$REPO/pull/new/$head"
    echo "    Title: $title"
    echo ""
  done
  echo ""
  echo "To install GitHub CLI:"
  echo "  Windows: winget install GitHub.cli"
  echo "  Mac: brew install gh"
  echo "  Linux: https://github.com/cli/cli/blob/main/docs/install_linux.md"
  echo ""
  echo "Then: gh auth login && bash create-prs.sh"
fi
