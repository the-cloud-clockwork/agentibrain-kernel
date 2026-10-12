# Repository rulesets

`dev-no-delete.json` is the update payload for the existing dev ruleset. Dev
blocks deletion and force push, keeps linear history, takes changes only by
squash pull request with zero required approvals, requires the `Gate — Required`
check from `ci.yml`, and merges through a merge queue that runs `ci.yml` on
`merge_group`. The bypass list is empty.

Agents never apply rulesets. The operator applies the payload once this file
is on dev:

```bash
gh api \
  --method PUT \
  repos/the-cloud-clockwork/agentibrain-kernel/rulesets/16090911 \
  --input .github/rulesets/dev-no-delete.json
```

Read it back with admin access; a read only token omits `bypass_actors`:

```bash
gh api repos/the-cloud-clockwork/agentibrain-kernel/rulesets/16090911 \
  --jq '{bypass_actors, rules: [.rules[].type]}'
gh api repos/the-cloud-clockwork/agentibrain-kernel/rules/branches/dev \
  --jq '[.[].type]'
```

The read back must show an empty `bypass_actors` and the rules `deletion`,
`non_fast_forward`, `required_linear_history`, `pull_request`,
`required_status_checks` and `merge_queue`.
