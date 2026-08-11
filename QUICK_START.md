# Quick start: task-agents

Hand one bounded task to one agent, on the host. Design and details:
`docs/design-task-agents.md`.

## Once

```bash
poetry install
cld build
```

## 1. Spawn

```bash
cd ~/projects/myrepo
cld task-agent start implementer -n add-oauth -p "Add OAuth login to the web app. Tests must pass."
```

- `implementer` — the role persona (`cld prompts` lists them).
- `-n add-oauth` — the task slug: names the container and its deliverable branch.

It starts working immediately; your task is its first turn.

## 2. Watch

```bash
cld task-agent status                  # phase, messages, cost
cld task-agent transcript add-oauth    # the conversation
```

## 3. Tell it to wrap up

```bash
echo "Wrap up: squash your work into add-oauth and report what landed." > /tmp/msg.md
python -m cld.messenger.send --to cld_agent_myrepo_add-oauth \
    --subject "wrap up" --body-file /tmp/msg.md

python -m cld.messenger.inbox          # its reply lands here
```

## 4. Take the work, then reap

```bash
jj log -r add-oauth                    # the branch survives teardown
cld task-agent shutdown add-oauth
```
